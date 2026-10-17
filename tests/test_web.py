# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

import logging
import re
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from youtube_helper.channels import Channel, ChannelUnavailable, NotAChannel
from youtube_helper.notifications import EmailSettings, MailError, Report
from youtube_helper.state import (
    AllDownloadsFailed,
    CycleOutcome,
    LowSpace,
    StateStore,
)
from youtube_helper.upload_feed import Upload
from youtube_helper.web import create_app

START = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
GIB = 1024**3

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


class FakeMailer:
    """Records the emails sent, or fails with the error set on it."""

    def __init__(self) -> None:
        self.sent: list[tuple[EmailSettings, str | None, EmailMessage]] = []
        self.error: MailError | None = None

    def send(self, settings: EmailSettings, password: str | None, message: EmailMessage) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append((settings, password, message))


SECRET = b"the deployment's secret"


@pytest.fixture
def state(state_dir: Path) -> Iterator[StateStore]:
    with closing(StateStore.open(state_dir, secret_key=SECRET)) as store:
        yield store


@pytest.fixture
def mailer() -> FakeMailer:
    return FakeMailer()


@pytest.fixture
def client(
    state: StateStore, resolver: FakeResolver, clock: FakeClock, mailer: FakeMailer
) -> TestClient:
    app = create_app(
        state, resolver, timezone=ZoneInfo("Europe/Berlin"), clock=clock, mailer=mailer
    )
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


