# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from youtube_helper.notifications import EmailSettings, PasswordState, Security
from youtube_helper.state import DATABASE_NAME, MIGRATIONS, StateStore

SECRET = b"the deployment's secret"

EMAIL = EmailSettings(
    enabled=True,
    new_videos=False,
    warnings=True,
    host="smtp.example.org",
    port=465,
    security=Security.TLS,
    username="owner",
    from_address="youtube-helper@example.org",
    recipients=("owner@example.org", "partner@example.org"),
)


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


def test_email_settings_before_any_were_saved_are_the_defaults(state: StateStore) -> None:
    saved = state.email_settings()

    assert saved.settings == EmailSettings()
    assert not saved.settings.enabled
    assert saved.settings.new_videos
    assert saved.settings.warnings
    assert saved.settings.security is Security.STARTTLS
    assert saved.settings.port == 587
    assert saved.password_state is PasswordState.UNSET


def test_email_settings_are_kept_with_the_password_stored_only_encrypted(state_dir: Path) -> None:
    with closing(StateStore.open(state_dir, secret_key=SECRET)) as state:
        state.save_email_settings(EMAIL, password="hunter2-correct-horse")

    with closing(StateStore.open(state_dir, secret_key=SECRET)) as state:
        saved = state.email_settings()

    assert saved.settings == EMAIL
    assert saved.password_state is PasswordState.SET
    assert saved.password == "hunter2-correct-horse"
    assert b"hunter2-correct-horse" not in (state_dir / DATABASE_NAME).read_bytes()


@pytest.mark.parametrize(
    "change",
    [
        {"host": "mail.example.net"},
        {"port": 2465},
        {"security": Security.STARTTLS},
        {"username": "someone-else"},
    ],
)
def test_changing_the_server_or_user_clears_the_password(
    state_dir: Path, change: dict[str, object]
) -> None:
    with closing(StateStore.open(state_dir, secret_key=SECRET)) as state:
        state.save_email_settings(EMAIL, password="hunter2")

        state.save_email_settings(replace(EMAIL, **change))  # type: ignore[arg-type]

        assert state.email_settings().password_state is PasswordState.UNSET


def test_changing_anything_else_keeps_the_password(state_dir: Path) -> None:
    with closing(StateStore.open(state_dir, secret_key=SECRET)) as state:
        state.save_email_settings(EMAIL, password="hunter2")

        state.save_email_settings(
            replace(
                EMAIL,
                enabled=False,
                new_videos=True,
                warnings=False,
                from_address="other@example.org",
                recipients=("third@example.org",),
            )
        )

        assert state.email_settings().password == "hunter2"


def test_a_new_password_with_a_new_server_is_kept(state_dir: Path) -> None:
    with closing(StateStore.open(state_dir, secret_key=SECRET)) as state:
        state.save_email_settings(EMAIL, password="hunter2")

        state.save_email_settings(replace(EMAIL, host="mail.example.net"), password="swordfish")

        assert state.email_settings().password == "swordfish"


def test_a_password_encrypted_under_another_key_is_reported_unreadable(state_dir: Path) -> None:
    with closing(StateStore.open(state_dir, secret_key=SECRET)) as state:
        state.save_email_settings(EMAIL, password="hunter2")

    with closing(StateStore.open(state_dir, secret_key=b"a rotated secret")) as state:
        saved = state.email_settings()

    assert saved.settings == EMAIL
    assert saved.password_state is PasswordState.UNREADABLE
    assert saved.password is None


def test_without_a_secret_key_no_password_can_be_stored_or_read(state_dir: Path) -> None:
    with closing(StateStore.open(state_dir, secret_key=SECRET)) as state:
        state.save_email_settings(EMAIL, password="hunter2")

    with closing(StateStore.open(state_dir)) as state:
        assert not state.can_store_password
        assert state.email_settings().password_state is PasswordState.UNREADABLE
        with pytest.raises(ValueError, match="No secret key"):
            state.save_email_settings(EMAIL, password="swordfish")


def test_feeds_failing_before_their_start_was_recorded_count_from_the_last_cycle(
    state_dir: Path,
) -> None:
    before = MIGRATIONS.index("ALTER TABLE feed_failure ADD COLUMN since TEXT")
    with closing(sqlite3.connect(state_dir / DATABASE_NAME, autocommit=False)) as old, old:
        for migration in MIGRATIONS[:before]:
            if isinstance(migration, str):
                old.execute(migration)
            else:
                migration(old)
        old.execute(
            "INSERT INTO cycle_outcome (id, started, finished, downloaded)"
            " VALUES (1, '2026-10-16T12:00:00+00:00', '2026-10-16T12:05:00+00:00', 0)"
        )
        old.execute("INSERT INTO feed_failure VALUES ('UC1', 'HTTP 500')")
        old.execute(f"PRAGMA user_version = {before}")

    with closing(StateStore.open(state_dir)) as state:
        assert state.feeds_failing_since() == {"UC1": datetime(2026, 10, 16, 12, 0, tzinfo=UTC)}
