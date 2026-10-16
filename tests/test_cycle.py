# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

import logging
import os
import re
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from youtube_helper.account import AccountUnavailable, LoginLapsed, LoginMissing, Subscription
from youtube_helper.artwork import ArtworkUnavailable
from youtube_helper.cycle import Cycle
from youtube_helper.media_library import MediaLibraryUnwritable
from youtube_helper.state import StateStore
from youtube_helper.upload_feed import FeedUnavailable, Upload
from youtube_helper.video_fetcher import FetchFailed, NotAiredYet, NotRegularVideo, VideoMetadata

START = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
GIB = 1024**3
UTC_ZONE = ZoneInfo("UTC")

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


class FakeDisk:
    """Reports the free space set on it for the Media library."""

    def __init__(self, free: int = 100 * GIB) -> None:
        self.free = free

    def __call__(self, path: Path) -> int:
        return self.free


class FakeFeed:
    """Answers with the uploads set per channel, or raises the error set for it."""

    def __init__(self) -> None:
        self.channels: dict[str, list[Upload] | Exception] = {}

    def publish(self, channel_id: str, upload: Upload) -> None:
        uploads = self.channels.setdefault(channel_id, [])
        assert isinstance(uploads, list)
        uploads.insert(0, upload)

    def uploads(self, channel_id: str) -> list[Upload]:
        uploads = self.channels.get(channel_id, [])
        if isinstance(uploads, Exception):
            raise uploads
        return list(uploads)


class FakeFetcher:
    """Writes a video and thumbnail for each fetch, or raises the error set for the video."""

    def __init__(self) -> None:
        self.fetched: list[str] = []
        self.errors: dict[str, Exception] = {}
        self.library_during_fetch: list[Path] = []
        self.media_library: Path | None = None
        self.disk: FakeDisk | None = None

    def fetch(self, video_id: str, base: Path) -> VideoMetadata:
        self.fetched.append(video_id)
        if self.disk is not None:
            self.disk.free -= GIB
        if self.media_library is not None:
            self.library_during_fetch = media_files(self.media_library)
        if video_id in self.errors:
            base.with_name(base.name + ".mkv.part").write_bytes(b"partial")
            raise self.errors[video_id]
        base.with_name(base.name + ".mkv").write_bytes(f"video {video_id}".encode())
        base.with_name(base.name + "-thumb.jpg").write_bytes(f"thumb {video_id}".encode())
        return VideoMetadata(description=f"Description of {video_id}")


class FakeArtwork:
    """Answers each image URL with made-up image data, or raises the error set for it."""

    def __init__(self) -> None:
        self.errors: dict[str, Exception] = {}

    def download(self, url: str) -> bytes:
        if url in self.errors:
            raise self.errors[url]
        return f"image {url}".encode()


def media_files(media_library: Path) -> list[Path]:
    """Every file a media server would see: hidden directories are ignored."""
    return sorted(
        path.relative_to(media_library)
        for path in media_library.rglob("*")
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(media_library).parts)
    )


def videos(media_library: Path) -> list[Path]:
    """Every video file in the Media library."""
    return [path for path in media_files(media_library) if path.suffix == ".mkv"]


def nfo(path: Path) -> tuple[str, dict[str, str]]:
    """An NFO file's root element and the text of each child, `uniqueid` with its type."""
    root = ET.parse(path).getroot()
    fields = {}
    for child in root:
        key = f"uniqueid {child.get('type')}" if child.tag == "uniqueid" else child.tag
        fields[key] = child.text or ""
    return root.tag, fields


def episode_numbers(media_library: Path) -> dict[str, tuple[str, str]]:
    """Each filed video's ID with the season and episode number from its NFO."""
    numbers = {}
    for path in media_library.rglob("*.nfo"):
        tag, fields = nfo(path)
        if tag == "episodedetails":
            numbers[fields["uniqueid youtube"]] = (fields["season"], fields["episode"])
    return numbers


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def feed() -> FakeFeed:
    return FakeFeed()


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher()


@pytest.fixture
def artwork() -> FakeArtwork:
    return FakeArtwork()


@pytest.fixture(autouse=True)
def info_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)