def status(client: TestClient) -> str:
    """The status fragment the page polls for, as htmx asks for it."""
    response = client.get("/status", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<html" not in response.text
    return unescape(response.text)


def subscription_on(html: str, name: str) -> str:
    """The text listing the Subscription called `name`, without markup."""
    section = html.split('id="subscriptions"', 1)[1]
    listing = section.split(f'<span class="channel-name">{name}</span>', 1)[1]
    listing = listing.split('<li class="channel">', 1)[0]
    return " ".join(re.sub(r"<[^>]+>", "", listing).split())


LAST_CYCLE = CycleOutcome(started=START, finished=START + timedelta(minutes=3), downloaded=2)


def test_page_polls_the_status_every_30_seconds(client: TestClient) -> None:
    html = page(client)

    assert 'hx-get="/status"' in html
    assert 'hx-trigger="every 30s"' in html


def test_status_before_the_first_cycle_says_so(client: TestClient) -> None:
    html = status(client)

    assert "No cycle has run yet" in html


def test_status_shows_the_last_cycle_in_local_time(client: TestClient, state: StateStore) -> None:
    state.record_cycle(LAST_CYCLE)

    html = status(client)

    assert "Last cycle: 2026-10-16 14:00 to 2026-10-16 14:03, 2 New uploads downloaded" in html
    assert "Nothing needs your attention" in html
    assert "Last cycle: 2026-10-16 14:00" in page(client)


def test_status_says_when_yt_dlp_is_likely_outdated(client: TestClient, state: StateStore) -> None:
    state.record_cycle(
        replace(LAST_CYCLE, all_downloads_failed=AllDownloadsFailed(3, "HTTP Error 403", 0))
    )

    html = status(client)

    assert (
        "All downloads failing (3 in the last cycle that tried any, last error: HTTP Error 403):"
        " yt-dlp is likely outdated, pull the latest image" in html
    )
    assert "bot check" not in html
    assert "Nothing needs your attention" not in html


def test_status_gives_the_po_token_hint_when_youtube_asked_for_bot_checks(
    client: TestClient, state: StateStore
) -> None:
    state.record_cycle(
        replace(LAST_CYCLE, all_downloads_failed=AllDownloadsFailed(3, "Sign in to confirm", 2))
    )

    html = status(client)

    assert "yt-dlp is likely outdated, pull the latest image" in html
    assert (
        "YouTube asked 2 of them for a bot check. If this keeps happening, a PO-token provider"
        " is needed" in html
    )


def test_status_warns_about_low_free_space(client: TestClient, state: StateStore) -> None:
    state.record_cycle(replace(LAST_CYCLE, low_space=LowSpace(free=9 * GIB, threshold=10 * GIB)))

    html = status(client)

    assert (
        "Only 9.0 GiB free in the Media library, less than 10.0 GiB: downloads are paused" in html
    )


def test_status_names_an_unwritable_media_library(client: TestClient, state: StateStore) -> None:
    state.record_cycle(
        replace(
            LAST_CYCLE, unwritable="Media library /media/youtube is not writable: Permission denied"
        )
    )

    html = status(client)

    assert "Media library /media/youtube is not writable: Permission denied" in html
    assert "cycles are skipped until it is" in html


def test_status_warns_while_notifications_cannot_be_sent(
    client: TestClient, state: StateStore
) -> None:
    state.record_cycle(LAST_CYCLE)
    state.notification_failed("Email can't be sent: [Errno 111] Connection refused", START)

    html = status(client)

    assert "Email can't be sent: [Errno 111] Connection refused" in html
    assert "since 2026-10-16 14:00" in html
    assert "Nothing needs your attention" not in html

    state.notification_delivered(Report())

    assert "Email can't be sent" not in status(client)


def failing(state: StateStore, video_id: str, title: str, error: str, at: datetime) -> None:
    """Record a New upload of First Channel whose download failed at `at`."""
    state.record_uploads("UC1", [Upload(video_id, title, START + timedelta(minutes=5))])
    state.record_failure(video_id, error, at)


def test_each_subscription_shows_its_failing_videos(
    client: TestClient, state: StateStore, clock: FakeClock
) -> None:
    add(client, "UC1")
    add(client, "UC2")
    failing(state, "vid00000001", "Flaky", "HTTP Error 503", START + timedelta(hours=1))
    failing(state, "vid00000002", "Doomed", "HTTP Error 404", START + timedelta(hours=1))
    state.mark_failed("vid00000002", START + timedelta(hours=25))
    state.record_uploads("UC1", [Upload("vid00000003", "Private", START + timedelta(minutes=6))])
    state.mark_skipped("vid00000003", "Private video", START + timedelta(hours=2))
    state.record_uploads("UC1", [Upload("vid00000004", "A Short", START + timedelta(minutes=7))])
    state.mark_excluded("vid00000004")
    clock.now = START + timedelta(days=2)

    first = subscription_on(status(client), "First Channel")
    second = subscription_on(status(client), "Second Channel")

    assert "Retrying Flaky: HTTP Error 503 (failing since 2026-10-16 15:00)" in first
    assert "Gave up on Doomed on 2026-10-17 15:00 after 24 hours: HTTP Error 404" in first
    assert "Skipped Private for good on 2026-10-16 16:00: Private video" in first
    assert "A Short" not in first
    assert "Retrying" not in second
    assert "Retrying Flaky" in subscription_on(page(client), "First Channel")


def test_videos_given_up_more_than_30_days_ago_are_no_longer_shown(
    client: TestClient, state: StateStore, clock: FakeClock
) -> None:
    add(client, "UC1")
    failing(state, "vid00000001", "Doomed", "HTTP Error 404", START)
    state.mark_failed("vid00000001", START + timedelta(hours=24))
    clock.now = START + timedelta(days=31, hours=1)

    assert "Doomed" not in subscription_on(status(client), "First Channel")


def test_subscription_whose_feed_could_not_be_read_says_so(
    client: TestClient, state: StateStore
) -> None:
    add(client, "UC1")
    add(client, "UC2")
    state.record_cycle(replace(LAST_CYCLE, feed_failures={"UC2": "HTTP 500"}))

    html = status(client)

    assert "feed" not in subscription_on(html, "First Channel")
    assert (
        "Couldn't read the channel's feed in the last cycle (HTTP 500); trying again next cycle"
        in subscription_on(html, "Second Channel")
    )


def notifications_page(client: TestClient) -> str:
    response = client.get("/notifications")
    assert response.status_code == 200
    return unescape(response.text)


def field(html: str, name: str) -> str:
    """The <input>, <select> or <textarea> tag (with a textarea's text) for the field `name`."""
    match = re.search(rf'<(input|select|textarea)\b[^>]*\bname="{name}"[^>]*>', html)
    assert match is not None, f"no field {name}"
    if match[1] == "textarea":
        return html[match.start() : html.index("</textarea>", match.end())]
    if match[1] == "select":
        return html[match.start() : html.index("</select>", match.end())]
    return match[0]


def selected(html: str, name: str) -> str:
    """The value of the option selected in the <select> called `name`."""
    match = re.search(r'<option value="([^"]*)"[^>]*\bselected\b', field(html, name))
    assert match is not None, f"nothing selected in {name}"
    return match[1]


def test_notifications_page_is_linked_from_the_header(client: TestClient) -> None:
    assert 'href="/notifications"' in page(client)
    assert 'href="/"' in notifications_page(client)


def test_notifications_are_off_by_default_with_both_kinds_ticked(client: TestClient) -> None:
    html = notifications_page(client)

    assert "checked" not in field(html, "enabled")
    assert "checked" in field(html, "new_videos")
    assert "checked" in field(html, "warnings")
    assert 'value="587"' in field(html, "port")
    assert selected(html, "security") == "starttls"
    assert "No password set" in html


SETTINGS_FORM = {
    "enabled": "on",
    "warnings": "on",
    "host": "smtp.example.org",
    "port": "465",
    "security": "tls",
    "username": "owner",
    "password": "hunter2-correct-horse",
    "from_address": "youtube-helper@example.org",
    "recipients": "owner@example.org\r\n partner@example.org \r\n",
}


def save_email(client: TestClient, **changes: str | None) -> str:
    """Save the settings form with `changes`; a change to None leaves that field out."""
    form = {name: value for name, value in (SETTINGS_FORM | changes).items() if value is not None}
    response = client.post("/notifications", data=form)
    assert response.status_code == 200
    return unescape(response.text)


def test_saved_settings_are_shown_without_the_password(
    client: TestClient, state: StateStore
) -> None:
    html = save_email(client)

    assert "Saved" in html
    shown = notifications_page(client)
    assert "checked" in field(shown, "enabled")
    assert "checked" not in field(shown, "new_videos")
    assert "checked" in field(shown, "warnings")
    assert 'value="smtp.example.org"' in field(shown, "host")
    assert 'value="465"' in field(shown, "port")
    assert selected(shown, "security") == "tls"
    assert 'value="owner"' in field(shown, "username")
    assert "owner@example.org\npartner@example.org" in field(shown, "recipients")
    assert "A password is set" in shown
    assert "hunter2-correct-horse" not in html
    assert "hunter2-correct-horse" not in shown
    saved = state.email_settings()
    assert saved.settings.recipients == ("owner@example.org", "partner@example.org")
    assert saved.password == "hunter2-correct-horse"


def test_saving_with_a_blank_password_keeps_the_stored_one(
    client: TestClient, state: StateStore
) -> None:
    save_email(client)

    save_email(client, password="", warnings=None)

    assert not state.email_settings().settings.warnings
    assert state.email_settings().password == "hunter2-correct-horse"


def test_saving_with_htmx_sends_only_the_form(client: TestClient) -> None:
    response = client.post("/notifications", data=SETTINGS_FORM, headers={"HX-Request": "true"})

    assert "Saved" in response.text
    assert "<html" not in response.text
    assert 'id="email-settings"' in response.text


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        (
            {"security": "none", "port": "25", "password": ""},
            "Security “none” would send the username and password unencrypted",
        ),
        (
            {"security": "none", "port": "25", "username": ""},
            "Security “none” would send the username and password unencrypted",
        ),
        ({"from_address": "youtube-helper"}, "From address youtube-helper is not an email address"),
        (
            {"recipients": "owner@example.org\nowner at example.org"},
            "Recipient owner at example.org is not an email address",
        ),
        ({"recipients": "a@example.org, b@example.org"}, "Recipient a@example.org, b@example.org"),
        ({"port": "smtp"}, "Port smtp is not a port number (1-65535)"),
        ({"port": "70000"}, "Port 70000 is not a port number (1-65535)"),
        ({"security": "ssl"}, "Choose a security setting"),
        ({"username": ""}, "A password is only used with a username"),
        ({"host": ""}, "Email Notifications need an SMTP host, a From address and a recipient"),
        (
            {"recipients": ""},
            "Email Notifications need an SMTP host, a From address and a recipient",
        ),
    ],
)
def test_invalid_settings_are_rejected_and_nothing_is_saved(
    client: TestClient, state: StateStore, changes: dict[str, str], error: str
) -> None:
    html = save_email(client, **changes)

    assert error in html
    assert "Saved" not in html
    assert "hunter2-correct-horse" not in html
    assert state.email_settings().settings == EmailSettings()


