# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The SQLite state store that keeps all tool state in one file under the state directory."""

import sqlite3
from pathlib import Path
from typing import Self

DATABASE_NAME = "youtube-helper.sqlite3"

# Schema changes, applied in order; the database's user_version counts those already applied.
# Append only: never edit or reorder an entry once it has been released.
MIGRATIONS = (
    "CREATE TABLE login (id INTEGER PRIMARY KEY CHECK (id = 1), refresh_token TEXT NOT NULL)",
)


class StateUnavailable(Exception):
    def __init__(self, path: Path, reason: sqlite3.Error) -> None:
        super().__init__(f"State database {path} can't be opened: {reason}")


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


def _migrate(connection: sqlite3.Connection) -> None:
    (version,) = connection.execute("PRAGMA user_version").fetchone()
    if version >= len(MIGRATIONS):
        return
    with connection:
        for migration in MIGRATIONS[version:]:
            connection.execute(migration)
        connection.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
