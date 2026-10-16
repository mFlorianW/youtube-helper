# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

import sqlite3
from contextlib import closing
from pathlib import Path

from youtube_helper.state import DATABASE_NAME, StateStore


def test_refresh_token_persists_across_restarts(state_dir: Path) -> None:
    state = StateStore.open(state_dir)
    state.save_refresh_token("refresh-token")
    state.close()

    reopened = StateStore.open(state_dir)
    try:
        assert reopened.refresh_token() == "refresh-token"
    finally:
        reopened.close()


def test_fresh_state_has_no_refresh_token(state: StateStore) -> None:
    assert state.refresh_token() is None


def test_subscriptions_tracked_before_channel_folders_were_stored_get_one_from_their_name(
    state_dir: Path,
) -> None:
    # A state file as the Subscription sync (schema version 2) left it.
    with closing(sqlite3.connect(state_dir / DATABASE_NAME, autocommit=True)) as old:
        old.execute("CREATE TABLE login (id INTEGER PRIMARY KEY, refresh_token TEXT NOT NULL)")
        old.execute(
            "CREATE TABLE subscription (channel_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,"
            " active INTEGER NOT NULL, tracking_start TEXT NOT NULL)"
        )
        old.execute(
            "INSERT INTO subscription VALUES ('UC1', 'AC/DC: Live', 1, '2026-10-16T12:00:00+00:00')"
        )
        old.execute("PRAGMA user_version = 2")

    with closing(StateStore.open(state_dir)) as state:
        [tracked] = state.tracked_subscriptions()

    assert tracked.channel_folder == "AC_DC - Live"
    assert tracked.display_name == "AC/DC: Live"
