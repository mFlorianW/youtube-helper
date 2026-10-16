# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""What every Google call shares: the owner's OAuth client and Google's token endpoint."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

SECRETS_DIR = Path("/run/secrets")
CLIENT_ID_SECRET = "google_client_id"
CLIENT_SECRET_SECRET = "google_client_secret"

TOKEN_URL = "https://oauth2.googleapis.com/token"

HTTP_TIMEOUT = 30


class CredentialsUnavailable(Exception):
    pass


class UnexpectedResponse(Exception):
    """Google couldn't be reached or didn't answer with a JSON object."""


@dataclass(frozen=True)
class ClientCredentials:
    client_id: str
    client_secret: str


def read_client_credentials(secrets_dir: Path) -> ClientCredentials:
    return ClientCredentials(
        _read_secret(secrets_dir / CLIENT_ID_SECRET),
        _read_secret(secrets_dir / CLIENT_SECRET_SECRET),
    )


def _read_secret(path: Path) -> str:
    try:
        value = path.read_text().strip()
    except OSError as error:
        raise CredentialsUnavailable(
            f"can't read secret {path}: {error.strerror or error}"
        ) from error
    if not value:
        raise CredentialsUnavailable(f"secret {path} is empty")
    return value


def send(http: httpx.Client, request: httpx.Request) -> tuple[int, dict[str, Any]]:
    """Send `request`; return the status and the JSON object Google answers with."""
    try:
        response = http.send(request)
    except httpx.HTTPError as error:
        raise UnexpectedResponse(f"can't reach {request.url}: {error}") from error
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise UnexpectedResponse(
            f"unexpected response from {request.url}: HTTP {response.status_code}"
        )
    return response.status_code, body


def describe_oauth_error(body: Mapping[str, Any]) -> str:
    """The `error` of an OAuth error response, with its description if there is one."""
    description = body.get("error_description")
    return f"{body['error']} ({description})" if description else str(body["error"])
