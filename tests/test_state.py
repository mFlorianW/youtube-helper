# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

from pathlib import Path

from youtube_helper.state import StateStore


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
