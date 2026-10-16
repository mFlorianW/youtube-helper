# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The Cycle: everything the tool does every 30 minutes, behind a single "run one cycle" entry."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from youtube_helper.account import (
    Account,
    AccountUnavailable,
    LoginLapsed,
    LoginMissing,
    Subscription,
)
from youtube_helper.media_library import (
    channel_folder,
    ensure_writable,
    file_video,
    free_space,
    staged,
    video_base,
)
from youtube_helper.state import StateStore, TrackedSubscription
from youtube_helper.upload_feed import FeedUnavailable, Upload, UploadFeed
from youtube_helper.video_fetcher import BotCheck, FetchFailed, VideoFetcher, VideoUnavailable

log = logging.getLogger(__name__)

# Below this much free space in the Media library, downloads wait so the disk never fills up.
GIB = 1024**3
MIN_FREE_SPACE = 10 * GIB

# How long a video may keep failing before the Cycle gives up on it.
RETRY_PERIOD = timedelta(hours=24)

BOT_CHECK_ADVICE = "If this keeps happening, a PO-token provider is needed"


class Cycle:
    def __init__(
        self,
        media_library: Path,
        state: StateStore,
        *,
        account: Account,
        feed: UploadFeed,
        fetcher: VideoFetcher,
        timezone: ZoneInfo,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        free_space: Callable[[Path], int] = free_space,
    ) -> None:
        self._media_library = media_library
        self._state = state
        self._account = account
        self._feed = feed
        self._fetcher = fetcher
        self._timezone = timezone
        self._clock = clock
        self._free_space = free_space

    def run(self) -> None:
        """Run one cycle.

        Raises MediaLibraryUnwritable before doing anything if the Media library can't be written.
        """
        ensure_writable(self._media_library)
        log.info("Cycle started")
        subscriptions = self._active_subscriptions()
        for subscription in subscriptions:
            self._detect_new_uploads(subscription)
        downloads = _Downloads()
        self._download_new_uploads(subscriptions, downloads)
        downloads.report_retries()
        log.info("Cycle finished")

    def _enough_free_space(self) -> bool:
        """Whether there is room for the next download; logs why not when there isn't."""
        free = self._free_space(self._media_library)
        if free >= MIN_FREE_SPACE:
            return True
        log.warning(
            "Only %s free in the Media library %s, less than %s; "
            "no more downloads this cycle, New uploads stay pending",
            _format_size(free),
            self._media_library,
            _format_size(MIN_FREE_SPACE),
        )
        return False

    def _detect_new_uploads(self, subscription: TrackedSubscription) -> None:
        """Remember the uploads released after the Subscription's tracking start."""
        try:
            uploads = self._feed.uploads(subscription.channel_id)
        except FeedUnavailable as error:
            log.warning(
                "Can't read the uploads of %s (%s); trying again next cycle",
                subscription.display_name,
                error,
            )
            return
        self._state.record_uploads(
            subscription.channel_id,
            (u for u in uploads if subscription.tracks(u)),
        )

    def _download_new_uploads(
        self, subscriptions: list[TrackedSubscription], downloads: _Downloads
    ) -> None:
        for subscription in subscriptions:
            for upload in self._state.pending_uploads(subscription.channel_id):
                # Uploads left pending before an unsubscribe fall into the gap: never fetched.
                if not subscription.tracks(upload):
                    continue
                # Checked before every download, as each one eats into the free space.
                if not self._enough_free_space():
                    return
                self._download(subscription, upload, downloads)

    def _download(
        self, subscription: TrackedSubscription, upload: Upload, downloads: _Downloads
    ) -> None:
        base = video_base(self._media_library, subscription.channel_folder, upload, self._timezone)
        log.info("Downloading %s (%s)", upload.title, upload.video_id)
        downloads.attempts += 1
        try:
            with staged(self._media_library, upload.video_id) as staged_base:
                self._fetcher.fetch(upload.video_id, staged_base)
                file_video(staged_base, base)
        except VideoUnavailable as error:
            self._state.mark_skipped(upload.video_id, str(error))
            log.warning("Skipped %s (%s) for good: %s", upload.title, upload.video_id, error)
        except (FetchFailed, OSError) as error:
            self._retry_later(upload, error, downloads)
        else:
            self._state.mark_downloaded(upload.video_id)
            log.info("Downloaded %s", base.relative_to(self._media_library))

    def _retry_later(self, upload: Upload, error: Exception, downloads: _Downloads) -> None:
        """Leave the upload pending for the next cycle, or give up once it failed for too long."""
        now = self._clock()
        first_failure = self._state.record_failure(upload.video_id, str(error), now)
        if now - first_failure >= RETRY_PERIOD:
            self._state.mark_failed(upload.video_id)
            log.error(
                "Gave up on %s (%s) after failing for %d hours: %s",
                upload.title,
                upload.video_id,
                RETRY_PERIOD // timedelta(hours=1),
                error,
            )
            downloads.failures.append(_Failure(upload, error, retried=False))
        else:
            downloads.failures.append(_Failure(upload, error, retried=True))

    def _active_subscriptions(self) -> list[TrackedSubscription]:
        """The Subscriptions to follow this cycle, synced with the account when it can be read."""
        try:
            current = self._account.subscriptions()
        except LoginMissing:
            log.warning("Not logged in, run `login`")
            return []
        except LoginLapsed:
            log.warning("Login has lapsed, run `login` again; using the last known Subscriptions")
        except AccountUnavailable as error:
            log.warning("Can't read the Subscriptions (%s); the last known ones are used", error)
        else:
            self._sync(current)
        active = [s for s in self._state.tracked_subscriptions() if s.active]
        log.info(
            "Active Subscriptions (%d): %s",
            len(active),
            ", ".join(s.display_name for s in active),
        )
        return active

    def _sync(self, current: list[Subscription]) -> None:
        """Start, restart or stop tracking so the tracked Subscriptions match `current`."""
        now = self._clock()
        known = {s.channel_id: s for s in self._state.tracked_subscriptions()}
        changed = []
        for subscription in current:
            tracked = known.get(subscription.channel_id)
            if tracked is None:
                tracked = TrackedSubscription(
                    channel_id=subscription.channel_id,
                    display_name=subscription.display_name,
                    channel_folder=channel_folder(
                        subscription.display_name, subscription.channel_id
                    ),
                    active=True,
                    tracking_start=now,
                )
                action = "Started"
            elif not tracked.active:
                # Keep the name from first sight: the Channel folder never follows a rename.
                tracked = replace(tracked, active=True, tracking_start=now)
                action = "Restarted"
            else:
                continue
            changed.append(tracked)
            log.info(
                "%s tracking %s (%s) from %s",
                action,
                tracked.display_name,
                tracked.channel_id,
                _format_time(now),
            )
        current_ids = {s.channel_id for s in current}
        for tracked in known.values():
            if tracked.active and tracked.channel_id not in current_ids:
                changed.append(replace(tracked, active=False))
                log.info(
                    "Stopped tracking %s (%s): unsubscribed; its files are kept",
                    tracked.display_name,
                    tracked.channel_id,
                )
        self._state.save_tracked_subscriptions(changed)


