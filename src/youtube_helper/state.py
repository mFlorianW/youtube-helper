# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The SQLite state store that keeps all tool state in one file under the state directory."""

import sqlite3
from pathlib import Path
from typing import Self

DATABASE_NAME = "youtube-helper.sqlite3"


class StateUnavailable(Exception):
    def __init__(self, path: Path, reason: sqlite3.Error) -> None:
        super().__init__(f"State database {path} can't be opened: {reason}")
        self.path = path


class StateStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def open(cls, state_dir: Path) -> Self:
        """Open the state database in `state_dir`, creating it on first use."""
        path = state_dir / DATABASE_NAME
        try:
            connection = sqlite3.connect(path, autocommit=False)
            connection.execute("PRAGMA user_version")
        except sqlite3.Error as error:
            raise StateUnavailable(path, error) from error
        return cls(connection)

    def close(self) -> None:
        self._connection.close()
