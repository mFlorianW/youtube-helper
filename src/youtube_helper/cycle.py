# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The Cycle: everything the tool does every 30 minutes, behind a single "run one cycle" entry."""

import logging
from pathlib import Path

from youtube_helper.media_library import ensure_writable
from youtube_helper.state import StateStore

log = logging.getLogger(__name__)


class Cycle:
    def __init__(self, media_library: Path, state: StateStore) -> None:
        self._media_library = media_library
        self._state = state

    def run(self) -> None:
        """Run one cycle.

        Raises MediaLibraryUnwritable before doing anything if the Media library can't be written.
        """
        ensure_writable(self._media_library)
        log.info("Cycle started")
        log.info("Cycle finished")
