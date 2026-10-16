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
from youtube_helper.artwork import Artwork, ArtworkUnavailable
from youtube_helper.media_library import (
    NFO_SUFFIX,
    POSTER,
    TVSHOW_NFO,
    channel_folder,
    ensure_writable,
    file_atomically,
    file_video,
    staged,
    video_base,
)
from youtube_helper.nfo import episode_nfo, tvshow_nfo
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
        artwork: Artwork,
        timezone: ZoneInfo,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._media_library = media_library
        self._state = state
        self._account = account
        self._feed = feed
        self._fetcher = fetcher
        self._artwork = artwork
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
            self._complete_channel_folder(subscription)
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
            aired = upload.released.astimezone(self._timezone).date()
            # Numbered when filed, so a video arriving late gets the next number of its season.
            episode = self._state.next_episode(subscription.channel_id, aired.year)
            log.info("Downloading %s (%s)", upload.title, upload.video_id)
            try:
                with staged(self._media_library, upload.video_id) as staged_base:
                    metadata = self._fetcher.fetch(upload.video_id, staged_base)
                    staged_base.with_name(staged_base.name + NFO_SUFFIX).write_bytes(
                        episode_nfo(
                            title=upload.title,
                            plot=metadata.description,
                            aired=aired,
                            season=aired.year,
                            episode=episode,
                            video_id=upload.video_id,
                        )
                    )
                    file_video(staged_base, base)
            except (FetchFailed, OSError) as error:
                log.warning(
                    "Downloading %s (%s) failed (%s); trying again next cycle",
                    upload.title,
                    upload.video_id,
                    error,
                )
                continue
            self._state.mark_downloaded(
                upload.video_id, subscription.channel_id, aired.year, episode
            )
            log.info("Downloaded %s", base.relative_to(self._media_library))

    def _complete_channel_folder(self, subscription: TrackedSubscription) -> None:
        """Give a Channel folder holding videos the `tvshow.nfo` and poster it still lacks.

        Each is written once; one that can't be written is tried again next cycle.
        """
        folder = self._media_library / subscription.channel_folder
        if not folder.is_dir():
            return
        tvshow = folder / TVSHOW_NFO
        if not tvshow.exists():
            content = tvshow_nfo(
                title=subscription.display_name,
                plot=subscription.description,
                channel_id=subscription.channel_id,
            )
            try:
                file_atomically(self._media_library, tvshow, content)
            except OSError as error:
                log.warning(
                    "Can't write the %s of %s (%s); trying again next cycle",
                    TVSHOW_NFO,
                    subscription.display_name,
                    error,
                )
        poster = folder / POSTER
        if subscription.avatar_url is not None and not poster.exists():
            try:
                image = self._artwork.download(subscription.avatar_url)
                file_atomically(self._media_library, poster, image)
            except (ArtworkUnavailable, OSError) as error:
                log.warning(
                    "Can't download the poster of %s (%s); trying again next cycle",
                    subscription.display_name,
                    error,
                )

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
                    description=subscription.description,
                    avatar_url=subscription.avatar_url,
                )
                action = "Started"
            elif not tracked.active:
                # Keep the name from first sight: the Channel folder never follows a rename.
                tracked = replace(
                    tracked,
                    active=True,
                    tracking_start=now,
                    description=subscription.description,
                    avatar_url=subscription.avatar_url,
                )
                action = "Restarted"
            else:
                details = replace(
                    tracked,
                    description=subscription.description,
                    avatar_url=subscription.avatar_url,
                )
                if details != tracked:
                    changed.append(details)
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
