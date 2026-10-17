# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

import logging
import os
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from youtube_helper.artwork import ArtworkUnavailable
from youtube_helper.channels import Channel, ChannelUnavailable
from youtube_helper.cycle import Cycle
from youtube_helper.notifications import EmailSettings, MailError, NotificationFailure, Security
from youtube_helper.state import (
    DATABASE_NAME,
    MIGRATIONS,
    AllDownloadsFailed,
    CycleOutcome,
    DownloadProblem,
    LowSpace,
    StateStore,
)
from youtube_helper.upload_feed import FeedUnavailable, Upload
from youtube_helper.video_fetcher import (
    BotCheck,
    FetchFailed,
    NotAiredYet,
    NotRegularVideo,
    VideoMetadata,
    VideoUnavailable,
)

START = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
GIB = 1024**3
UTC_ZONE = ZoneInfo("UTC")

FIRST = Channel("UC1", "First Channel", "https://yt3.ggpht.com/UC1", "About First")
SECOND = Channel("UC2", "Second Channel", "https://yt3.ggpht.com/UC2", "About Second")
THIRD = Channel("UC3", "Third Channel", None, "")


class FakeResolver:
    """Resolves each channel set on it by its channel ID, or raises the error set for it."""

    def __init__(self, *channels: Channel) -> None:
        self.channels: dict[str, Channel | Exception] = {c.channel_id: c for c in channels}
        self.resolved: list[str] = []

    def resolve(self, reference: str) -> Channel:
        self.resolved.append(reference)
        channel = self.channels.get(reference, ChannelUnavailable(f"unknown {reference}"))
        if isinstance(channel, Exception):
            raise channel
        return channel


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


class FakeMailer:
    """Records the emails sent, or fails with the error set on it."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.error: MailError | None = None

    def send(self, settings: EmailSettings, password: str | None, message: EmailMessage) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(message)


def subscribe(state: StateStore, clock: FakeClock | None, *channels: Channel) -> None:
    """Subscribe to the channels, as the owner does in the web UI, at the clock's time."""
    for channel in channels:
        state.subscribe(channel, (clock or FakeClock())())


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
    clock: FakeClock | None = None,
    *,
    resolver: FakeResolver | None = None,
    feed: FakeFeed | None = None,
    fetcher: FakeFetcher | None = None,
    artwork: FakeArtwork | None = None,
    disk: FakeDisk | None = None,
    mailer: FakeMailer | None = None,
    timezone: ZoneInfo = UTC_ZONE,
) -> None:
    Cycle(
        media_library,
        state,
        resolver=resolver or FakeResolver(FIRST, SECOND, THIRD),
        feed=feed or FakeFeed(),
        fetcher=fetcher or FakeFetcher(),
        artwork=artwork or FakeArtwork(),
        timezone=timezone,
        clock=clock or FakeClock(),
        free_space=disk or FakeDisk(),
        mailer=mailer or FakeMailer(),
    ).run()


