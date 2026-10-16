# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The SQLite state store that keeps all tool state in one file under the state directory."""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from youtube_helper.upload_feed import Upload

DATABASE_NAME = "youtube-helper.sqlite3"

# Schema changes, applied in order; the database's user_version counts those already applied.
# Append only: never edit or reorder an entry once it has been released.
MIGRATIONS = (
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
)


class StateUnavailable(Exception):
    def __init__(self, path: Path, reason: sqlite3.Error) -> None:
        super().__init__(f"State database {path} can't be opened: {reason}")


@dataclass(frozen=True)
class TrackedSubscription:
    """A Subscription as the tool knows it: inactive once the owner has unsubscribed.

    The display name is the one from first sight; later renames don't change it.
    """

    channel_id: str
    display_name: str
    active: bool
    tracking_start: datetime


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
            "SELECT channel_id, display_name, active, tracking_start FROM subscription"
            " ORDER BY rowid"
        )
        return [
            TrackedSubscription(
                channel_id, display_name, bool(active), datetime.fromisoformat(start)
            )
            for channel_id, display_name, active, start in rows
        ]

    def save_tracked_subscriptions(self, subscriptions: Iterable[TrackedSubscription]) -> None:
        """Insert or replace these Subscriptions, all at once."""
        with self._connection:
            self._connection.executemany(
                "INSERT INTO subscription (channel_id, display_name, active, tracking_start)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (channel_id) DO UPDATE SET"
                " display_name = excluded.display_name,"
                " active = excluded.active,"
                " tracking_start = excluded.tracking_start",
                [
                    (s.channel_id, s.display_name, s.active, s.tracking_start.isoformat())
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
        with self._connection:
            self._connection.execute(
                "UPDATE video SET status = 'downloaded' WHERE video_id = ?", (video_id,)
            )


def _migrate(connection: sqlite3.Connection) -> None:
    (version,) = connection.execute("PRAGMA user_version").fetchone()
    if version >= len(MIGRATIONS):
        return
    with connection:
        for migration in MIGRATIONS[version:]:
            connection.execute(migration)
        connection.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
