# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The Account port: the owner's current Subscriptions, read from their Google account."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from youtube_helper.google import (
    SECRETS_DIR,
    TOKEN_URL,
    CredentialsUnavailable,
    UnexpectedResponse,
    describe_oauth_error,
    read_client_credentials,
    send,
)
from youtube_helper.state import StateStore

SUBSCRIPTIONS_URL = "https://www.googleapis.com/youtube/v3/subscriptions"
PAGE_SIZE = 50


class LoginMissing(Exception):
    """`login` has never been run."""


class LoginLapsed(Exception):
    """Google no longer accepts the stored login; `login` must be run again."""


class AccountUnavailable(Exception):
    """The Subscriptions can't be read right now for any other reason; worth retrying later."""


@dataclass(frozen=True)
class Subscription:
    channel_id: str
    display_name: str
    avatar_url: str | None
    description: str


class Account(Protocol):
    def subscriptions(self) -> list[Subscription]:
        """All current Subscriptions.

        Raises LoginMissing, LoginLapsed or AccountUnavailable when they can't be read.
        """
        ...


class GoogleAccount:
    """Reads the Subscriptions over the YouTube Data API with the stored refresh token."""

    def __init__(
        self, state: StateStore, http: httpx.Client, *, secrets_dir: Path = SECRETS_DIR
    ) -> None:
        self._state = state
        self._http = http
        self._secrets_dir = secrets_dir

    def subscriptions(self) -> list[Subscription]:
        refresh_token = self._state.refresh_token()
        if refresh_token is None:
            raise LoginMissing
        access_token = self._refresh(refresh_token)
        subscriptions: list[Subscription] = []
        params: dict[str, str | int] = {"mine": "true", "part": "snippet", "maxResults": PAGE_SIZE}
        while True:
            status, body = self._send(
                self._http.build_request(
                    "GET",
                    SUBSCRIPTIONS_URL,
                    params=params,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            )
            if status != httpx.codes.OK:
                raise AccountUnavailable(
                    f"Google refused to list the Subscriptions: HTTP {status}: {_api_error(body)}"
                )
            try:
                subscriptions.extend(_subscription(item) for item in body["items"])
                next_page_token = body.get("nextPageToken")
            except (KeyError, TypeError, AttributeError) as error:
                raise AccountUnavailable(
                    f"unexpected Subscriptions page from Google: {body}"
                ) from error
            if not next_page_token:
                return subscriptions
            params["pageToken"] = str(next_page_token)

    def _refresh(self, refresh_token: str) -> str:
        """Trade the refresh token for a fresh access token."""
        # Read on use, so the daemon runs (and asks for `login`) before the secrets are in place.
        try:
            credentials = read_client_credentials(self._secrets_dir)
        except CredentialsUnavailable as error:
            raise AccountUnavailable(str(error)) from error
        _, body = self._send(
            self._http.build_request(
                "POST",
                TOKEN_URL,
                data={
                    "client_id": credentials.client_id,
                    "client_secret": credentials.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        )
        match body.get("error"):
            case None:
                access_token = body.get("access_token")
                if not isinstance(access_token, str) or not access_token:
                    raise AccountUnavailable("Google's token response contains no access token")
                return access_token
            case "invalid_grant":
                raise LoginLapsed
            case _:
                raise AccountUnavailable(
                    f"Google refused to refresh the login: {describe_oauth_error(body)}"
                )

    def _send(self, request: httpx.Request) -> tuple[int, dict[str, Any]]:
        try:
            return send(self._http, request)
        except UnexpectedResponse as error:
            raise AccountUnavailable(str(error)) from error


def _api_error(body: dict[str, Any]) -> str:
    error = body.get("error")
    return str(error.get("message", error)) if isinstance(error, dict) else str(body)


def _subscription(item: dict[str, Any]) -> Subscription:
    snippet = item["snippet"]
    thumbnails: dict[str, dict[str, str]] = snippet.get("thumbnails", {})
    largest = next(
        (thumbnails[size] for size in ("high", "medium", "default") if size in thumbnails), {}
    )
    return Subscription(
        channel_id=str(snippet["resourceId"]["channelId"]),
        display_name=str(snippet["title"]),
        avatar_url=largest.get("url"),
        description=str(snippet.get("description", "")),
    )
