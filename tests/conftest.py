# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

from collections.abc import Iterator
from pathlib import Path

import pytest

from youtube_helper.state import StateStore


@pytest.fixture
def media_library(tmp_path: Path) -> Path:
    library = tmp_path / "library"
    library.mkdir()
    return library


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir()
    return directory


@pytest.fixture
def state(state_dir: Path) -> Iterator[StateStore]:
    store = StateStore.open(state_dir)
    yield store
    store.close()
