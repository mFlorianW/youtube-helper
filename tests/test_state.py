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


def test_videos_given_up_on_before_that_time_was_recorded_count_from_their_first_failure(
    state_dir: Path,
) -> None:
    before = MIGRATIONS.index("ALTER TABLE video ADD COLUMN gave_up TEXT")
    with closing(sqlite3.connect(state_dir / DATABASE_NAME, autocommit=False)) as old, old:
        for migration in MIGRATIONS[:before]:
            if isinstance(migration, str):
                old.execute(migration)
            else:
                migration(old)
        old.execute(
            "INSERT INTO subscription (channel_id, display_name, active, tracking_start)"
            " VALUES ('UC1', 'First', 1, '2026-10-01T12:00:00+00:00')"
        )
        old.execute(
            "INSERT INTO video (video_id, channel_id, title, released, status, last_error,"
            " first_failure) VALUES ('vid00000001', 'UC1', 'Doomed', '2026-10-02T12:00:00+00:00',"
            " 'failed', 'HTTP Error 503', '2026-10-02T13:00:00+00:00')"
        )
        old.execute(f"PRAGMA user_version = {before}")

    with closing(StateStore.open(state_dir)) as state:
        [problem] = state.download_problems(datetime(2026, 10, 17, tzinfo=UTC))["UC1"]

    assert problem.kind == "failed"
    assert problem.gave_up == datetime(2026, 10, 2, 13, 0, tzinfo=UTC)