def test_rejected_settings_are_shown_as_entered_to_correct_them(client: TestClient) -> None:
    html = save_email(client, port="smtp", recipients="owner@example.org\nnobody")

    assert 'value="smtp"' in field(html, "port")
    assert "owner@example.org\nnobody" in field(html, "recipients")
    assert 'value="smtp.example.org"' in field(html, "host")


def test_switched_off_notifications_may_be_saved_incomplete(
    client: TestClient, state: StateStore
) -> None:
    save_email(client, enabled=None, host="", recipients="")

    assert state.email_settings().settings.from_address == "youtube-helper@example.org"


@pytest.mark.parametrize(("security", "port"), [("starttls", 587), ("tls", 465), ("none", 25)])
def test_a_blank_port_is_the_security_settings_usual_one(
    client: TestClient, state: StateStore, security: str, port: int
) -> None:
    save_email(client, security=security, port="", username="", password="")

    assert state.email_settings().settings.port == port


def app_on(state: StateStore, mailer: FakeMailer) -> TestClient:
    return TestClient(
        create_app(state, FakeResolver(), timezone=ZoneInfo("Europe/Berlin"), mailer=mailer)
    )


def test_a_password_the_current_key_cannot_decrypt_is_asked_for_again(
    client: TestClient, state_dir: Path, mailer: FakeMailer
) -> None:
    save_email(client)

    with closing(StateStore.open(state_dir, secret_key=b"a rotated secret")) as rotated:
        html = notifications_page(app_on(rotated, mailer))

    assert "The stored password can't be decrypted" in html
    assert "please enter it again" in html
    assert "disabled" not in field(html, "password")