def test_cycle_with_writable_media_library_is_logged(
    media_library: Path, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    run_cycle(media_library, state)

    assert "Cycle finished" in caplog.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to read-only directories")
def test_cycle_with_read_only_media_library_is_skipped_naming_the_directory(
    media_library: Path, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    media_library.chmod(0o555)
    try:
        run_cycle(media_library, state)
    finally:
        media_library.chmod(0o755)

    outcome = state.last_cycle()
    assert outcome is not None
    assert outcome.unwritable is not None
    assert f"Media library {media_library} is not writable" in outcome.unwritable
    assert "Cycle started" not in caplog.text


def test_cycle_with_missing_media_library_is_skipped_naming_the_directory(
    tmp_path: Path,
    state: StateStore,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing = tmp_path / "missing"
    subscribe(state, None, FIRST)
    feed.publish("UC1", Upload("vid00000001", "Waiting", START + timedelta(minutes=5)))

    run_cycle(missing, state, feed=feed, fetcher=fetcher)

    assert (
        f"Media library {missing} is not writable: No such file or directory;"
        " skipping this cycle, trying again next cycle" in caplog.text
    )
    assert fetcher.fetched == []
    outcome = state.last_cycle()
    assert outcome is not None
    assert (
        outcome.unwritable == f"Media library {missing} is not writable: No such file or directory"
    )
    assert outcome.downloaded == 0


def test_unwritable_media_library_warning_clears_once_it_is_writable_again(
    tmp_path: Path, state: StateStore, clock: FakeClock
) -> None:
    library = tmp_path / "library"
    run_cycle(library, state, clock)
    library.mkdir()
    clock.advance()

    run_cycle(library, state, clock)

    outcome = state.last_cycle()
    assert outcome is not None
    assert outcome.unwritable is None


def test_skipped_cycle_keeps_the_warnings_it_could_not_check(
    tmp_path: Path, state: StateStore, clock: FakeClock, feed: FakeFeed
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    subscribe(state, clock, FIRST)
    feed.channels["UC1"] = FeedUnavailable("HTTP 500")
    run_cycle(library, state, clock, feed=feed, disk=FakeDisk(free=GIB))
    library.rmdir()
    clock.advance()

    run_cycle(library, state, clock, feed=feed)

    outcome = state.last_cycle()
    assert outcome is not None
    assert outcome.unwritable is not None
    assert outcome.started == clock.now
    assert outcome.low_space == LowSpace(free=GIB, threshold=10 * GIB)
    assert outcome.feed_failures == {"UC1": "HTTP 500"}


def test_cycle_records_when_it_ran_and_how_many_new_uploads_it_downloaded(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    assert state.last_cycle() is None
    subscribe(state, clock, FIRST, SECOND)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    feed.publish("UC2", Upload("vid00000002", "Two", START + timedelta(minutes=6)))
    feed.publish("UC2", Upload("vid00000003", "Broken", START + timedelta(minutes=7)))
    fetcher.errors["vid00000003"] = FetchFailed("HTTP Error 503")
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert state.last_cycle() == CycleOutcome(started=clock.now, finished=clock.now, downloaded=2)


def test_cycle_outcome_survives_a_restart(
    media_library: Path, state_dir: Path, clock: FakeClock
) -> None:
    with closing(StateStore.open(state_dir)) as state:
        run_cycle(media_library, state, clock, disk=FakeDisk(free=GIB))
        recorded = state.last_cycle()

    with closing(StateStore.open(state_dir)) as state:
        assert state.last_cycle() == recorded


def test_cycle_follows_the_active_subscriptions(
    media_library: Path, state: StateStore, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    subscribe(state, clock, FIRST, SECOND)

    run_cycle(media_library, state, clock)

    assert "Active Subscriptions (2): First Channel, Second Channel" in caplog.text


def test_cycle_without_subscriptions_says_where_to_add_them(
    media_library: Path, state: StateStore, fetcher: FakeFetcher, caplog: pytest.LogCaptureFixture
) -> None:
    run_cycle(media_library, state, fetcher=fetcher)

    assert "No active Subscriptions; add channels in the web UI" in caplog.text
    assert fetcher.fetched == []


def test_subscription_is_tracked_from_when_it_was_added(
    media_library: Path, state_dir: Path, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    with closing(StateStore.open(state_dir)) as state:
        subscribe(state, clock, FIRST)
    clock.advance(timedelta(hours=2))
    with closing(StateStore.open(state_dir)) as state:
        subscribe(state, clock, SECOND)
    feed.publish("UC1", Upload("vid00000001", "First, after", START + timedelta(hours=1)))
    feed.publish("UC2", Upload("vid00000002", "Second, before", START + timedelta(hours=1)))
    feed.publish("UC2", Upload("vid00000003", "Second, after", START + timedelta(hours=3)))
    clock.advance(timedelta(hours=2))

    # A restart in between: a new process on the same state file.
    with closing(StateStore.open(state_dir)) as state:
        run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001", "vid00000003"]


def test_unsubscribed_channel_is_no_longer_followed_and_its_files_stay(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    subscribe(state, clock, FIRST, SECOND)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    downloaded = media_library / "First Channel" / "Season 2026" / "video.mkv"
    downloaded.parent.mkdir(parents=True)
    downloaded.write_bytes(b"video")
    state.unsubscribe("UC1")
    feed.publish("UC1", Upload("vid00000001", "After unsubscribing", START + timedelta(hours=1)))
    clock.advance(timedelta(hours=2))
    caplog.clear()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert "Active Subscriptions (1): Second Channel" in caplog.text
    assert fetcher.fetched == []
    assert downloaded.read_bytes() == b"video"


def test_resubscribed_channel_restarts_tracking_and_the_gap_is_never_fetched(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    subscribe(state, clock, FIRST)
    clock.advance()
    state.unsubscribe("UC1")
    feed.publish("UC1", Upload("vid00000001", "In the gap", START + timedelta(hours=1)))
    clock.advance(timedelta(days=3))
    subscribe(state, clock, FIRST)
    feed.publish("UC1", Upload("vid00000002", "After", clock.now + timedelta(minutes=10)))
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000002"]


def test_subscribing_again_to_an_active_subscription_changes_nothing(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    subscribe(state, clock, FIRST)
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(hours=1)))
    clock.advance(timedelta(hours=2))

    subscribe(state, clock, replace(FIRST, display_name="Renamed Channel"))
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001"]
    assert videos(media_library)[0].parts[0] == "First Channel"


def test_resubscribed_channel_keeps_the_name_it_was_first_seen_with(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    subscribe(state, clock, FIRST)
    state.unsubscribe("UC1")
    subscribe(state, clock, replace(FIRST, display_name="Renamed Channel"))
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert "Active Subscriptions (1): First Channel" in caplog.text
    assert videos(media_library)[0].parts[0] == "First Channel"


def test_channel_details_are_refreshed_once_a_day(
    media_library: Path, state: StateStore, clock: FakeClock
) -> None:
    resolver = FakeResolver(FIRST)
    subscribe(state, clock, FIRST)
    clock.advance()
    run_cycle(media_library, state, clock, resolver=resolver)
    assert resolver.resolved == []

    clock.advance(timedelta(days=1))
    run_cycle(media_library, state, clock, resolver=resolver)
    clock.advance()
    run_cycle(media_library, state, clock, resolver=resolver)

    assert resolver.resolved == ["UC1"]


def test_channel_details_that_cannot_be_refreshed_keep_their_last_known_values(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = FakeResolver()
    resolver.channels["UC1"] = ChannelUnavailable("HTTP 503")
    subscribe(state, clock, FIRST)
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    clock.advance(timedelta(days=1))

    run_cycle(media_library, state, clock, resolver=resolver, feed=feed, fetcher=fetcher)

    assert "Can't refresh the details of First Channel (HTTP 503); trying again next cycle" in (
        caplog.text
    )
    assert nfo(media_library / "First Channel" / "tvshow.nfo")[1]["plot"] == "About First"
    resolver.channels["UC1"] = replace(FIRST, description="About First, revised")
    clock.advance()

    run_cycle(media_library, state, clock, resolver=resolver, feed=feed, fetcher=fetcher)

    assert nfo(media_library / "First Channel" / "tvshow.nfo")[1]["plot"] == "About First, revised"


# The last schema with the owner's Google login. Released migrations never change, so applying
# them reproduces a state file that version left behind.
GOOGLE_SCHEMA_VERSION = 10


def test_subscriptions_from_the_google_login_carry_on_after_upgrading(
    media_library: Path,
    state_dir: Path,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with closing(sqlite3.connect(state_dir / DATABASE_NAME, autocommit=True)) as old:
        for migration in MIGRATIONS[:GOOGLE_SCHEMA_VERSION]:
            if isinstance(migration, str):
                old.execute(migration)
            else:
                migration(old)
        old.execute(f"PRAGMA user_version = {GOOGLE_SCHEMA_VERSION}")
        old.execute("INSERT INTO login VALUES (1, 'refresh-token')")
        old.execute(
            "INSERT INTO subscription (channel_id, display_name, active, tracking_start,"
            " channel_folder, description, avatar_url) VALUES"
            " ('UC1', 'First Channel', 1, '2026-10-16T12:00:00+00:00', 'First Channel',"
            " 'About First', 'https://yt3.ggpht.com/UC1'),"
            " ('UC2', 'Second Channel', 0, '2026-10-16T12:00:00+00:00', 'Second Channel', '', NULL)"
        )
        old.execute(
            "INSERT INTO video (video_id, channel_id, title, released, status) VALUES"
            " ('vid00000001', 'UC1', 'Downloaded', '2026-10-16T12:05:00+00:00', 'downloaded'),"
            " ('vid00000002', 'UC1', 'Pending', '2026-10-16T12:10:00+00:00', 'pending')"
        )
        old.execute("INSERT INTO season VALUES ('UC1', 2026, 7)")
    feed.publish("UC1", Upload("vid00000001", "Downloaded", START + timedelta(minutes=5)))
    feed.publish("UC1", Upload("vid00000002", "Pending", START + timedelta(minutes=10)))
    feed.publish("UC2", Upload("vid00000003", "Unsubscribed", START + timedelta(minutes=15)))
    resolver = FakeResolver(FIRST, SECOND)
    clock.advance()

    with closing(StateStore.open(state_dir)) as state:
        run_cycle(media_library, state, clock, resolver=resolver, feed=feed, fetcher=fetcher)

    assert "Active Subscriptions (1): First Channel" in caplog.text
    assert fetcher.fetched == ["vid00000002"]
    assert episode_numbers(media_library) == {"vid00000002": ("2026", "8")}
    # Never looked up by the tool itself: due right away.
    assert resolver.resolved == ["UC1"]


def test_new_upload_lands_in_its_channel_folder_named_by_release_time_in_local_time(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    released = START + timedelta(minutes=10)
    feed.publish("UC1", Upload("vid00000001", "Part 1: The Start", released))
    clock.advance()

    run_cycle(
        media_library,
        state,
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
    subscribe(state, clock, FIRST)

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == []
    assert media_files(media_library) == []


def test_each_video_is_downloaded_once_across_cycles_and_restarts(
    media_library: Path, state_dir: Path, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    with closing(StateStore.open(state_dir)) as state:
        subscribe(state, clock, FIRST)
        run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
        feed.publish("UC1", Upload("vid00000001", "Once", START + timedelta(minutes=5)))
        clock.advance()
        run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
        clock.advance()
        run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    # A restart in between: a new process on the same state file.
    with closing(StateStore.open(state_dir)) as state:
        clock.advance()
        run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001"]


def test_channel_rename_keeps_the_channel_folder(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    resolver = FakeResolver(replace(FIRST, display_name="Renamed Channel"))
    feed.publish("UC1", Upload("vid00000001", "After rename", START + timedelta(minutes=5)))
    clock.advance(timedelta(days=1))

    run_cycle(media_library, state, clock, resolver=resolver, feed=feed, fetcher=fetcher)

    assert resolver.resolved == ["UC1"]
    basename = "First Channel - 2026-10-16 12-05 - After rename [vid00000001]"
    assert Path("First Channel", "Season 2026", f"{basename}.mkv") in media_files(media_library)
    assert nfo(media_library / "First Channel" / "tvshow.nfo")[1]["title"] == "First Channel"


def test_unsafe_characters_in_channel_and_title_are_replaced(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    subscribe(state, clock, Channel("UC9", ".AC/DC: Live*", None, ""))
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    title = 'Q&A: "Why?" <1/2> | 10:30\tpm...'
    feed.publish("UC9", Upload("vid00000001", title, START + timedelta(minutes=5)))
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    basename = "AC_DC - Live_ - 2026-10-16 12-05 - Q&A - _Why__ _1_2_ _ 10-30 pm [vid00000001]"
    assert Path("AC_DC - Live_", "Season 2026", f"{basename}.mkv") in media_files(media_library)


def test_overlong_title_is_shortened_to_fit_a_filename(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "ä" * 300, START + timedelta(minutes=5)))
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

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
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "Flaky", START + timedelta(minutes=5)))
    fetcher.errors["vid00000001"] = error
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert list(media_library.rglob("*")) == [media_library / ".youtube-helper-staging"]
    del fetcher.errors["vid00000001"]
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001", "vid00000001"]
    assert len(videos(media_library)) == 1


def test_video_appears_under_its_final_name_only_once_complete(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    fetcher.media_library = media_library
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "Big", START + timedelta(minutes=5)))
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

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
    subscribe(state, clock, FIRST, SECOND)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "Hidden", START + timedelta(minutes=5)))
    feed.publish("UC2", Upload("vid00000002", "Visible", START + timedelta(minutes=5)))
    hidden = feed.channels["UC1"]
    feed.channels["UC1"] = FeedUnavailable("HTTP 500")
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert "Can't read the uploads of First Channel (HTTP 500)" in caplog.text
    assert fetcher.fetched == ["vid00000002"]
    feed.channels["UC1"] = hidden
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000002", "vid00000001"]


def test_unreadable_feed_is_recorded_for_its_channel_until_it_can_be_read_again(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed
) -> None:
    subscribe(state, clock, FIRST, SECOND)
    feed.channels["UC1"] = FeedUnavailable("HTTP 500")

    run_cycle(media_library, state, clock, feed=feed)

    outcome = state.last_cycle()
    assert outcome is not None
    assert outcome.feed_failures == {"UC1": "HTTP 500"}
    feed.channels["UC1"] = []
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed)

    outcome = state.last_cycle()
    assert outcome is not None
    assert outcome.feed_failures == {}


def test_upload_left_pending_before_an_unsubscribe_is_not_fetched_after_resubscribing(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "In the gap", START + timedelta(minutes=5)))
    fetcher.errors["vid00000001"] = FetchFailed("HTTP Error 503")
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    state.unsubscribe("UC1")
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    del fetcher.errors["vid00000001"]
    subscribe(state, clock, FIRST)
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001"]
    assert media_files(media_library) == []


def test_new_upload_gets_an_episode_nfo_next_to_it(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    released = START + timedelta(hours=11, minutes=30)
    feed.publish("UC1", Upload("vid00000001", "Tom & Jerry <live>", released))
    clock.advance(timedelta(hours=12))

    run_cycle(
        media_library,
        state,
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
    subscribe(state, clock, FIRST, SECOND)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    feed.publish("UC2", Upload("vid00000002", "Other channel", START + timedelta(minutes=6)))
    feed.publish("UC1", Upload("vid00000003", "Two", START + timedelta(minutes=7)))
    feed.publish("UC1", Upload("vid00000004", "Three", START + timedelta(minutes=8)))
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000005", "Four", START + timedelta(minutes=40)))
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

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
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    new_year = datetime(2027, 1, 1, tzinfo=UTC)
    feed.publish("UC1", Upload("vid00000001", "Autumn", START + timedelta(minutes=5)))
    feed.publish("UC1", Upload("vid00000002", "New Year's Eve", new_year - timedelta(minutes=1)))
    clock.now = new_year
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000003", "New Year", new_year + timedelta(minutes=1)))
    feed.publish("UC1", Upload("vid00000004", "January", new_year + timedelta(days=1)))
    clock.advance(timedelta(days=2))

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

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
    with closing(StateStore.open(state_dir)) as state:
        subscribe(state, clock, FIRST)
        run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
        feed.publish("UC1", Upload("vid00000001", "Early", START + timedelta(minutes=5)))
        feed.publish("UC1", Upload("vid00000002", "Newer", START + timedelta(minutes=20)))
        clock.advance()
        run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    # A premiere released between the two shows up only later; a restart happens in between.
    feed.publish("UC1", Upload("vid00000003", "Premiere", START + timedelta(minutes=10)))
    clock.advance()

    with closing(StateStore.open(state_dir)) as state:
        run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert episode_numbers(media_library) == {
        "vid00000001": ("2026", "1"),
        "vid00000002": ("2026", "2"),
        "vid00000003": ("2026", "3"),
    }


def test_video_downloaded_after_a_failure_gets_the_next_number(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "Flaky", START + timedelta(minutes=5)))
    feed.publish("UC1", Upload("vid00000002", "Fine", START + timedelta(minutes=20)))
    fetcher.errors["vid00000001"] = FetchFailed("HTTP Error 503")
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    del fetcher.errors["vid00000001"]
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert episode_numbers(media_library) == {
        "vid00000001": ("2026", "2"),
        "vid00000002": ("2026", "1"),
    }


def test_channel_folder_gets_a_tvshow_nfo_with_its_first_video(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    assert media_files(media_library) == []
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert nfo(media_library / "First Channel" / "tvshow.nfo") == (
        "tvshow",
        {"title": "First Channel", "plot": "About First", "uniqueid youtube": "UC1"},
    )


def test_channel_folder_gets_the_channel_avatar_as_poster(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

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
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher, artwork=artwork)
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    artwork.errors[FIRST.avatar_url or ""] = ArtworkUnavailable("HTTP 503")
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher, artwork=artwork)

    assert "Can't download the poster of First Channel (HTTP 503)" in caplog.text
    assert Path("First Channel", "poster.jpg") not in media_files(media_library)
    assert Path("First Channel", "tvshow.nfo") in media_files(media_library)
    artwork.errors.clear()
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher, artwork=artwork)

    assert Path("First Channel", "poster.jpg") in media_files(media_library)


def test_channel_without_avatar_gets_no_poster(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    subscribe(state, clock, THIRD)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC3", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert Path("Third Channel", "tvshow.nfo") in media_files(media_library)
    assert Path("Third Channel", "poster.jpg") not in media_files(media_library)
    assert "poster" not in caplog.text


def test_tvshow_nfo_follows_a_changed_channel_description(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    resolver = FakeResolver(replace(FIRST, description="About First, revised"))
    clock.advance(timedelta(days=1))

    run_cycle(media_library, state, clock, resolver=resolver, feed=feed, fetcher=fetcher)

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
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher, disk=disk)
    feed.publish("UC1", Upload("vid00000001", "Waiting", START + timedelta(minutes=5)))
    subscribe(state, clock, SECOND)
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher, disk=disk)

    assert (
        f"Only 9.0 GiB free in the Media library {media_library}, less than 10.0 GiB; "
        "no more downloads this cycle, New uploads stay pending" in caplog.text
    )
    assert "Active Subscriptions (2): First Channel, Second Channel" in caplog.text
    assert fetcher.fetched == []
    assert media_files(media_library) == []
    # Gone from the feed by now: only detection during the low-space cycle knows about it.
    feed.channels["UC1"] = []
    disk.free = 10 * GIB
    caplog.clear()
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher, disk=disk)

    assert "no more downloads" not in caplog.text
    assert fetcher.fetched == ["vid00000001"]
    assert len(videos(media_library)) == 1


def test_low_disk_space_is_recorded_until_space_returns(
    media_library: Path, state: StateStore, clock: FakeClock
) -> None:
    disk = FakeDisk(free=9 * GIB)

    # Even without anything to download: downloads are paused all the same.
    run_cycle(media_library, state, clock, disk=disk)

    outcome = state.last_cycle()
    assert outcome is not None
    assert outcome.low_space == LowSpace(free=9 * GIB, threshold=10 * GIB)
    disk.free = 10 * GIB
    clock.advance()

    run_cycle(media_library, state, clock, disk=disk)

    outcome = state.last_cycle()
    assert outcome is not None
    assert outcome.low_space is None


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
    subscribe(state, clock, FIRST, SECOND)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher, disk=disk)
    feed.publish("UC1", Upload("vid00000001", "Fits", START + timedelta(minutes=5)))
    feed.publish("UC1", Upload("vid00000002", "Also fits", START + timedelta(minutes=6)))
    feed.publish("UC2", Upload("vid00000003", "Waits", START + timedelta(minutes=7)))
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher, disk=disk)

    assert len(fetcher.fetched) == 2
    assert caplog.text.count("no more downloads this cycle") == 1
    disk.free = 100 * GIB
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher, disk=disk)

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
    with closing(StateStore.open(state_dir)) as state:
        subscribe(state, clock, FIRST)
        run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
        feed.publish("UC1", Upload("vid00000001", "Not regular", START + timedelta(minutes=5)))
        fetcher.errors["vid00000001"] = NotRegularVideo(kind)
        clock.advance()
        run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
        clock.advance()
        run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    # A restart in between: a new process on the same state file.
    with closing(StateStore.open(state_dir)) as state:
        clock.advance()
        run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

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
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "Premiere", START + timedelta(minutes=5)))
    fetcher.errors["vid00000001"] = NotAiredYet("upcoming")
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert media_files(media_library) == []
    assert "Premiere (vid00000001) has not aired yet (upcoming)" in caplog.text
    assert "failed" not in caplog.text
    del fetcher.errors["vid00000001"]
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001"] * 3
    basename = "First Channel - 2026-10-16 12-05 - Premiere [vid00000001]"
    assert Path("First Channel", "Season 2026", f"{basename}.mkv") in media_files(media_library)


def test_videos_not_downloaded_take_no_episode_number(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "Short", START + timedelta(minutes=5)))
    feed.publish("UC1", Upload("vid00000002", "Premiere", START + timedelta(minutes=6)))
    feed.publish("UC1", Upload("vid00000003", "Regular", START + timedelta(minutes=7)))
    fetcher.errors["vid00000001"] = NotRegularVideo("a Short")
    fetcher.errors["vid00000002"] = NotAiredYet("upcoming")
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    del fetcher.errors["vid00000002"]
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert episode_numbers(media_library) == {
        "vid00000002": ("2026", "2"),
        "vid00000003": ("2026", "1"),
    }


def new_upload_failing_with(
    error: Exception,
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
) -> None:
    """Start tracking First Channel, then publish a New upload whose download raises `error`."""
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "Doomed", START + timedelta(minutes=5)))
    fetcher.errors["vid00000001"] = error
    clock.advance()


