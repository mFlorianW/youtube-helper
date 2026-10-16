# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

import logging
import os
import re
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from youtube_helper.account import AccountUnavailable, LoginLapsed, LoginMissing, Subscription
from youtube_helper.cycle import Cycle
from youtube_helper.media_library import MediaLibraryUnwritable
from youtube_helper.state import StateStore

START = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

FIRST = Subscription("UC1", "First Channel", "https://yt3.ggpht.com/UC1", "About First")
SECOND = Subscription("UC2", "Second Channel", "https://yt3.ggpht.com/UC2", "About Second")
THIRD = Subscription("UC3", "Third Channel", None, "")


class FakeAccount:
    """Answers with the Subscriptions set on it, or raises the error set on it."""

    def __init__(self, *subscriptions: Subscription, error: Exception | None = None) -> None:
        self.current: list[Subscription] | Exception = error or list(subscriptions)

    def subscriptions(self) -> list[Subscription]:
        if isinstance(self.current, Exception):
            raise self.current
        return list(self.current)


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta = timedelta(minutes=30)) -> None:
        self.now += delta


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(autouse=True)
def info_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)


def run_cycle(
    media_library: Path, state: StateStore, account: FakeAccount, clock: FakeClock | None = None
) -> None:
    Cycle(media_library, state, account, clock or FakeClock()).run()


def test_cycle_with_writable_media_library_is_logged(
    media_library: Path, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    run_cycle(media_library, state, FakeAccount())

    assert "Cycle finished" in caplog.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to read-only directories")
def test_cycle_with_read_only_media_library_fails_naming_the_directory(
    media_library: Path, state: StateStore
) -> None:
    media_library.chmod(0o555)
    try:
        with pytest.raises(MediaLibraryUnwritable, match=re.escape(str(media_library))):
            run_cycle(media_library, state, FakeAccount())
    finally:
        media_library.chmod(0o755)


def test_cycle_with_missing_media_library_fails_naming_the_directory(
    tmp_path: Path, state: StateStore
) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(MediaLibraryUnwritable, match=re.escape(str(missing))):
        run_cycle(missing, state, FakeAccount())


def test_cycle_after_login_does_not_ask_for_login(
    media_library: Path, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    run_cycle(media_library, state, FakeAccount(FIRST))

    assert "Not logged in" not in caplog.text


def test_first_start_tracks_every_subscription_from_now(
    media_library: Path, state: StateStore, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    run_cycle(media_library, state, FakeAccount(FIRST, SECOND), clock)

    assert "Started tracking First Channel (UC1) from 2026-10-16 12:00 UTC" in caplog.text
    assert "Started tracking Second Channel (UC2) from 2026-10-16 12:00 UTC" in caplog.text
    assert "Active Subscriptions (2): First Channel, Second Channel" in caplog.text


def test_new_subscription_is_tracked_from_when_it_is_first_seen(
    media_library: Path, state_dir: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    account = FakeAccount(FIRST)
    with closing(StateStore.open(state_dir)) as state:
        run_cycle(media_library, state, account, clock)
    caplog.clear()
    clock.advance(timedelta(hours=2))
    account.current = [FIRST, SECOND]

    # A restart in between: a new process on the same state file.
    with closing(StateStore.open(state_dir)) as state:
        run_cycle(media_library, state, account, clock)

    assert "Started tracking Second Channel (UC2) from 2026-10-16 14:00 UTC" in caplog.text
    assert "First Channel (UC1) from" not in caplog.text
    assert "Active Subscriptions (2): First Channel, Second Channel" in caplog.text


def test_unsubscribed_channel_becomes_inactive_and_its_files_stay(
    media_library: Path, state: StateStore, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    account = FakeAccount(FIRST, SECOND)
    run_cycle(media_library, state, account, clock)
    downloaded = media_library / "First Channel" / "Season 2026" / "video.mkv"
    downloaded.parent.mkdir(parents=True)
    downloaded.write_bytes(b"video")
    caplog.clear()
    clock.advance()
    account.current = [SECOND]

    run_cycle(media_library, state, account, clock)

    assert "Stopped tracking First Channel (UC1): unsubscribed" in caplog.text
    assert "Active Subscriptions (1): Second Channel" in caplog.text
    assert downloaded.read_bytes() == b"video"


def test_unsubscribed_channel_is_reported_only_once(
    media_library: Path, state: StateStore, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    account = FakeAccount(FIRST, SECOND)
    run_cycle(media_library, state, account, clock)
    account.current = [SECOND]
    run_cycle(media_library, state, account, clock)
    caplog.clear()

    run_cycle(media_library, state, account, clock)

    assert "Stopped tracking" not in caplog.text
    assert "Active Subscriptions (1): Second Channel" in caplog.text


def test_resubscribed_channel_restarts_tracking_from_now(
    media_library: Path, state: StateStore, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    account = FakeAccount(FIRST, SECOND)
    run_cycle(media_library, state, account, clock)
    clock.advance()
    account.current = [SECOND]
    run_cycle(media_library, state, account, clock)
    caplog.clear()
    clock.advance(timedelta(days=3))
    account.current = [FIRST, SECOND]

    run_cycle(media_library, state, account, clock)

    assert "Restarted tracking First Channel (UC1) from 2026-10-19 12:30 UTC" in caplog.text
    assert "Started tracking" not in caplog.text
    assert "Active Subscriptions (2): First Channel, Second Channel" in caplog.text


def test_lapsed_login_keeps_the_last_known_subscriptions_and_says_so_every_cycle(
    media_library: Path, state: StateStore, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    account = FakeAccount(FIRST, SECOND)
    run_cycle(media_library, state, account, clock)
    account.current = [SECOND]
    run_cycle(media_library, state, account, clock)
    caplog.clear()
    account.current = LoginLapsed()

    run_cycle(media_library, state, account, clock)
    run_cycle(media_library, state, account, clock)

    assert caplog.text.count("Login has lapsed, run `login` again") == 2
    assert caplog.text.count("Active Subscriptions (1): Second Channel") == 2
    assert "Stopped tracking" not in caplog.text


def test_unreadable_account_keeps_the_last_known_subscriptions(
    media_library: Path, state: StateStore, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock)
    caplog.clear()
    account.current = AccountUnavailable("can't reach oauth2.googleapis.com: timed out")

    run_cycle(media_library, state, account, clock)

    assert (
        "Can't read the Subscriptions (can't reach oauth2.googleapis.com: timed out)" in caplog.text
    )
    assert "Active Subscriptions (1): First Channel" in caplog.text
    assert "Stopped tracking" not in caplog.text


def test_missing_login_tracks_nothing(
    media_library: Path, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    run_cycle(media_library, state, FakeAccount(error=LoginMissing()))

    assert "Not logged in, run `login`" in caplog.text
    assert "tracking" not in caplog.text
    assert "Active Subscriptions" not in caplog.text


def test_resubscribed_channel_keeps_the_name_it_was_first_seen_with(
    media_library: Path, state: StateStore, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock)
    account.current = []
    run_cycle(media_library, state, account, clock)
    caplog.clear()
    account.current = [Subscription("UC1", "Renamed Channel", None, "")]

    run_cycle(media_library, state, account, clock)

    assert "Restarted tracking First Channel (UC1)" in caplog.text
    assert "Active Subscriptions (1): First Channel" in caplog.text