@dataclass(frozen=True)
class _Failure:
    upload: Upload
    error: Exception
    retried: bool


@dataclass
class _Downloads:
    """The download attempts of one cycle and those that failed, not for good: retried or given up.

    Tells a broken yt-dlp from videos that fail on their own.
    """

    attempts: int = 0
    failures: list[_Failure] = field(default_factory=list)

    def _yt_dlp_broken(self) -> bool:
        """Whether every download attempt failed, and in yt-dlp: not writing the Media library."""
        return (
            self.attempts > 0
            and len(self.failures) == self.attempts
            and all(isinstance(f.error, FetchFailed) for f in self.failures)
        )

    def report_retries(self) -> None:
        """Log each download to be retried, or one message if every download failed."""
        if self._yt_dlp_broken():
            bot_checks = sum(isinstance(f.error, BotCheck) for f in self.failures)
            log.error(
                "All downloads failing (%d this cycle, last error: %s); yt-dlp is likely"
                " outdated, pull the latest image%s",
                self.attempts,
                self.failures[-1].error,
                f". YouTube asked {bot_checks} of them for a bot check. {BOT_CHECK_ADVICE}"
                if bot_checks
                else "",
            )
            return
        for failure in self.failures:
            if not failure.retried:
                continue
            if isinstance(failure.error, BotCheck):
                log.warning(
                    "YouTube asked for a bot check while downloading %s (%s); trying again"
                    " next cycle. %s",
                    failure.upload.title,
                    failure.upload.video_id,
                    BOT_CHECK_ADVICE,
                )
            else:
                log.warning(
                    "Downloading %s (%s) failed (%s); trying again next cycle",
                    failure.upload.title,
                    failure.upload.video_id,
                    failure.error,
                )


def _format_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _format_size(size: int) -> str:
    return f"{size / GIB:.1f} GiB"
