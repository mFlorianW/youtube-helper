# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

import logging
import os
import re
from pathlib import Path

import pytest

from youtube_helper.cycle import Cycle
from youtube_helper.media_library import MediaLibraryUnwritable
from youtube_helper.state import StateStore


def test_cycle_with_writable_media_library_is_logged(
    media_library: Path, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    Cycle(media_library, state).run()

    assert "Cycle finished" in caplog.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to read-only directories")
def test_cycle_with_read_only_media_library_fails_naming_the_directory(
    media_library: Path, state: StateStore
) -> None:
    media_library.chmod(0o555)
    try:
        with pytest.raises(MediaLibraryUnwritable, match=re.escape(str(media_library))):
            Cycle(media_library, state).run()
    finally:
        media_library.chmod(0o755)


def test_cycle_with_missing_media_library_fails_naming_the_directory(
    tmp_path: Path, state: StateStore
) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(MediaLibraryUnwritable, match=re.escape(str(missing))):
        Cycle(missing, state).run()


def test_cycle_without_login_asks_for_login(
    media_library: Path, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    Cycle(media_library, state).run()

    assert "Not logged in, run `login`" in caplog.text


def test_cycle_after_login_does_not_ask_for_login(
    media_library: Path, state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    state.save_refresh_token("refresh-token")

    Cycle(media_library, state).run()

    assert "Not logged in" not in caplog.text
