# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""One-time `login`: authorise the tool via the OAuth 2.0 device flow (RFC 8628).

The owner approves on another device (e.g. their phone); the resulting refresh token is
stored in the state database for the daemon to use.
"""

import sys
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from youtube_helper.config import load_state_dir
from youtube_helper.state import StateStore, StateUnavailable

SECRETS_DIR = Path("/run/secrets")
CLIENT_ID_SECRET = "google_client_id"
CLIENT_SECRET_SECRET = "google_client_secret"

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# RFC 8628: poll every 5 seconds unless told otherwise, and 5 seconds slower after slow_down.
DEFAULT_POLL_INTERVAL = 5
SLOW_DOWN_STEP = 5

HTTP_TIMEOUT = 30


class LoginFailed(Exception):
    pass


@dataclass(frozen=True)
class ClientCredentials:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


def run_login(
    environ: Mapping[str, str],
    *,
    secrets_dir: Path = SECRETS_DIR,
    http: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Log in and store the refresh token; return a non-zero exit status on failure."""
    try:
        credentials = read_client_credentials(secrets_dir)
        with closing(StateStore.open(load_state_dir(environ))) as state:
            with http or httpx.Client(timeout=HTTP_TIMEOUT) as client:
                authorization = request_device_code(client, credentials)
                print(
                    f"To authorise youtube-helper, open {authorization.verification_url}"
                    f" and enter the code {authorization.user_code}",
                    flush=True,
                )
                print("Waiting for approval…", flush=True)
                refresh_token = poll_for_refresh_token(
                    client, credentials, authorization, sleep=sleep, clock=clock
                )
            state.save_refresh_token(refresh_token)
    except (LoginFailed, StateUnavailable) as error:
        print(f"Login failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Login cancelled", file=sys.stderr)
        return 130
    print("Logged in. The daemon picks up the login at its next cycle.")
    return 0


def read_client_credentials(secrets_dir: Path) -> ClientCredentials:
    return ClientCredentials(
        _read_secret(secrets_dir / CLIENT_ID_SECRET),
        _read_secret(secrets_dir / CLIENT_SECRET_SECRET),
    )


def _read_secret(path: Path) -> str:
    try:
        value = path.read_text().strip()
    except OSError as error:
        raise LoginFailed(f"can't read secret {path}: {error.strerror or error}") from error
    if not value:
        raise LoginFailed(f"secret {path} is empty")
    return value


def request_device_code(http: httpx.Client, credentials: ClientCredentials) -> DeviceAuthorization:
    body = _post(http, DEVICE_CODE_URL, {"client_id": credentials.client_id, "scope": SCOPE})
    if "error" in body:
        raise LoginFailed(f"Google refused the device code request: {_describe_error(body)}")
    try:
        return DeviceAuthorization(
            device_code=str(body["device_code"]),
            user_code=str(body["user_code"]),
            # Google says verification_url; RFC 8628 says verification_uri.
            verification_url=str(body.get("verification_url") or body["verification_uri"]),
            expires_in=int(body["expires_in"]),
            interval=int(body.get("interval", DEFAULT_POLL_INTERVAL)),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise LoginFailed(f"unexpected device code response from Google: {body}") from error


def poll_for_refresh_token(
    http: httpx.Client,
    credentials: ClientCredentials,
    authorization: DeviceAuthorization,
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> str:
    """Poll the token endpoint until the owner approves, denies or the code expires."""
    deadline = clock() + authorization.expires_in
    interval = authorization.interval
    while True:
        sleep(interval)
        body = _post(
            http,
            TOKEN_URL,
            {
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "device_code": authorization.device_code,
                "grant_type": DEVICE_CODE_GRANT_TYPE,
            },
        )
        match body.get("error"):
            case None:
                refresh_token = body.get("refresh_token")
                if not isinstance(refresh_token, str) or not refresh_token:
                    raise LoginFailed("Google's token response contains no refresh token")
                return refresh_token
            case "authorization_pending":
                pass
            case "slow_down":
                interval += SLOW_DOWN_STEP
            case "access_denied":
                raise LoginFailed("access was denied; run `login` again to retry")
            case "expired_token":
                raise _expired()
            case _:
                raise LoginFailed(f"Google refused the login: {_describe_error(body)}")
        if clock() >= deadline:
            raise _expired()


def _expired() -> LoginFailed:
    return LoginFailed("the code expired before it was approved; run `login` again")


def _post(http: httpx.Client, url: str, data: dict[str, str]) -> dict[str, Any]:
    """POST a form and return the JSON object Google answers with, error responses included."""
    try:
        response = http.post(url, data=data)
    except httpx.HTTPError as error:
        raise LoginFailed(f"can't reach {url}: {error}") from error
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise LoginFailed(f"unexpected response from {url}: HTTP {response.status_code}")
    return body


def _describe_error(body: Mapping[str, Any]) -> str:
    description = body.get("error_description")
    return f"{body['error']} ({description})" if description else str(body["error"])
