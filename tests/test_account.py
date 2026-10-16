# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from youtube_helper.account import (
    SUBSCRIPTIONS_URL,
    AccountUnavailable,
    GoogleAccount,
    LoginLapsed,
    LoginMissing,
    Subscription,
)
from youtube_helper.google import TOKEN_URL
from youtube_helper.state import StateStore

type Reply = tuple[int, dict[str, object]]

REFRESHED: Reply = (200, {"access_token": "access-token", "expires_in": 3599})


def subscription_item(channel_id: str, title: str) -> dict[str, object]:
    return {
        "snippet": {
            "title": title,
            "description": f"About {title}",
            "resourceId": {"kind": "youtube#channel", "channelId": channel_id},
            "thumbnails": {
                "default": {"url": f"https://yt3.ggpht.com/{channel_id}=s88"},
                "high": {"url": f"https://yt3.ggpht.com/{channel_id}=s800"},
            },
        }
    }


def page(*items: dict[str, object], next_page_token: str | None = None) -> Reply:
    body: dict[str, object] = {"kind": "youtube#subscriptionListResponse", "items": list(items)}
    if next_page_token is not None:
        body["nextPageToken"] = next_page_token
    return 200, body


class FakeGoogle:
    """Answers the token refresh, then each subscriptions.list call with the next page."""

    def __init__(self, token_response: Reply, pages: list[Reply] | None = None) -> None:
        self.token_response = token_response
        self.pages = list(pages or [])
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            status, body = self.token_response
        elif str(request.url.copy_with(query=None)) == SUBSCRIPTIONS_URL:
            status, body = self.pages.pop(0)
        else:
            status, body = 404, {}
        return httpx.Response(status, json=body)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def subscription_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url) != TOKEN_URL]


@pytest.fixture
def logged_in(state: StateStore) -> StateStore:
    state.save_refresh_token("refresh-token")
    return state


@pytest.fixture
def account(secrets_dir: Path, logged_in: StateStore) -> Callable[[FakeGoogle], GoogleAccount]:
    return lambda google: GoogleAccount(logged_in, google.client(), secrets_dir=secrets_dir)


def test_refreshes_the_access_token_and_reads_the_subscriptions(
    account: Callable[[FakeGoogle], GoogleAccount],
) -> None:
    google = FakeGoogle(REFRESHED, [page(subscription_item("UC1", "First Channel"))])

    subscriptions = account(google).subscriptions()

    assert subscriptions == [
        Subscription(
            channel_id="UC1",
            display_name="First Channel",
            avatar_url="https://yt3.ggpht.com/UC1=s800",
            description="About First Channel",
        )
    ]
    refresh = google.requests[0]
    assert str(refresh.url) == TOKEN_URL
    assert parse_qs(refresh.content.decode()) == {
        "client_id": ["client-id.apps.googleusercontent.com"],
        "client_secret": ["client-secret"],
        "refresh_token": ["refresh-token"],
        "grant_type": ["refresh_token"],
    }
    [listing] = google.subscription_requests()
    assert listing.headers["Authorization"] == "Bearer access-token"
    assert dict(listing.url.params) == {"mine": "true", "part": "snippet", "maxResults": "50"}


def test_reads_every_page_of_subscriptions(account: Callable[[FakeGoogle], GoogleAccount]) -> None:
    google = FakeGoogle(
        REFRESHED,
        [
            page(subscription_item("UC1", "First"), next_page_token="page-2"),
            page(subscription_item("UC2", "Second"), next_page_token="page-3"),
            page(subscription_item("UC3", "Third")),
        ],
    )

    subscriptions = account(google).subscriptions()

    assert [s.channel_id for s in subscriptions] == ["UC1", "UC2", "UC3"]
    page_tokens = [r.url.params.get("pageToken") for r in google.subscription_requests()]
    assert page_tokens == [None, "page-2", "page-3"]


def test_refresh_rejected_with_invalid_grant_means_the_login_lapsed(
    account: Callable[[FakeGoogle], GoogleAccount],
) -> None:
    google = FakeGoogle(
        (400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    )

    with pytest.raises(LoginLapsed):
        account(google).subscriptions()

    assert google.subscription_requests() == []


def test_without_a_stored_login_the_login_is_missing(state: StateStore, tmp_path: Path) -> None:
    google = FakeGoogle(REFRESHED)

    # Not logged in, the secrets aren't needed yet.
    with pytest.raises(LoginMissing):
        GoogleAccount(state, google.client(), secrets_dir=tmp_path / "no-secrets").subscriptions()

    assert google.requests == []


@pytest.mark.parametrize(
    ("token_response", "pages", "reason"),
    [
        pytest.param(
            (
                401,
                {"error": "invalid_client", "error_description": "The OAuth client was deleted."},
            ),
            [],
            "invalid_client (The OAuth client was deleted.)",
            id="client rejected",
        ),
        pytest.param(
            REFRESHED,
            [(403, {"error": {"code": 403, "message": "The request cannot be completed."}})],
            "HTTP 403: The request cannot be completed.",
            id="quota exceeded",
        ),
        pytest.param(REFRESHED, [(200, {"unexpected": True})], "unexpected", id="malformed page"),
    ],
)
def test_other_google_failures_make_the_account_unavailable(
    account: Callable[[FakeGoogle], GoogleAccount],
    token_response: Reply,
    pages: list[Reply],
    reason: str,
) -> None:
    google = FakeGoogle(token_response, pages)

    with pytest.raises(AccountUnavailable, match=re.escape(reason)):
        account(google).subscriptions()


def test_unreachable_google_makes_the_account_unavailable(
    secrets_dir: Path, logged_in: StateStore
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known", request=request)

    http = httpx.Client(transport=httpx.MockTransport(refuse))

    with pytest.raises(AccountUnavailable, match="Name or service not known"):
        GoogleAccount(logged_in, http, secrets_dir=secrets_dir).subscriptions()


def test_missing_secret_makes_the_account_unavailable_naming_it(
    secrets_dir: Path, logged_in: StateStore
) -> None:
    (secrets_dir / "google_client_secret").unlink()
    google = FakeGoogle(REFRESHED)

    with pytest.raises(
        AccountUnavailable, match=re.escape(str(secrets_dir / "google_client_secret"))
    ):
        GoogleAccount(logged_in, google.client(), secrets_dir=secrets_dir).subscriptions()

    assert google.requests == []
