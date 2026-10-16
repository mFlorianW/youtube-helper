# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The Cycle: everything the tool does every 30 minutes, behind a single "run one cycle" entry."""

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
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
    staged,
    video_base,
)
from youtube_helper.state import StateStore, TrackedSubscription
from youtube_helper.upload_feed import FeedUnavailable, UploadFeed
from youtube_helper.video_fetcher import FetchFailed, VideoFetcher

log = logging.getLogger(__name__)


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
    ) -> None:
        self._media_library = media_library
        self._state = state
        self._account = account
        self._feed = feed
        self._fetcher = fetcher
        self._timezone = timezone
        self._clock = clock

    def run(self) -> None:
        """Run one cycle.

        Raises MediaLibraryUnwritable before doing anything if the Media library can't be written.
        """
        ensure_writable(self._media_library)
        log.info("Cycle started")
        for subscription in self._active_subscriptions():
            self._detect_new_uploads(subscription)
            self._download_new_uploads(subscription)
        log.info("Cycle finished")

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

    def _download_new_uploads(self, subscription: TrackedSubscription) -> None:
        for upload in self._state.pending_uploads(subscription.channel_id):
            # Uploads left pending before an unsubscribe fall into the gap: never fetched.
            if not subscription.tracks(upload):
                continue
            base = video_base(
                self._media_library, subscription.channel_folder, upload, self._timezone
            )
            log.info("Downloading %s (%s)", upload.title, upload.video_id)
            try:
                with staged(self._media_library, upload.video_id) as staged_base:
                    self._fetcher.fetch(upload.video_id, staged_base)
                    file_video(staged_base, base)
            except (FetchFailed, OSError) as error:
                log.warning(
                    "Downloading %s (%s) failed (%s); trying again next cycle",
                    upload.title,
                    upload.video_id,
                    error,
                )
                continue
            self._state.mark_downloaded(upload.video_id)
            log.info("Downloaded %s", base.relative_to(self._media_library))

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


def _format_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