def test_permanently_unavailable_video_is_skipped_logged_once_and_never_retried(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = VideoUnavailable("Private video. Sign in if you've been granted access")
    new_upload_failing_with(error, media_library, state, clock, feed, fetcher)

    for _ in range(3):
        run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
        clock.advance()

    assert fetcher.fetched == ["vid00000001"]
    assert (
        caplog.text.count(
            "Skipped Doomed (vid00000001) for good: Private video. Sign in if you've been"
            " granted access"
        )
        == 1
    )
    assert "trying again" not in caplog.text
    assert "outdated" not in caplog.text
    assert media_files(media_library) == []


def test_transiently_failing_video_is_retried_every_cycle_then_given_up_after_24_hours(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = FetchFailed("HTTP Error 503: Service Unavailable")
    new_upload_failing_with(error, media_library, state, clock, feed, fetcher)
    first_failure = clock.now

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    clock.now = first_failure + timedelta(hours=23, minutes=59)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001", "vid00000001"]
    assert "Gave up" not in caplog.text
    clock.now = first_failure + timedelta(hours=24)

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001"] * 3
    assert (
        caplog.text.count(
            "Gave up on Doomed (vid00000001) after failing for 24 hours:"
            " HTTP Error 503: Service Unavailable"
        )
        == 1
    )


def test_bot_check_is_called_out_and_retried(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = BotCheck("Sign in to confirm you're not a bot")
    new_upload_failing_with(error, media_library, state, clock, feed, fetcher)
    feed.publish("UC1", Upload("vid00000002", "Fine", START + timedelta(minutes=6)))

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert (
        "YouTube asked for a bot check while downloading Doomed (vid00000001); trying again"
        " next cycle. If this keeps happening, a PO-token provider is needed" in caplog.text
    )
    del fetcher.errors["vid00000001"]
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001", "vid00000002", "vid00000001"]
    assert len(videos(media_library)) == 2


def test_cycle_where_every_download_fails_says_once_that_yt_dlp_is_likely_outdated(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    subscribe(state, clock, FIRST, SECOND)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    feed.publish("UC1", Upload("vid00000002", "Two", START + timedelta(minutes=6)))
    feed.publish("UC2", Upload("vid00000003", "Three", START + timedelta(minutes=7)))
    fetcher.errors["vid00000001"] = FetchFailed("Unable to extract player response")
    fetcher.errors["vid00000002"] = FetchFailed("Unable to extract player response")
    fetcher.errors["vid00000003"] = BotCheck("Sign in to confirm you're not a bot")
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert fetcher.fetched == ["vid00000001", "vid00000002", "vid00000003"]
    assert (
        caplog.text.count(
            "All downloads failing (3 this cycle, last error: Sign in to confirm you're not a"
            " bot); yt-dlp is likely outdated, pull the latest image. YouTube asked 1 of them"
            " for a bot check. If this keeps happening, a PO-token provider is needed"
        )
        == 1
    )
    assert "trying again" not in caplog.text
    fetcher.errors.clear()
    caplog.clear()
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert len(videos(media_library)) == 3
    assert "outdated" not in caplog.text


def test_cycle_with_some_downloads_succeeding_reports_each_failure(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = FetchFailed("HTTP Error 503")
    new_upload_failing_with(error, media_library, state, clock, feed, fetcher)
    feed.publish("UC1", Upload("vid00000002", "Fine", START + timedelta(minutes=6)))

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert "Downloading Doomed (vid00000001) failed (HTTP Error 503); trying again" in caplog.text
    assert "outdated" not in caplog.text


def test_permanent_failures_alone_do_not_suggest_yt_dlp_is_outdated(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    new_upload_failing_with(
        VideoUnavailable("Video unavailable"), media_library, state, clock, feed, fetcher
    )
    feed.publish("UC1", Upload("vid00000002", "Flaky", START + timedelta(minutes=6)))
    fetcher.errors["vid00000002"] = FetchFailed("HTTP Error 503")

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert "Downloading Flaky (vid00000002) failed (HTTP Error 503); trying again" in caplog.text
    assert "outdated" not in caplog.text


def test_failing_writes_to_the_media_library_do_not_blame_yt_dlp(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = OSError(28, "No space left on device")
    new_upload_failing_with(error, media_library, state, clock, feed, fetcher)

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert (
        "Downloading Doomed (vid00000001) failed ([Errno 28] No space left on device);"
        " trying again next cycle" in caplog.text
    )
    assert "outdated" not in caplog.text


def test_videos_not_to_be_downloaded_neither_hide_nor_count_as_failing_downloads(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "Premiere", START + timedelta(minutes=5)))
    feed.publish("UC1", Upload("vid00000002", "Short", START + timedelta(minutes=6)))
    feed.publish("UC1", Upload("vid00000003", "Broken", START + timedelta(minutes=7)))
    fetcher.errors["vid00000001"] = NotAiredYet("upcoming")
    fetcher.errors["vid00000002"] = NotRegularVideo("a Short")
    fetcher.errors["vid00000003"] = FetchFailed("Unable to extract player response")
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert "All downloads failing (1 this cycle" in caplog.text
    # A premiere upcoming for days is never given up on.
    del fetcher.errors["vid00000003"]
    clock.advance(timedelta(days=2))
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    del fetcher.errors["vid00000001"]
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert "Gave up" not in caplog.text
    assert sorted(fetcher.fetched) == [
        "vid00000001",
        "vid00000001",
        "vid00000001",
        "vid00000002",
        "vid00000003",
        "vid00000003",
    ]
    assert len(videos(media_library)) == 2


def test_cycle_where_every_download_fails_is_recorded_until_a_download_succeeds(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    subscribe(state, clock, FIRST)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=5)))
    feed.publish("UC1", Upload("vid00000002", "Two", START + timedelta(minutes=6)))
    fetcher.errors["vid00000001"] = FetchFailed("Unable to extract player response")
    fetcher.errors["vid00000002"] = BotCheck("Sign in to confirm you're not a bot")
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    outcome = state.last_cycle()
    assert outcome is not None
    assert outcome.all_downloads_failed == AllDownloadsFailed(
        attempts=2, last_error="Sign in to confirm you're not a bot", bot_checks=1
    )
    fetcher.errors.clear()
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    outcome = state.last_cycle()
    assert outcome is not None
    assert outcome.all_downloads_failed is None


def test_cycle_without_downloads_keeps_the_verdict_on_yt_dlp(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    error = FetchFailed("Unable to extract player response")
    new_upload_failing_with(error, media_library, state, clock, feed, fetcher)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    # Given up on: the next cycle has nothing left to try.
    clock.advance(timedelta(hours=24))
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    outcome = state.last_cycle()
    assert outcome is not None
    assert outcome.all_downloads_failed == AllDownloadsFailed(
        attempts=1, last_error="Unable to extract player response", bot_checks=0
    )


def test_videos_being_retried_are_problems_of_their_subscription(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    error = FetchFailed("HTTP Error 503")
    new_upload_failing_with(error, media_library, state, clock, feed, fetcher)
    first_failure = clock.now
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    fetcher.errors["vid00000001"] = FetchFailed("HTTP Error 429")
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert state.download_problems(clock.now) == {
        "UC1": [
            DownloadProblem(
                "vid00000001", "Doomed", "retrying", "HTTP Error 429", first_failure, None
            )
        ]
    }
    del fetcher.errors["vid00000001"]
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    assert state.download_problems(clock.now) == {}


def test_videos_given_up_or_skipped_are_problems_for_30_days(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    error = FetchFailed("HTTP Error 503")
    new_upload_failing_with(error, media_library, state, clock, feed, fetcher)
    feed.publish("UC1", Upload("vid00000002", "Private", START + timedelta(minutes=6)))
    feed.publish("UC1", Upload("vid00000003", "Short", START + timedelta(minutes=7)))
    fetcher.errors["vid00000002"] = VideoUnavailable("Private video")
    fetcher.errors["vid00000003"] = NotRegularVideo("a Short")
    first_failure = clock.now
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    clock.advance(timedelta(hours=24))
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)
    gave_up = clock.now

    clock.advance(timedelta(days=30) - timedelta(hours=24))

    assert state.download_problems(clock.now) == {
        "UC1": [
            DownloadProblem(
                "vid00000001", "Doomed", "failed", "HTTP Error 503", first_failure, gave_up
            ),
            DownloadProblem(
                "vid00000002", "Private", "skipped", "Private video", None, first_failure
            ),
        ]
    }
    clock.advance(timedelta(hours=24, seconds=1))

    assert state.download_problems(clock.now) == {}


def test_problems_of_unsubscribed_channels_are_not_shown(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, fetcher: FakeFetcher
) -> None:
    error = FetchFailed("HTTP Error 503")
    new_upload_failing_with(error, media_library, state, clock, feed, fetcher)
    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher)

    state.unsubscribe("UC1")

    assert state.download_problems(clock.now) == {}
    # Left pending from before: back catalogue once tracking restarts, so no problem.
    clock.advance()
    subscribe(state, clock, FIRST)
    assert state.download_problems(clock.now) == {}


EMAIL = EmailSettings(
    enabled=True,
    host="smtp.example.org",
    port=25,
    security=Security.NONE,
    from_address="youtube-helper@example.org",
    recipients=("owner@example.org",),
)


@pytest.fixture
def mailer() -> FakeMailer:
    return FakeMailer()


def file_videos(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    mailer: FakeMailer,
    *uploads: tuple[str, Upload],
) -> None:
    """Run a cycle half an hour later that files `uploads`, each published by its channel ID."""
    for channel_id, upload in uploads:
        feed.publish(channel_id, upload)
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, mailer=mailer)


def body(message: EmailMessage) -> str:
    content = message.get_content()
    assert isinstance(content, str)
    return content


def test_cycle_sends_one_notification_listing_the_videos_it_filed_by_subscription(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, mailer: FakeMailer
) -> None:
    subscribe(state, clock, FIRST, SECOND)
    state.save_email_settings(EMAIL)

    file_videos(
        media_library,
        state,
        clock,
        feed,
        mailer,
        ("UC2", Upload("vid00000003", "Three", START + timedelta(minutes=3))),
        ("UC1", Upload("vid00000001", "One", START + timedelta(minutes=1))),
        ("UC1", Upload("vid00000002", "Two", START + timedelta(days=-1, minutes=2))),
        ("UC1", Upload("vid00000004", "Four", START + timedelta(minutes=4))),
    )

    [message] = mailer.sent
    assert message["Subject"] == "youtube-helper: 3 new videos"
    assert message["From"] == "youtube-helper@example.org"
    assert message["To"] == "owner@example.org"
    assert body(message) == (
        "3 new videos were filed into the Media library.\n"
        "\n"
        "First Channel\n"
        "\n"
        "- One (aired 2026-10-16)\n"
        "  https://www.youtube.com/watch?v=vid00000001\n"
        "- Four (aired 2026-10-16)\n"
        "  https://www.youtube.com/watch?v=vid00000004\n"
        "\n"
        "Second Channel\n"
        "\n"
        "- Three (aired 2026-10-16)\n"
        "  https://www.youtube.com/watch?v=vid00000003\n"
    )


def test_notification_of_a_single_video_says_video(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, mailer: FakeMailer
) -> None:
    subscribe(state, clock, FIRST)
    state.save_email_settings(EMAIL)

    file_videos(
        media_library,
        state,
        clock,
        feed,
        mailer,
        ("UC1", Upload("vid00000001", "One", START + timedelta(minutes=1))),
    )

    [message] = mailer.sent
    assert message["Subject"] == "youtube-helper: 1 new video"
    assert body(message).startswith("1 new video was filed into the Media library.\n")


def test_air_date_is_the_local_one(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, mailer: FakeMailer
) -> None:
    subscribe(state, clock, FIRST)
    state.save_email_settings(EMAIL)
    feed.publish("UC1", Upload("vid00000001", "Late", START + timedelta(hours=11)))
    clock.advance(timedelta(hours=12))

    run_cycle(
        media_library, state, clock, feed=feed, mailer=mailer, timezone=ZoneInfo("Asia/Tokyo")
    )

    # 23:00 UTC is 08:00 the next day in Tokyo.
    assert "- Late (aired 2026-10-17)" in body(mailer.sent[0])


def test_each_video_is_reported_once(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, mailer: FakeMailer
) -> None:
    subscribe(state, clock, FIRST)
    state.save_email_settings(EMAIL)
    file_videos(
        media_library,
        state,
        clock,
        feed,
        mailer,
        ("UC1", Upload("vid00000001", "One", START + timedelta(minutes=1))),
    )

    file_videos(
        media_library,
        state,
        clock,
        feed,
        mailer,
        ("UC1", Upload("vid00000002", "Two", START + timedelta(minutes=31))),
    )
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, mailer=mailer)

    assert [m["Subject"] for m in mailer.sent] == [
        "youtube-helper: 1 new video",
        "youtube-helper: 1 new video",
    ]
    assert "vid00000001" not in body(mailer.sent[1])
    assert "vid00000002" in body(mailer.sent[1])


def test_no_notification_when_nothing_was_filed(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    fetcher: FakeFetcher,
    mailer: FakeMailer,
) -> None:
    subscribe(state, clock, FIRST)
    state.save_email_settings(EMAIL)
    feed.publish("UC1", Upload("vid00000001", "Broken", START + timedelta(minutes=1)))
    fetcher.errors["vid00000001"] = FetchFailed("HTTP Error 503")
    clock.advance()

    run_cycle(media_library, state, clock, feed=feed, fetcher=fetcher, mailer=mailer)

    assert mailer.sent == []


@pytest.mark.parametrize(
    "settings", [replace(EMAIL, enabled=False), replace(EMAIL, new_videos=False)]
)
def test_no_notification_when_email_or_new_videos_are_switched_off(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    mailer: FakeMailer,
    settings: EmailSettings,
) -> None:
    subscribe(state, clock, FIRST)
    state.save_email_settings(settings)

    file_videos(
        media_library,
        state,
        clock,
        feed,
        mailer,
        ("UC1", Upload("vid00000001", "One", START + timedelta(minutes=1))),
    )

    assert mailer.sent == []


@pytest.mark.parametrize(
    "before", [replace(EMAIL, enabled=False), replace(EMAIL, new_videos=False)]
)
def test_switching_notifications_on_does_not_report_videos_filed_before(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    mailer: FakeMailer,
    before: EmailSettings,
) -> None:
    subscribe(state, clock, FIRST)
    state.save_email_settings(before)
    file_videos(
        media_library,
        state,
        clock,
        feed,
        mailer,
        ("UC1", Upload("vid00000001", "Back catalogue", START + timedelta(minutes=1))),
    )

    state.save_email_settings(EMAIL)
    file_videos(
        media_library,
        state,
        clock,
        feed,
        mailer,
        ("UC1", Upload("vid00000002", "Fresh", START + timedelta(minutes=31))),
    )

    [message] = mailer.sent
    assert "vid00000001" not in body(message)
    assert "vid00000002" in body(message)


def test_saving_settings_that_keep_notifications_on_reports_everything_still_unreported(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, mailer: FakeMailer
) -> None:
    subscribe(state, clock, FIRST)
    state.save_email_settings(EMAIL)
    mailer.error = MailError("Connection refused")
    file_videos(
        media_library,
        state,
        clock,
        feed,
        mailer,
        ("UC1", Upload("vid00000001", "One", START + timedelta(minutes=1))),
    )

    state.save_email_settings(replace(EMAIL, host="mail.example.net"))
    mailer.error = None
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, mailer=mailer)

    [message] = mailer.sent
    assert "vid00000001" in body(message)


def test_failed_notification_is_a_warning_and_the_next_delivered_one_covers_both_cycles(
    media_library: Path,
    state: StateStore,
    clock: FakeClock,
    feed: FakeFeed,
    mailer: FakeMailer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    subscribe(state, clock, FIRST)
    state.save_email_settings(EMAIL)
    mailer.error = MailError("[Errno 111] Connection refused")
    file_videos(
        media_library,
        state,
        clock,
        feed,
        mailer,
        ("UC1", Upload("vid00000001", "One", START + timedelta(minutes=1))),
    )
    failed_at = clock.now
    file_videos(
        media_library,
        state,
        clock,
        feed,
        mailer,
        ("UC1", Upload("vid00000002", "Two", START + timedelta(minutes=31))),
    )

    assert mailer.sent == []
    assert state.notification_failure() == NotificationFailure(
        failed_at, "Email can't be sent: [Errno 111] Connection refused"
    )
    assert "Email can't be sent: [Errno 111] Connection refused" in caplog.text

    mailer.error = None
    file_videos(
        media_library,
        state,
        clock,
        feed,
        mailer,
        ("UC1", Upload("vid00000003", "Three", START + timedelta(minutes=61))),
    )

    [message] = mailer.sent
    assert message["Subject"] == "youtube-helper: 3 new videos"
    assert body(message).startswith(
        "Notifications were failing since 2026-10-16 12:30 UTC;"
        " this one covers everything since then.\n"
        "\n"
        "3 new videos were filed into the Media library.\n"
    )
    assert state.notification_failure() is None


def test_notification_after_a_failure_says_when_it_failed_first_in_local_time(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, mailer: FakeMailer
) -> None:
    subscribe(state, clock, FIRST)
    state.save_email_settings(EMAIL)
    mailer.error = MailError("timed out")
    feed.publish("UC1", Upload("vid00000001", "One", START + timedelta(minutes=1)))
    berlin = ZoneInfo("Europe/Berlin")
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, mailer=mailer, timezone=berlin)

    mailer.error = None
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, mailer=mailer, timezone=berlin)

    assert body(mailer.sent[0]).startswith(
        "Notifications were failing since 2026-10-16 14:30 CEST;"
    )


def test_undecryptable_password_is_a_warning_and_nothing_is_sent(
    media_library: Path, state_dir: Path, clock: FakeClock, feed: FakeFeed, mailer: FakeMailer
) -> None:
    with closing(StateStore.open(state_dir, secret_key=b"the old secret")) as state:
        subscribe(state, clock, FIRST)
        state.save_email_settings(
            replace(EMAIL, security=Security.STARTTLS, port=587, username="owner"),
            password="hunter2",
        )

    with closing(StateStore.open(state_dir, secret_key=b"a rotated secret")) as state:
        file_videos(
            media_library,
            state,
            clock,
            feed,
            mailer,
            ("UC1", Upload("vid00000001", "One", START + timedelta(minutes=1))),
        )

        failure = state.notification_failure()

    assert mailer.sent == []
    assert failure is not None
    assert failure.warning == (
        "SMTP password can't be decrypted with the current secret key; enter it again"
    )


def test_failing_notification_does_not_break_the_cycle(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, mailer: FakeMailer
) -> None:
    subscribe(state, clock, FIRST)
    state.save_email_settings(EMAIL)
    mailer.error = MailError("timed out")

    file_videos(
        media_library,
        state,
        clock,
        feed,
        mailer,
        ("UC1", Upload("vid00000001", "One", START + timedelta(minutes=1))),
    )

    outcome = state.last_cycle()
    assert outcome is not None
    assert outcome.downloaded == 1
    assert len(videos(media_library)) == 1


def test_notification_warning_clears_once_notifications_are_switched_off(
    media_library: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, mailer: FakeMailer
) -> None:
    subscribe(state, clock, FIRST)
    state.save_email_settings(EMAIL)
    mailer.error = MailError("timed out")
    file_videos(
        media_library,
        state,
        clock,
        feed,
        mailer,
        ("UC1", Upload("vid00000001", "One", START + timedelta(minutes=1))),
    )

    state.save_email_settings(replace(EMAIL, enabled=False))
    clock.advance()
    run_cycle(media_library, state, clock, feed=feed, mailer=mailer)

    assert state.notification_failure() is None


def test_unreported_videos_are_still_reported_after_a_restart(
    media_library: Path, state_dir: Path, clock: FakeClock, feed: FakeFeed, mailer: FakeMailer
) -> None:
    with closing(StateStore.open(state_dir)) as state:
        subscribe(state, clock, FIRST)
        state.save_email_settings(EMAIL)
        mailer.error = MailError("timed out")
        file_videos(
            media_library,
            state,
            clock,
            feed,
            mailer,
            ("UC1", Upload("vid00000001", "One", START + timedelta(minutes=1))),
        )

    mailer.error = None
    with closing(StateStore.open(state_dir)) as state:
        clock.advance()
        run_cycle(media_library, state, clock, feed=feed, mailer=mailer)

    [message] = mailer.sent
    assert "vid00000001" in body(message)
    assert body(message).startswith("Notifications were failing since")


def test_skipped_cycle_still_sends_what_is_unreported(
    tmp_path: Path, state: StateStore, clock: FakeClock, feed: FakeFeed, mailer: FakeMailer
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    subscribe(state, clock, FIRST)
    state.save_email_settings(EMAIL)
    mailer.error = MailError("timed out")
    file_videos(
        library,
        state,
        clock,
        feed,
        mailer,
        ("UC1", Upload("vid00000001", "One", START + timedelta(minutes=1))),
    )
    library.rename(tmp_path / "moved")

    mailer.error = None
    clock.advance()
    run_cycle(library, state, clock, feed=feed, mailer=mailer)

    [message] = mailer.sent
    assert "vid00000001" in body(message)
