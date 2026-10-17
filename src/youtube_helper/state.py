# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The SQLite state store that keeps all tool state in one file under the state directory."""

import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Self

from youtube_helper.channels import Channel
from youtube_helper.media_library import channel_folder
from youtube_helper.notifications import (
    Changes,
    EmailSettings,
    FiledVideo,
    LastingWarning,
    NotificationFailure,
    PasswordCipher,
    PasswordState,
    SavedEmailSettings,
    Security,
    VideoWarning,
    feed_warning_key,
)
from youtube_helper.upload_feed import Upload

DATABASE_NAME = "youtube-helper.sqlite3"


def _name_channel_folders(connection: sqlite3.Connection) -> None:
    """Give every Subscription tracked so far the Channel folder of its first-seen name."""
    rows = connection.execute("SELECT channel_id, display_name FROM subscription").fetchall()
    connection.executemany(
        "UPDATE subscription SET channel_folder = ? WHERE channel_id = ?",
        [
            (channel_folder(display_name, channel_id), channel_id)
            for channel_id, display_name in rows
        ],
    )


# Schema changes, applied in order; the database's user_version counts those already applied.
# Append only: never edit or reorder an entry once it has been released.
MIGRATIONS: tuple[str | Callable[[sqlite3.Connection], None], ...] = (
    "CREATE TABLE login (id INTEGER PRIMARY KEY CHECK (id = 1), refresh_token TEXT NOT NULL)",
    "CREATE TABLE subscription ("
    " channel_id TEXT PRIMARY KEY,"
    " display_name TEXT NOT NULL,"
    " active INTEGER NOT NULL,"
    " tracking_start TEXT NOT NULL)",
    "CREATE TABLE video ("
    " video_id TEXT PRIMARY KEY,"
    " channel_id TEXT NOT NULL,"
    " title TEXT NOT NULL,"
    " released TEXT NOT NULL,"
    " status TEXT NOT NULL)",
    "ALTER TABLE subscription ADD COLUMN channel_folder TEXT NOT NULL DEFAULT ''",
    _name_channel_folders,
    "ALTER TABLE video ADD COLUMN last_error TEXT",
    "ALTER TABLE video ADD COLUMN first_failure TEXT",
    "CREATE TABLE season ("
    " channel_id TEXT NOT NULL,"
    " season INTEGER NOT NULL,"
    " last_episode INTEGER NOT NULL,"
    " PRIMARY KEY (channel_id, season))",
    "ALTER TABLE subscription ADD COLUMN description TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE subscription ADD COLUMN avatar_url TEXT",
    # The owner's Google login is gone with Google: the Subscriptions are the tool's own now.
    "DROP TABLE login",
    "ALTER TABLE subscription ADD COLUMN details_refreshed TEXT",
    "ALTER TABLE video ADD COLUMN gave_up TEXT",
    # Close enough for the videos given up on before it was recorded; skipped ones have no time.
    "UPDATE video SET gave_up = first_failure WHERE status = 'failed'",
    "CREATE TABLE cycle_outcome ("
    " id INTEGER PRIMARY KEY CHECK (id = 1),"
    " started TEXT NOT NULL,"
    " finished TEXT NOT NULL,"
    " downloaded INTEGER NOT NULL,"
    " unwritable TEXT,"
    " free_space INTEGER,"
    " free_space_threshold INTEGER,"
    " failed_downloads INTEGER,"
    " last_download_error TEXT,"
    " bot_checks INTEGER)",
    "CREATE TABLE feed_failure (channel_id TEXT PRIMARY KEY, error TEXT NOT NULL)",
    "CREATE TABLE email_settings ("
    " id INTEGER PRIMARY KEY CHECK (id = 1),"
    " enabled INTEGER NOT NULL,"
    " new_videos INTEGER NOT NULL,"
    " warnings INTEGER NOT NULL,"
    " host TEXT NOT NULL,"
    " port INTEGER NOT NULL,"
    " security TEXT NOT NULL,"
    " username TEXT NOT NULL,"
    " password TEXT,"
    " from_address TEXT NOT NULL,"
    " recipients TEXT NOT NULL)",
    # Filed into the Media library, but not yet reported in a delivered Notification.
    "ALTER TABLE video ADD COLUMN unreported INTEGER NOT NULL DEFAULT 0",
    "CREATE TABLE notification_failure ("
    " id INTEGER PRIMARY KEY CHECK (id = 1),"
    " since TEXT NOT NULL,"
    " warning TEXT NOT NULL)",
    "ALTER TABLE feed_failure ADD COLUMN since TEXT",
    "UPDATE feed_failure SET since = (SELECT started FROM cycle_outcome)",
    # Given up on or skipped, but not yet reported in a delivered Notification.
    "ALTER TABLE video ADD COLUMN warning_unreported INTEGER NOT NULL DEFAULT 0",
    # The lasting Warnings active as of the last delivered Notification.
    "CREATE TABLE reported_warning (key TEXT PRIMARY KEY, title TEXT NOT NULL)",
)