def test_without_a_secret_key_the_password_field_is_disabled_with_a_hint(
    client: TestClient, state_dir: Path, mailer: FakeMailer
) -> None:
    with closing(StateStore.open(state_dir)) as keyless:
        without_key = app_on(keyless, mailer)
        html = notifications_page(without_key)
        rejected = save_email(without_key)
        saved = save_email(without_key, password=None)

        assert "disabled" in field(html, "password")
        assert "Set SECRET_KEY_FILE to store a password" in re.sub(r"<[^>]+>", "", html)
        assert "SECRET_KEY_FILE" in rejected
        assert "Saved" not in rejected
        assert "Saved" in saved
        assert keyless.email_settings().settings.host == "smtp.example.org"


def send_test_email(client: TestClient) -> str:
    response = client.post("/notifications/test", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<html" not in response.text
    return unescape(response.text)


def test_test_email_is_sent_right_away_with_the_saved_settings(
    client: TestClient, state: StateStore, mailer: FakeMailer
) -> None:
    save_email(client, enabled=None)

    html = send_test_email(client)

    assert "Test email sent to owner@example.org, partner@example.org" in html
    [(settings, password, message)] = mailer.sent
    assert settings == state.email_settings().settings
    assert password == "hunter2-correct-horse"
    assert message["From"] == "youtube-helper@example.org"
    assert message["To"] == "owner@example.org, partner@example.org"
    assert "test" in message["Subject"].lower()


def test_test_email_shows_the_smtp_error(client: TestClient, mailer: FakeMailer) -> None:
    save_email(client)
    mailer.error = MailError("(535, b'5.7.8 Authentication failed')")

    html = send_test_email(client)

    assert "Couldn't send the test email: (535, b'5.7.8 Authentication failed')" in html
    assert "Test email sent" not in html


def test_test_email_without_a_server_and_recipients_says_what_is_missing(
    client: TestClient, mailer: FakeMailer
) -> None:
    html = send_test_email(client)

    assert "Save an SMTP host, a From address and a recipient first" in html
    assert mailer.sent == []


def test_test_email_with_an_undecryptable_password_asks_for_it_again(
    client: TestClient, state_dir: Path, mailer: FakeMailer
) -> None:
    save_email(client)

    with closing(StateStore.open(state_dir, secret_key=b"a rotated secret")) as rotated:
        html = send_test_email(app_on(rotated, mailer))

    assert "The stored password can't be decrypted" in html
    assert mailer.sent == []


def test_test_email_without_htmx_shows_the_whole_page(client: TestClient) -> None:
    save_email(client)

    response = client.post("/notifications/test")

    assert "<html" in response.text
    assert "Test email sent" in response.text


def test_test_email_after_the_secret_key_was_removed_asks_for_it(
    client: TestClient, state_dir: Path, mailer: FakeMailer
) -> None:
    save_email(client)

    with closing(StateStore.open(state_dir)) as keyless:
        html = send_test_email(app_on(keyless, mailer))

    assert "Set SECRET_KEY_FILE to use the stored password" in html
    assert "enter it again" not in html
    assert mailer.sent == []