def run_cycle(
    media_library: Path,
    state: StateStore,
    account: FakeAccount,
    clock: FakeClock | None = None,
    *,
    feed: FakeFeed | None = None,
    fetcher: FakeFetcher | None = None,
    artwork: FakeArtwork | None = None,
    disk: FakeDisk | None = None,
    timezone: ZoneInfo = UTC_ZONE,
) -> None:
    Cycle(
        media_library,
        state,
        account=account,
        feed=feed or FakeFeed(),
        fetcher=fetcher or FakeFetcher(),
        artwork=artwork or FakeArtwork(),
        timezone=timezone,
        clock=clock or FakeClock(),
        free_space=disk or FakeDisk(),
    ).run()


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


def test_new_upload_lands_in_its_channel_folder_named_by_release_time_in_local_time(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    released = START + timedelta(minutes=10)
    feed.publish("UC1", Upload("vid00000001", "Part 1: The Start", released))
    clock.advance()

    run_cycle(
        media_library,
        state,
        account,
        clock,
        feed=feed,
        fetcher=fetcher,
        timezone=ZoneInfo("Europe/Berlin"),
    )

    # 12:10 UTC is 14:10 in Berlin (CEST).
    basename = "First Channel - 2026-10-16 14-10 - Part 1 - The Start [vid00000001]"
    assert media_files(media_library) == [
        Path("First Channel", "Season 2026", f"{basename}-thumb.jpg"),
        Path("First Channel", "Season 2026", f"{basename}.mkv"),
        Path("First Channel", "Season 2026", f"{basename}.nfo"),
        Path("First Channel", "poster.jpg"),
        Path("First Channel", "tvshow.nfo"),
    ]
    video = media_library / "First Channel" / "Season 2026" / f"{basename}.mkv"
    assert video.read_bytes() == b"video vid00000001"


def test_uploads_released_before_tracking_start_are_never_downloaded(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    feed.publish("UC1", Upload("old00000001", "Back catalogue", START - timedelta(days=1)))
    feed.publish("UC1", Upload("old00000002", "Same minute", START))
    account = FakeAccount(FIRST)

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    clock.advance()
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == []
    assert media_files(media_library) == []


def test_each_video_is_downloaded_once_across_cycles_and_restarts(
    media_library: Path, state_dir: Path, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST)
    with closing(StateStore.open(state_dir)) as state:
        run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
        feed.publish("UC1", Upload("vid00000001", "Once", START + timedelta(minutes=5)))
        clock.advance()
        run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
        clock.advance()
        run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    # A restart in between: a new process on the same state file.
    with closing(StateStore.open(state_dir)) as state:
        clock.advance()
        run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001"]


def test_channel_rename_keeps_the_channel_folder(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    account.current = [Subscription("UC1", "Renamed Channel", None, "")]
    feed.publish("UC1", Upload("vid00000001", "After rename", START + timedelta(minutes=5)))
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    basename = "First Channel - 2026-10-16 12-05 - After rename [vid00000001]"
    assert Path("First Channel", "Season 2026", f"{basename}.mkv") in media_files(media_library)


def test_unsafe_characters_in_channel_and_title_are_replaced(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(Subscription("UC9", ".AC/DC: Live*", None, ""))
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    title = 'Q&A: "Why?" <1/2> | 10:30\tpm...'
    feed.publish("UC9", Upload("vid00000001", title, START + timedelta(minutes=5)))
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    basename = "AC_DC - Live_ - 2026-10-16 12-05 - Q&A - _Why__ _1_2_ _ 10-30 pm [vid00000001]"
    assert Path("AC_DC - Live_", "Season 2026", f"{basename}.mkv") in media_files(media_library)


def test_overlong_title_is_shortened_to_fit_a_filename(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "ä" * 300, START + timedelta(minutes=5)))
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    [video] = videos(media_library)
    assert video.name.endswith(" [vid00000001].mkv")
    assert len(video.name.replace(".mkv", "-thumb.jpg").encode()) <= 255


@pytest.mark.parametrize(
    "error", [FetchFailed("HTTP Error 503"), OSError(28, "No space left on device")]
)
def test_failed_download_leaves_nothing_in_the_media_library_and_is_retried(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "Flaky", START + timedelta(minutes=5)))
    fetcher.errors["vid00000001"] = error
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert "Downloading Flaky (vid00000001) failed" in caplog.text
    assert list(media_library.rglob("*")) == [media_library / ".youtube-helper-staging"]
    del fetcher.errors["vid00000001"]
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001", "vid00000001"]
    assert len(videos(media_library)) == 1


def test_video_appears_under_its_final_name_only_once_complete(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    fetcher.media_library = media_library
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "Big", START + timedelta(minutes=5)))
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert fetcher.library_during_fetch == []
    assert len(videos(media_library)) == 1


def test_unreadable_feed_is_retried_next_cycle_while_other_channels_go_on(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = FakeAccount(FIRST, SECOND)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "Hidden", START + timedelta(minutes=5)))
    feed.publish("UC2", Upload("vid00000002", "Visible", START + timedelta(minutes=5)))
    hidden = feed.channels["UC1"]
    feed.channels["UC1"] = FeedUnavailable("HTTP 500")
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert "Can't read the uploads of First Channel (HTTP 500)" in caplog.text
    assert fetcher.fetched == ["vid00000002"]
    feed.channels["UC1"] = hidden
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000002", "vid00000001"]


