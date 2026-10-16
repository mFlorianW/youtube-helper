# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The SQLite state store that keeps all tool state in one file under the state directory."""

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from youtube_helper.media_library import channel_folder
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
)


class StateUnavailable(Exception):
    def __init__(self, path: Path, reason: sqlite3.Error) -> None:
        super().__init__(f"State database {path} can't be opened: {reason}")


@dataclass(frozen=True)
class TrackedSubscription:
    """A Subscription as the tool knows it: inactive once the owner has unsubscribed.

    The display name and the Channel folder are the ones from first sight; later renames don't
    change them.
    """

    channel_id: str
    display_name: str
    channel_folder: str
    active: bool
    tracking_start: datetime

    def tracks(self, upload: Upload) -> bool:
        """Whether `upload` is a New upload: released after the current tracking start."""
        return upload.released > self.tracking_start


class StateStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def open(cls, state_dir: Path) -> Self:
        """Open the state database in `state_dir`, creating or upgrading it as needed."""
        path = state_dir / DATABASE_NAME
        try:
            connection = sqlite3.connect(path, autocommit=False)
            _migrate(connection)
        except sqlite3.Error as error:
            raise StateUnavailable(path, error) from error
        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    def refresh_token(self) -> str | None:
        """The owner's OAuth refresh token, or None until `login` has been run."""
        row = self._connection.execute("SELECT refresh_token FROM login").fetchone()
        return None if row is None else str(row[0])

    def save_refresh_token(self, refresh_token: str) -> None:
        """Store the owner's OAuth refresh token, replacing any earlier one."""
        with self._connection:
            self._connection.execute(
                "INSERT INTO login (id, refresh_token) VALUES (1, ?)"
                " ON CONFLICT (id) DO UPDATE SET refresh_token = excluded.refresh_token",
                (refresh_token,),
            )

    def tracked_subscriptions(self) -> list[TrackedSubscription]:
        """Every Subscription ever seen, active or not, in the order first seen."""
        rows = self._connection.execute(
            "SELECT channel_id, display_name, channel_folder, active, tracking_start"
            " FROM subscription ORDER BY rowid"
        )
        return [
            TrackedSubscription(
                channel_id, display_name, folder, bool(active), datetime.fromisoformat(start)
            )
            for channel_id, display_name, folder, active, start in rows
        ]

    def save_tracked_subscriptions(self, subscriptions: Iterable[TrackedSubscription]) -> None:
        """Insert or replace these Subscriptions, all at once."""
        with self._connection:
            self._connection.executemany(
                "INSERT INTO subscription"
                " (channel_id, display_name, channel_folder, active, tracking_start)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (channel_id) DO UPDATE SET"
                " display_name = excluded.display_name,"
                " channel_folder = excluded.channel_folder,"
                " active = excluded.active,"
                " tracking_start = excluded.tracking_start",
                [
                    (
                        s.channel_id,
                        s.display_name,
                        s.channel_folder,
                        s.active,
                        s.tracking_start.isoformat(),
                    )
                    for s in subscriptions
                ],
            )

    def record_uploads(self, channel_id: str, uploads: Iterable[Upload]) -> None:
        """Remember these uploads as pending, unless they are already known."""
        with self._connection:
            self._connection.executemany(
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
        rows = self._connection.execute(
            "SELECT video_id, title, released FROM video"
            " WHERE channel_id = ? AND status = 'pending' ORDER BY released",
            (channel_id,),
        )
        return [
            Upload(video_id, title, datetime.fromisoformat(released))
            for video_id, title, released in rows
        ]

    def mark_downloaded(self, video_id: str) -> None:
        """Remember that the video is in the Media library, so it is never fetched again."""
        self._set_status(video_id, "downloaded")

    def mark_excluded(self, video_id: str) -> None:
        """Remember that the video is not a regular one, so it is never fetched again."""
        self._set_status(video_id, "excluded")

    def _set_status(self, video_id: str, status: str) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE video SET status = ? WHERE video_id = ?", (status, video_id)
            )

    def mark_skipped(self, video_id: str, error: str) -> None:
        """Remember that the video can never be downloaded, so it is never tried again."""
        with self._connection:
            self._connection.execute(
                "UPDATE video SET status = 'skipped', last_error = ? WHERE video_id = ?",
                (error, video_id),
            )

    def record_failure(self, video_id: str, error: str, at: datetime) -> datetime:
        """Remember that downloading the video failed at `at`; return when it first failed."""
        with self._connection:
            (first_failure,) = self._connection.execute(
                "UPDATE video SET last_error = ?, first_failure = COALESCE(first_failure, ?)"
                " WHERE video_id = ? RETURNING first_failure",
                (error, at.astimezone(UTC).isoformat(), video_id),
            ).fetchone()
        return datetime.fromisoformat(first_failure)

    def mark_failed(self, video_id: str) -> None:
        """Remember that the video kept failing for too long, so it is never tried again."""
        with self._connection:
            self._connection.execute(
                "UPDATE video SET status = 'failed' WHERE video_id = ?", (video_id,)
            )


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
