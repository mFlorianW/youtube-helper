# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

import logging
from datetime import UTC, datetime, timedelta
from html import unescape
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from youtube_helper.channels import Channel, ChannelUnavailable, NotAChannel
from youtube_helper.state import StateStore
from youtube_helper.web import create_app

START = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

FIRST = Channel("UC1", "First Channel", "https://yt3.ggpht.com/UC1", "About First")
SECOND = Channel("UC2", "Second Channel", None, "")


class FakeResolver:
    """Resolves the references set on it, or raises the error set for them."""

    def __init__(self) -> None:
        self.references: dict[str, Channel | Exception] = {}
        self.resolved: list[str] = []

    def knows(self, channel: Channel, *references: str) -> None:
        for reference in (channel.channel_id, *references):
            self.references[reference] = channel

    def resolve(self, reference: str) -> Channel:
        self.resolved.append(reference)
        channel = self.references.get(reference, NotAChannel(f"{reference} leads nowhere"))
        if isinstance(channel, Exception):
            raise channel
        return channel


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def resolver() -> FakeResolver:
    resolver = FakeResolver()
    resolver.knows(FIRST, "@first", "https://www.youtube.com/watch?v=vid00000001")
    resolver.knows(SECOND, "@second")
    return resolver


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(state: StateStore, resolver: FakeResolver, clock: FakeClock) -> TestClient:
    app = create_app(state, resolver, timezone=ZoneInfo("Europe/Berlin"), clock=clock)
    return TestClient(app)


@pytest.fixture(autouse=True)
def info_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)


def page(client: TestClient) -> str:
    response = client.get("/")
    assert response.status_code == 200
    return unescape(response.text)


def subscriptions_on(html: str) -> list[str]:
    """The names of the channels listed as Subscriptions, in page order."""
    section = html.split('id="subscriptions"', 1)[1]
    return [
        part.split("</span>", 1)[0] for part in section.split('<span class="channel-name">')[1:]
    ]


def preview(client: TestClient, reference: str) -> str:
    response = client.post("/preview", data={"channel": reference})
    assert response.status_code == 200
    return unescape(response.text)


def add(client: TestClient, channel_id: str) -> str:
    response = client.post("/subscriptions", data={"channel_id": channel_id})
    assert response.status_code == 200
    return unescape(response.text)


def remove(client: TestClient, channel_id: str) -> str:
    response = client.post(f"/subscriptions/{channel_id}/remove")
    assert response.status_code == 200
    return unescape(response.text)


def test_page_without_subscriptions_invites_to_add_one(client: TestClient) -> None:
    html = page(client)

    assert subscriptions_on(html) == []
    assert "No Subscriptions yet" in html


def test_preview_shows_the_channel_without_subscribing(client: TestClient) -> None:
    html = preview(client, "@first")

    assert "First Channel" in html
    assert 'src="https://yt3.ggpht.com/UC1"' in html
    assert subscriptions_on(page(client)) == []


def test_preview_accepts_a_video_of_the_channel(client: TestClient) -> None:
    html = preview(client, "  https://www.youtube.com/watch?v=vid00000001  ")

    assert "First Channel" in html


def test_input_that_leads_to_no_channel_is_rejected(client: TestClient) -> None:
    html = preview(client, "@nobody")

    assert "No YouTube channel found for @nobody" in html
    assert 'name="channel_id"' not in html


def test_channel_that_cannot_be_looked_up_now_asks_to_try_again(
    client: TestClient, resolver: FakeResolver
) -> None:
    resolver.references["@first"] = ChannelUnavailable("HTTP Error 503")

    html = preview(client, "@first")

    assert "Can't reach YouTube right now (HTTP Error 503); please try again" in html


def test_confirming_subscribes_from_now(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    preview(client, "@first")

    html = add(client, "UC1")

    assert subscriptions_on(html) == ["First Channel"]
    assert subscriptions_on(page(client)) == ["First Channel"]
    assert "Tracking since 2026-10-16 14:00" in page(client)
    assert "Started tracking First Channel (UC1) from 2026-10-16 12:00 UTC" in caplog.text


def test_confirming_looks_the_channel_up_when_its_preview_is_gone(
    client: TestClient, resolver: FakeResolver
) -> None:
    add(client, "UC2")

    assert subscriptions_on(page(client)) == ["Second Channel"]
    assert resolver.resolved == ["UC2"]


def test_preview_of_an_active_subscription_says_so_and_adding_again_changes_nothing(
    client: TestClient, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    add(client, "UC1")
    clock.now += timedelta(hours=1)
    caplog.clear()

    html = preview(client, "@first")
    add(client, "UC1")

    assert "Already subscribed" in html
    assert subscriptions_on(page(client)) == ["First Channel"]
    assert "Tracking since 2026-10-16 14:00" in page(client)
    assert "tracking" not in caplog.text


def test_removing_stops_tracking_and_keeps_the_files(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    add(client, "UC1")
    add(client, "UC2")

    html = remove(client, "UC1")

    assert subscriptions_on(html) == ["Second Channel"]
    assert subscriptions_on(page(client)) == ["Second Channel"]
    assert "Stopped tracking First Channel (UC1): unsubscribed; its files are kept" in caplog.text


def test_adding_a_removed_channel_again_restarts_tracking_from_then(
    client: TestClient, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    add(client, "UC1")
    remove(client, "UC1")
    clock.now += timedelta(days=3)

    add(client, "UC1")

    assert subscriptions_on(page(client)) == ["First Channel"]
    assert "Tracking since 2026-10-19 14:00" in page(client)
    assert "Restarted tracking First Channel (UC1) from 2026-10-19 12:00 UTC" in caplog.text


def test_removing_what_is_not_subscribed_changes_nothing(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    add(client, "UC1")

    remove(client, "UC2")

    assert subscriptions_on(page(client)) == ["First Channel"]
    assert "Stopped tracking" not in caplog.text


def test_with_htmx_only_the_changed_parts_of_the_page_are_sent(client: TestClient) -> None:
    htmx = {"HX-Request": "true"}

    previewed = client.post("/preview", data={"channel": "@first"}, headers=htmx).text
    added = client.post("/subscriptions", data={"channel_id": "UC1"}, headers=htmx).text
    removed = client.post("/subscriptions/UC1/remove", headers=htmx).text

    assert "First Channel" in previewed
    assert "<html" not in previewed
    # The list, with the preview emptied out of band.
    assert subscriptions_on('id="subscriptions"' + added) == ["First Channel"]
    assert '<div id="preview" aria-live="polite" hx-swap-oob="true"></div>' in added
    assert "<html" not in added
    assert subscriptions_on('id="subscriptions"' + removed) == []
    assert "No Subscriptions yet" in removed


def test_with_htmx_a_failed_lookup_on_confirming_shows_the_error_in_the_preview(
    client: TestClient, resolver: FakeResolver
) -> None:
    resolver.references["UC1"] = ChannelUnavailable("HTTP Error 503")

    response = client.post(
        "/subscriptions", data={"channel_id": "UC1"}, headers={"HX-Request": "true"}
    )

    assert response.headers["HX-Retarget"] == "#preview"
    assert "Can't reach YouTube right now (HTTP Error 503); please try again" in unescape(
        response.text
    )
    assert "<html" not in response.text
    assert subscriptions_on(page(client)) == []


def test_page_serves_its_styles_and_scripts(client: TestClient) -> None:
    html = page(client)

    for asset in ("/static/pico.min.css", "/static/htmx.min.js"):
        assert asset in html
        assert client.get(asset).status_code == 200