def test_upload_left_pending_before_an_unsubscribe_is_not_fetched_after_resubscribing(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "In the gap", START + timedelta(minutes=5)))
    fetcher.errors["vid00000001"] = FetchFailed("HTTP Error 503")
    clock.advance()
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    account.current = []
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    del fetcher.errors["vid00000001"]
    account.current = [FIRST]
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001"]
    assert media_files(media_library) == []


def test_new_upload_gets_an_episode_nfo_next_to_it(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    released = START + timedelta(hours=11, minutes=30)
    feed.publish("UC1", Upload("vid00000001", "Tom & Jerry <live>", released))
    clock.advance(timedelta(hours=12))

    run_cycle(
        media_library,
        state,
        account,
        clock,
        feed=feed,
        fetcher=fetcher,
        timezone=ZoneInfo("Europe/Berlin"),
    )

    # 23:30 UTC is already the next day in Berlin.
    basename = "First Channel - 2026-10-17 01-30 - Tom & Jerry _live_ [vid00000001]"
    assert nfo(media_library / "First Channel" / "Season 2026" / f"{basename}.nfo") == (
        "episodedetails",
        {
            "title": "Tom & Jerry <live>",
            "plot": "Description of vid00000001",
            "aired": "2026-10-17",
            "season": "2026",
            "episode": "1",
            "uniqueid youtube": "vid00000001",
        },
    )


def test_episodes_are_numbered_per_channel_in_release_order_across_cycles(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST, SECOND)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    feed.publish("UC2", Upload("vid00000002", "Other channel", START + timedelta(minutes=6)))
    feed.publish("UC1", Upload("vid00000003", "Two", START + timedelta(minutes=7)))
    feed.publish("UC1", Upload("vid00000004", "Three", START + timedelta(minutes=8)))
    clock.advance()
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000005", "Four", START + timedelta(minutes=40)))
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert episode_numbers(media_library) == {
        "vid00000001": ("2026", "1"),
        "vid00000002": ("2026", "1"),
        "vid00000003": ("2026", "2"),
        "vid00000004": ("2026", "3"),
        "vid00000005": ("2026", "4"),
    }


def test_each_new_season_starts_at_episode_one(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    new_year = datetime(2027, 1, 1, tzinfo=UTC)
    feed.publish("UC1", Upload("vid00000001", "Autumn", START + timedelta(minutes=5)))
    feed.publish("UC1", Upload("vid00000002", "New Year's Eve", new_year - timedelta(minutes=1)))
    clock.now = new_year
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000003", "New Year", new_year + timedelta(minutes=1)))
    feed.publish("UC1", Upload("vid00000004", "January", new_year + timedelta(days=1)))
    clock.advance(timedelta(days=2))

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert episode_numbers(media_library) == {
        "vid00000001": ("2026", "1"),
        "vid00000002": ("2026", "2"),
        "vid00000003": ("2027", "1"),
        "vid00000004": ("2027", "2"),
    }
    assert {video.parent.name for video in videos(media_library)} == {
        "Season 2026",
        "Season 2027",
    }


def test_video_arriving_late_gets_the_next_number_and_nothing_is_renumbered(
    media_library: Path, state_dir: Path, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST)
    with closing(StateStore.open(state_dir)) as state:
        run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
        feed.publish("UC1", Upload("vid00000001", "Early", START + timedelta(minutes=5)))
        feed.publish("UC1", Upload("vid00000002", "Newer", START + timedelta(minutes=20)))
        clock.advance()
        run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    # A premiere released between the two shows up only later; a restart happens in between.
    feed.publish("UC1", Upload("vid00000003", "Premiere", START + timedelta(minutes=10)))
    clock.advance()

    with closing(StateStore.open(state_dir)) as state:
        run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert episode_numbers(media_library) == {
        "vid00000001": ("2026", "1"),
        "vid00000002": ("2026", "2"),
        "vid00000003": ("2026", "3"),
    }


def test_video_downloaded_after_a_failure_gets_the_next_number(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "Flaky", START + timedelta(minutes=5)))
    feed.publish("UC1", Upload("vid00000002", "Fine", START + timedelta(minutes=20)))
    fetcher.errors["vid00000001"] = FetchFailed("HTTP Error 503")
    clock.advance()
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    del fetcher.errors["vid00000001"]
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert episode_numbers(media_library) == {
        "vid00000001": ("2026", "2"),
        "vid00000002": ("2026", "1"),
    }


def test_channel_folder_gets_a_tvshow_nfo_with_its_first_video(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    assert media_files(media_library) == []
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert nfo(media_library / "First Channel" / "tvshow.nfo") == (
        "tvshow",
        {"title": "First Channel", "plot": "About First", "uniqueid youtube": "UC1"},
    )


def test_tvshow_nfo_is_written_from_the_last_known_subscription_while_the_login_has_lapsed(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    account.current = [Subscription("UC1", "Renamed Channel", None, "New description")]
    clock.advance()
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    account.current = LoginLapsed()
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=35)))
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    # The name stays the one from first sight, like the Channel folder; the plot is current.
    assert nfo(media_library / "First Channel" / "tvshow.nfo") == (
        "tvshow",
        {"title": "First Channel", "plot": "New description", "uniqueid youtube": "UC1"},
    )