# How long videos given up or skipped are still shown as problems.
PROBLEM_WINDOW = timedelta(days=30)


class StateUnavailable(Exception):
    def __init__(self, path: Path, reason: sqlite3.Error) -> None:
        super().__init__(f"State database {path} can't be opened: {reason}")


@dataclass(frozen=True)
class TrackedSubscription:
    """A Subscription as the tool knows it: inactive once the owner has unsubscribed.

    The display name and the Channel folder are the ones from first sight; later renames don't
    change them. The description and avatar URL are the ones last seen, at `details_refreshed`
    (None if not since the Subscriptions came from the Google account).
    """

    channel_id: str
    display_name: str
    channel_folder: str
    active: bool
    tracking_start: datetime
    description: str
    avatar_url: str | None
    details_refreshed: datetime | None

    def tracks(self, upload: Upload) -> bool:
        """Whether `upload` is a New upload: released after the current tracking start."""
        return upload.released > self.tracking_start


@dataclass(frozen=True)
class LowSpace:
    """Downloads paused: the Media library had only `free` bytes, less than `threshold`."""

    free: int
    threshold: int


@dataclass(frozen=True)
class AllDownloadsFailed:
    """Every download a cycle attempted failed in yt-dlp, which is then likely outdated."""

    attempts: int
    last_error: str
    bot_checks: int


@dataclass(frozen=True)
class CycleOutcome:
    """What the last cycle did and the problems it found.

    `unwritable` names the Media library and why it can't be written; the cycle was skipped then.
    `feed_failures` holds, per channel ID, why that Subscription's feed couldn't be read.
    """

    started: datetime
    finished: datetime
    downloaded: int = 0
    unwritable: str | None = None
    low_space: LowSpace | None = None
    all_downloads_failed: AllDownloadsFailed | None = None
    feed_failures: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DownloadProblem:
    """A video being retried since `first_failure`, given up on (`failed`) or `skipped` for good.

    `gave_up` is when it was given up on or skipped; None while it is retried.
    """

    video_id: str
    title: str
    kind: Literal["retrying", "failed", "skipped"]
    error: str
    first_failure: datetime | None
    gave_up: datetime | None


class Subscribed(Enum):
    STARTED = "Started"
    RESTARTED = "Restarted"
    ALREADY_ACTIVE = "Already tracking"


