# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from youtube_helper.state import DATABASE_NAME, MIGRATIONS, StateStore


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


# The last schema with the owner's Google login. Released migrations never change, so applying
# them reproduces a state file that version left behind.
GOOGLE_SCHEMA_VERSION = 10


def test_state_left_by_the_google_login_keeps_its_subscriptions_and_drops_the_login(
    state_dir: Path,
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
        old.execute("INSERT INTO season VALUES ('UC1', 2026, 7)")

    with closing(StateStore.open(state_dir)) as state:
        first, second = state.tracked_subscriptions()
        next_episode = state.next_episode("UC1", 2026)
    with closing(sqlite3.connect(state_dir / DATABASE_NAME)) as migrated:
        tables = {name for (name,) in migrated.execute("SELECT name FROM sqlite_master")}

    assert (first.channel_id, first.channel_folder, first.active) == ("UC1", "First Channel", True)
    assert first.tracking_start == datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    assert (first.description, first.avatar_url) == ("About First", "https://yt3.ggpht.com/UC1")
    # Never refreshed by the tool itself: due right away.
    assert first.details_refreshed is None
    assert (second.channel_id, second.active) == ("UC2", False)
    assert next_episode == 8
    assert "login" not in tables