def test_channel_folder_gets_the_channel_avatar_as_poster(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    poster = media_library / "First Channel" / "poster.jpg"
    assert poster.read_bytes() == b"image https://yt3.ggpht.com/UC1"


def test_poster_that_cannot_be_downloaded_is_tried_again_next_cycle(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    artwork: FakeArtwork,
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher, artwork=artwork)
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    artwork.errors[FIRST.avatar_url or ""] = ArtworkUnavailable("HTTP 503")
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher, artwork=artwork)

    assert "Can't download the poster of First Channel (HTTP 503)" in caplog.text
    assert Path("First Channel", "poster.jpg") not in media_files(media_library)
    assert Path("First Channel", "tvshow.nfo") in media_files(media_library)
    artwork.errors.clear()
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher, artwork=artwork)

    assert Path("First Channel", "poster.jpg") in media_files(media_library)


def test_channel_without_avatar_gets_no_poster(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = FakeAccount(THIRD)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC3", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert Path("Third Channel", "tvshow.nfo") in media_files(media_library)
    assert Path("Third Channel", "poster.jpg") not in media_files(media_library)
    assert "poster" not in caplog.text


def test_tvshow_nfo_follows_a_changed_channel_description(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    clock.advance()
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    account.current = [replace(FIRST, description="About First, revised")]
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert nfo(media_library / "First Channel" / "tvshow.nfo")[1]["plot"] == "About First, revised"


def test_low_disk_space_skips_downloads_and_keeps_new_uploads_pending_until_space_returns(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    disk = FakeDisk(free=9 * GIB)
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher, disk=disk)
    feed.publish("UC1", Upload("vid00000001", "Waiting", START + timedelta(minutes=5)))
    account.current = [FIRST, SECOND]
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher, disk=disk)

    assert (
        f"Only 9.0 GiB free in the Media library {media_library}, less than 10.0 GiB; "
        "no more downloads this cycle, New uploads stay pending" in caplog.text
    )
    assert "Started tracking Second Channel (UC2)" in caplog.text
    assert fetcher.fetched == []
    assert media_files(media_library) == []
    # Gone from the feed by now: only detection during the low-space cycle knows about it.
    feed.channels["UC1"] = []
    disk.free = 10 * GIB
    caplog.clear()
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher, disk=disk)

    assert "no more downloads" not in caplog.text
    assert fetcher.fetched == ["vid00000001"]
    assert len(videos(media_library)) == 1