class StateStore:
    """Shared by the daemon's threads: each method runs alone, in a transaction of its own."""

    def __init__(self, connection: sqlite3.Connection, cipher: PasswordCipher | None) -> None:
        self._connection = connection
        self._cipher = cipher
        self._lock = threading.Lock()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # Reads end their transaction too, so none stays open while the caller downloads.
        with self._lock, self._connection:
            yield self._connection

    @classmethod
    def open(cls, state_dir: Path, *, secret_key: bytes | None = None) -> Self:
        """Open the state database in `state_dir`, creating or upgrading it as needed.

        The SMTP password can only be stored and read with the deployment's `secret_key`.
        """
        path = state_dir / DATABASE_NAME
        try:
            connection = sqlite3.connect(path, autocommit=False, check_same_thread=False)
            _migrate(connection)
        except sqlite3.Error as error:
            raise StateUnavailable(path, error) from error
        return cls(connection, None if secret_key is None else PasswordCipher(secret_key))

    def close(self) -> None:
        self._connection.close()

    def tracked_subscriptions(self) -> list[TrackedSubscription]:
        """Every Subscription ever made, active or not, in the order first made."""
        with self._transaction() as connection:
            rows = connection.execute(_SELECT_SUBSCRIPTION + " ORDER BY rowid").fetchall()
        return [_tracked(row) for row in rows]

    def subscribe(self, channel: Channel, at: datetime) -> tuple[Subscribed, TrackedSubscription]:
        """Track `channel` from `at`, unless it is tracked already.

        A channel subscribed to before keeps the name and Channel folder it was first seen with.
        """
        with self._transaction() as connection:
            row = connection.execute(
                _SELECT_SUBSCRIPTION + " WHERE channel_id = ?", (channel.channel_id,)
            ).fetchone()
            if row is None:
                outcome = Subscribed.STARTED
                tracked = TrackedSubscription(
                    channel_id=channel.channel_id,
                    display_name=channel.display_name,
                    channel_folder=channel_folder(channel.display_name, channel.channel_id),
                    active=True,
                    tracking_start=at,
                    description=channel.description,
                    avatar_url=channel.avatar_url,
                    details_refreshed=at,
                )
            elif (known := _tracked(row)).active:
                return Subscribed.ALREADY_ACTIVE, known
            else:
                outcome = Subscribed.RESTARTED
                tracked = replace(
                    known,
                    active=True,
                    tracking_start=at,
                    description=channel.description,
                    avatar_url=channel.avatar_url,
                    details_refreshed=at,
                )
            connection.execute(
                "INSERT INTO subscription"
                " (channel_id, display_name, channel_folder, active, tracking_start,"
                " description, avatar_url, details_refreshed)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (channel_id) DO UPDATE SET"
                " active = excluded.active,"
                " tracking_start = excluded.tracking_start,"
                " description = excluded.description,"
                " avatar_url = excluded.avatar_url,"
                " details_refreshed = excluded.details_refreshed",
                (
                    tracked.channel_id,
                    tracked.display_name,
                    tracked.channel_folder,
                    tracked.active,
                    tracked.tracking_start.isoformat(),
                    tracked.description,
                    tracked.avatar_url,
                    at.isoformat(),
                ),
            )
        return outcome, tracked

    def unsubscribe(self, channel_id: str) -> TrackedSubscription | None:
        """Stop tracking the channel; return it, or None if it wasn't tracked."""
        with self._transaction() as connection:
            row = connection.execute(
                _SELECT_SUBSCRIPTION + " WHERE channel_id = ? AND active", (channel_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE subscription SET active = 0 WHERE channel_id = ?", (channel_id,)
            )
            # Its Warnings disappear silently: none is reported, not even as cleared.
            connection.execute(
                "UPDATE video SET warning_unreported = 0 WHERE channel_id = ?", (channel_id,)
            )
            connection.execute(
                "DELETE FROM reported_warning WHERE key = ?", (feed_warning_key(channel_id),)
            )
            # Subscribing again starts the 24 hours afresh.
            connection.execute("DELETE FROM feed_failure WHERE channel_id = ?", (channel_id,))
        return replace(_tracked(row), active=False)

    def refresh_details(self, channel: Channel, at: datetime) -> None:
        """Remember the channel's current description and avatar, as seen at `at`."""
        with self._transaction() as connection:
            connection.execute(
                "UPDATE subscription SET description = ?, avatar_url = ?, details_refreshed = ?"
                " WHERE channel_id = ?",
                (channel.description, channel.avatar_url, at.isoformat(), channel.channel_id),
            )

    def record_uploads(self, channel_id: str, uploads: Iterable[Upload]) -> None:
        """Remember these uploads as pending, unless they are already known."""
        with self._transaction() as connection:
            connection.executemany(
                "INSERT INTO video (video_id, channel_id, title, released, status)"
                " VALUES (?, ?, ?, ?, 'pending')"
                " ON CONFLICT (video_id) DO NOTHING",
                [
                    (u.video_id, channel_id, u.title, u.released.astimezone(UTC).isoformat())
                    for u in uploads
                ],
            )

    def pending_uploads(self, channel_id: str) -> list[Upload]:
        """The channel's uploads not downloaded yet, oldest first."""
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT video_id, title, released FROM video"
                " WHERE channel_id = ? AND status = 'pending' ORDER BY released",
                (channel_id,),
            ).fetchall()
        return [
            Upload(video_id, title, datetime.fromisoformat(released))
            for video_id, title, released in rows
        ]

    def next_episode(self, channel_id: str, season: int) -> int:
        """The episode number the channel's next video filed into `season` gets."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT last_episode FROM season WHERE channel_id = ? AND season = ?",
                (channel_id, season),
            ).fetchone()
        return 1 if row is None else int(row[0]) + 1

    def mark_downloaded(self, video_id: str, channel_id: str, season: int, episode: int) -> None:
        """Remember that the video is in the Media library as this episode of `season`.

        It is never fetched again, and its episode number is never handed out again. It is
        unreported until a Notification reporting it is delivered.
        """
        with self._transaction() as connection:
            connection.execute(
                "UPDATE video SET status = 'downloaded', unreported = 1 WHERE video_id = ?",
                (video_id,),
            )
            connection.execute(
                "INSERT INTO season (channel_id, season, last_episode) VALUES (?, ?, ?)"
                " ON CONFLICT (channel_id, season) DO UPDATE"
                " SET last_episode = max(last_episode, excluded.last_episode)",
                (channel_id, season, episode),
            )

    def mark_excluded(self, video_id: str) -> None:
        """Remember that the video is not a regular one, so it is never fetched again."""
        with self._transaction():
            self._set_status(video_id, "excluded")

    def _set_status(self, video_id: str, status: str) -> None:
        self._connection.execute(
            "UPDATE video SET status = ? WHERE video_id = ?", (status, video_id)
        )

    def mark_skipped(self, video_id: str, error: str, at: datetime) -> None:
        """Remember that the video can never be downloaded, so it is never tried again."""
        with self._transaction() as connection:
            connection.execute(
                "UPDATE video SET status = 'skipped', last_error = ?, gave_up = ?,"
                " warning_unreported = 1 WHERE video_id = ?",
                (error, _utc(at), video_id),
            )

    def record_failure(self, video_id: str, error: str, at: datetime) -> datetime:
        """Remember that downloading the video failed at `at`; return when it first failed."""
        with self._transaction() as connection:
            (first_failure,) = connection.execute(
                "UPDATE video SET last_error = ?, first_failure = COALESCE(first_failure, ?)"
                " WHERE video_id = ? RETURNING first_failure",
                (error, _utc(at), video_id),
            ).fetchone()
        return datetime.fromisoformat(first_failure)

    def mark_failed(self, video_id: str, at: datetime) -> None:
        """Remember that the video kept failing for too long, so it is never tried again."""
        with self._transaction() as connection:
            connection.execute(
                "UPDATE video SET status = 'failed', gave_up = ?, warning_unreported = 1"
                " WHERE video_id = ?",
                (_utc(at), video_id),
            )

    def download_problems(self, now: datetime) -> dict[str, list[DownloadProblem]]:
        """The active Subscriptions' problem videos by channel ID, oldest first.

        Those being retried, and those given up on or skipped within PROBLEM_WINDOW before `now`.
        Excluded videos are no problem, nor are uploads left pending outside the tracking.
        """
        with self._transaction() as connection:
            subscriptions = {
                s.channel_id: s
                for s in map(_tracked, connection.execute(_SELECT_SUBSCRIPTION + " WHERE active"))
            }
            rows = connection.execute(
                "SELECT channel_id, video_id, title, released, status, last_error, first_failure,"
                " gave_up FROM video"
                " WHERE (status = 'pending' AND first_failure IS NOT NULL)"
                " OR (status IN ('failed', 'skipped') AND gave_up >= ?)"
                " ORDER BY released",
                (_utc(now - PROBLEM_WINDOW),),
            ).fetchall()
        problems: dict[str, list[DownloadProblem]] = {}
        for channel_id, video_id, title, released, status, error, first, gave_up in rows:
            subscription = subscriptions.get(channel_id)
            if subscription is None:
                continue
            upload = Upload(video_id, title, datetime.fromisoformat(released))
            if status == "pending" and not subscription.tracks(upload):
                continue
            problems.setdefault(channel_id, []).append(
                DownloadProblem(
                    video_id,
                    title,
                    "retrying" if status == "pending" else status,
                    error or "",
                    _moment(first),
                    _moment(gave_up),
                )
            )
        return problems

    def record_cycle(self, outcome: CycleOutcome) -> None:
        """Remember `outcome` as the last cycle's, in place of the one before.

        A feed that failed before keeps failing since then; one failing anew since this cycle.
        """
        failed = outcome.all_downloads_failed
        low_space = outcome.low_space
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO cycle_outcome"
                " (id, started, finished, downloaded, unwritable, free_space,"
                " free_space_threshold, failed_downloads, last_download_error, bot_checks)"
                " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _utc(outcome.started),
                    _utc(outcome.finished),
                    outcome.downloaded,
                    outcome.unwritable,
                    None if low_space is None else low_space.free,
                    None if low_space is None else low_space.threshold,
                    None if failed is None else failed.attempts,
                    None if failed is None else failed.last_error,
                    None if failed is None else failed.bot_checks,
                ),
            )
            since = dict(connection.execute("SELECT channel_id, since FROM feed_failure"))
            connection.execute("DELETE FROM feed_failure")
            connection.executemany(
                "INSERT INTO feed_failure (channel_id, error, since) VALUES (?, ?, ?)",
                [
                    (channel_id, error, since.get(channel_id) or _utc(outcome.started))
                    for channel_id, error in outcome.feed_failures.items()
                ],
            )

    def feeds_failing_since(self) -> dict[str, datetime]:
        """Since when each Subscription's feed that couldn't be read in the last cycle fails,
        by channel ID."""
        with self._transaction() as connection:
            rows = connection.execute("SELECT channel_id, since FROM feed_failure").fetchall()
        return {channel_id: datetime.fromisoformat(since) for channel_id, since in rows}

    def last_cycle(self) -> CycleOutcome | None:
        """The outcome of the last cycle, or None if none has run yet."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT started, finished, downloaded, unwritable, free_space,"
                " free_space_threshold, failed_downloads, last_download_error, bot_checks"
                " FROM cycle_outcome"
            ).fetchone()
            feed_failures = dict(connection.execute("SELECT channel_id, error FROM feed_failure"))
        if row is None:
            return None
        started, finished, downloaded, unwritable, free, threshold, attempts, error, bot_checks = (
            row
        )
        return CycleOutcome(
            datetime.fromisoformat(started),
            datetime.fromisoformat(finished),
            downloaded,
            unwritable,
            None if free is None else LowSpace(free, threshold),
            None if attempts is None else AllDownloadsFailed(attempts, error, bot_checks),
            feed_failures,
        )

    def unreported_videos(self) -> list[FiledVideo]:
        """The videos filed and not reported yet, by Subscription in the order first made,
        oldest first."""
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT video_id, video.channel_id, display_name, title, released"
                " FROM video JOIN subscription USING (channel_id)"
                " WHERE unreported ORDER BY subscription.rowid, released"
            ).fetchall()
        return [
            FiledVideo(video_id, channel_id, subscription, title, datetime.fromisoformat(released))
            for video_id, channel_id, subscription, title, released in rows
        ]

    def unreported_video_warnings(self) -> list[VideoWarning]:
        """The videos given up on or skipped and not reported yet, by Subscription in the order
        first made, oldest first."""
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT video_id, display_name, title, status, last_error"
                " FROM video JOIN subscription USING (channel_id)"
                " WHERE warning_unreported ORDER BY subscription.rowid, released"
            ).fetchall()
        return [
            VideoWarning(video_id, subscription, title, status == "failed", error or "")
            for video_id, subscription, title, status, error in rows
        ]

    def reported_warnings(self) -> dict[str, str]:
        """The titles of the lasting Warnings active as of the last delivered Notification, by
        key, in the order raised."""
        with self._transaction() as connection:
            return dict(
                connection.execute("SELECT key, title FROM reported_warning ORDER BY rowid")
            )

    def notification_delivered(
        self, changes: Changes, active: Iterable[LastingWarning] | None = None
    ) -> None:
        """Remember that a Notification of `changes` was delivered: Notifications work again.

        `active` are the lasting Warnings active as of it, None if it doesn't report Warnings.
        """
        with self._transaction() as connection:
            connection.executemany(
                "UPDATE video SET unreported = 0 WHERE video_id = ?",
                [(video.video_id,) for video in changes.filed],
            )
            connection.executemany(
                "UPDATE video SET warning_unreported = 0 WHERE video_id = ?",
                [(video.video_id,) for video in changes.videos],
            )
            if active is not None:
                connection.execute("DELETE FROM reported_warning")
                connection.executemany(
                    "INSERT INTO reported_warning (key, title) VALUES (?, ?)",
                    [(warning.key, warning.title) for warning in active],
                )
            connection.execute("DELETE FROM notification_failure")

    def notification_failed(self, warning: str, at: datetime) -> None:
        """Remember that a Notification couldn't be sent at `at`, and why, as a Warning.

        Notifications count as failing since the first failure after the last one delivered.
        """
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO notification_failure (id, since, warning) VALUES (1, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET warning = excluded.warning",
                (_utc(at), warning),
            )

    def clear_notification_failure(self) -> None:
        """Forget that Notifications were failing: none are sent any more."""
        with self._transaction() as connection:
            connection.execute("DELETE FROM notification_failure")

    def notification_failure(self) -> NotificationFailure | None:
        """Since when and why Notifications are failing, or None if they aren't."""
        with self._transaction() as connection:
            row = connection.execute("SELECT since, warning FROM notification_failure").fetchone()
        if row is None:
            return None
        since, warning = row
        return NotificationFailure(datetime.fromisoformat(since), warning)

    @property
    def can_store_password(self) -> bool:
        """Whether there is a key to encrypt the SMTP password with."""
        return self._cipher is not None

    def email_settings(self) -> SavedEmailSettings:
        """The email settings last saved, or the defaults if none were."""
        with self._transaction() as connection:
            row = connection.execute(_SELECT_EMAIL_SETTINGS).fetchone()
        if row is None:
            return SavedEmailSettings(EmailSettings())
        settings, token = _email_settings(row)
        if token is None:
            return SavedEmailSettings(settings)
        password = None if self._cipher is None else self._cipher.decrypt(token)
        if password is None:
            return SavedEmailSettings(settings, PasswordState.UNREADABLE)
        return SavedEmailSettings(settings, PasswordState.SET, password)

    def save_email_settings(self, settings: EmailSettings, password: str | None = None) -> None:
        """Remember `settings`, with `password` if given, or else with the stored password.

        The stored password is dropped when the host, port, security or username change: the
        tool decrypts it to send it, so it must not follow the settings to someone else's server.
        Switching on Notifications of new videos counts every video filed so far as reported;
        switching on those of Warnings, every video given up on so far, while the lasting
        Warnings active then are reported as raised.
        """
        if password is not None and self._cipher is None:
            raise ValueError("No secret key to encrypt the SMTP password with")
        with self._transaction() as connection:
            token = None
            stored = EmailSettings()
            if row := connection.execute(_SELECT_EMAIL_SETTINGS).fetchone():
                stored, stored_token = _email_settings(row)
                if stored.same_server(settings):
                    token = stored_token
            if settings.reports_new_videos and not stored.reports_new_videos:
                connection.execute("UPDATE video SET unreported = 0")
                connection.execute("DELETE FROM notification_failure")
            if settings.reports_warnings and not stored.reports_warnings:
                connection.execute("UPDATE video SET warning_unreported = 0")
                connection.execute("DELETE FROM reported_warning")
            if password is not None and self._cipher is not None:
                token = self._cipher.encrypt(password)
            connection.execute(
                "INSERT OR REPLACE INTO email_settings"
                " (id, enabled, new_videos, warnings, host, port, security, username, password,"
                " from_address, recipients) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    settings.enabled,
                    settings.new_videos,
                    settings.warnings,
                    settings.host,
                    settings.port,
                    settings.security.value,
                    settings.username,
                    token,
                    settings.from_address,
                    "\n".join(settings.recipients),
                ),
            )