def test_downloads_stop_once_free_space_drops_below_the_threshold_during_a_cycle(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    disk = FakeDisk(free=11 * GIB)
    fetcher.disk = disk
    account = FakeAccount(FIRST, SECOND)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher, disk=disk)
    feed.publish("UC1", Upload("vid00000001", "Fits", START + timedelta(minutes=5)))
    feed.publish("UC1", Upload("vid00000002", "Also fits", START + timedelta(minutes=6)))
    feed.publish("UC2", Upload("vid00000003", "Waits", START + timedelta(minutes=7)))
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher, disk=disk)

    assert len(fetcher.fetched) == 2
    assert caplog.text.count("no more downloads this cycle") == 1
    disk.free = 100 * GIB
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher, disk=disk)

    assert sorted(fetcher.fetched) == ["vid00000001", "vid00000002", "vid00000003"]


@pytest.mark.parametrize(
    "kind",
    ["a Short", "a scheduled live stream", "a running live stream", "a livestream recording"],
)
def test_videos_that_are_not_regular_are_excluded_and_never_evaluated_again(
    media_library: Path,
    state_dir: Path,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    account = FakeAccount(FIRST)
    with closing(StateStore.open(state_dir)) as state:
        run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
        feed.publish("UC1", Upload("vid00000001", "Not regular", START + timedelta(minutes=5)))
        fetcher.errors["vid00000001"] = NotRegularVideo(kind)
        clock.advance()
        run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
        clock.advance()
        run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    # A restart in between: a new process on the same state file.
    with closing(StateStore.open(state_dir)) as state:
        clock.advance()
        run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001"]
    assert media_files(media_library) == []
    assert caplog.text.count(f"Excluded Not regular (vid00000001): {kind}") == 1
    assert "failed" not in caplog.text


def test_premiere_is_skipped_while_upcoming_and_downloaded_once_aired(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "Premiere", START + timedelta(minutes=5)))
    fetcher.errors["vid00000001"] = NotAiredYet("upcoming")
    clock.advance()
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    clock.advance()
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert media_files(media_library) == []
    assert "Premiere (vid00000001) has not aired yet (upcoming)" in caplog.text
    assert "failed" not in caplog.text
    del fetcher.errors["vid00000001"]
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001"] * 3
    basename = "First Channel - 2026-10-16 12-05 - Premiere [vid00000001]"
    assert Path("First Channel", "Season 2026", f"{basename}.mkv") in media_files(media_library)


def test_videos_not_downloaded_take_no_episode_number(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    account = FakeAccount(FIRST)
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "Short", START + timedelta(minutes=5)))
    feed.publish("UC1", Upload("vid00000002", "Premiere", START + timedelta(minutes=6)))
    feed.publish("UC1", Upload("vid00000003", "Regular", START + timedelta(minutes=7)))
    fetcher.errors["vid00000001"] = NotRegularVideo("a Short")
    fetcher.errors["vid00000002"] = NotAiredYet("upcoming")
    clock.advance()
    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)
    del fetcher.errors["vid00000002"]
    clock.advance()

    run_cycle(media_library, state, account, clock, feed=feed, fetcher=fetcher)

    assert episode_numbers(media_library) == {
        "vid00000002": ("2026", "2"),
        "vid00000003": ("2026", "1"),
    }