_SELECT_EMAIL_SETTINGS = (
    "SELECT enabled, new_videos, warnings, host, port, security, username, password,"
    " from_address, recipients FROM email_settings"
)


def _email_settings(row: tuple[Any, ...]) -> tuple[EmailSettings, str | None]:
    """The settings and the encrypted password in a row selected with _SELECT_EMAIL_SETTINGS."""
    (
        enabled,
        new_videos,
        warnings,
        host,
        port,
        security,
        username,
        token,
        from_address,
        recipients,
    ) = row
    settings = EmailSettings(
        enabled=bool(enabled),
        new_videos=bool(new_videos),
        warnings=bool(warnings),
        host=host,
        port=port,
        security=Security(security),
        username=username,
        from_address=from_address,
        recipients=tuple(recipients.split("\n")) if recipients else (),
    )
    return settings, token


_SELECT_SUBSCRIPTION = (
    "SELECT channel_id, display_name, channel_folder, active, tracking_start,"
    " description, avatar_url, details_refreshed FROM subscription"
)


def _tracked(row: tuple[Any, ...]) -> TrackedSubscription:
    """The TrackedSubscription in a row selected with _SELECT_SUBSCRIPTION."""
    channel_id, display_name, folder, active, start, description, avatar_url, refreshed = row
    return TrackedSubscription(
        channel_id,
        display_name,
        folder,
        bool(active),
        datetime.fromisoformat(start),
        description,
        avatar_url,
        _moment(refreshed),
    )


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _moment(stored: str | None) -> datetime | None:
    return None if stored is None else datetime.fromisoformat(stored)


def _migrate(connection: sqlite3.Connection) -> None:
    (version,) = connection.execute("PRAGMA user_version").fetchone()
    if version >= len(MIGRATIONS):
        return
    with connection:
        for migration in MIGRATIONS[version:]:
            if isinstance(migration, str):
                connection.execute(migration)
            else:
                migration(connection)
        connection.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
