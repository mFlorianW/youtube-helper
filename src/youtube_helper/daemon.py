# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The daemon: runs one Cycle every 30 minutes until stopped or it can no longer continue."""

import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from types import FrameType
from typing import NoReturn

from youtube_helper.config import ConfigError, load_config
from youtube_helper.cycle import Cycle
from youtube_helper.media_library import MediaLibraryUnwritable, ensure_writable
from youtube_helper.state import StateStore, StateUnavailable

CYCLE_INTERVAL = timedelta(minutes=30)

log = logging.getLogger(__name__)


def run_daemon(environ: Mapping[str, str], *, sleep: Callable[[float], None] = time.sleep) -> int:
    """Run cycles forever; return a non-zero exit status when the daemon can't continue."""
    try:
        config = load_config(environ)
        ensure_writable(config.media_library)
    except (ConfigError, MediaLibraryUnwritable) as error:
        log.error("%s", error)
        return 1
    log.info(
        "Starting with Media library %s, state in %s, time zone %s",
        config.media_library,
        config.state_dir,
        config.timezone,
    )
    try:
        state = StateStore.open(config.state_dir)
    except StateUnavailable as error:
        log.error("%s", error)
        return 1
    try:
        cycle = Cycle(config.media_library, state)
        while True:
            cycle.run()
            sleep(CYCLE_INTERVAL.total_seconds())
    except MediaLibraryUnwritable as error:
        log.error("%s", error)
        return 1
    finally:
        state.close()


def _stop(signum: int, frame: FrameType | None) -> NoReturn:
    log.info("Stopping")
    sys.exit(0)


def cli() -> NoReturn:
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # As PID 1 in a container, SIGTERM is ignored unless handled explicitly.
    signal.signal(signal.SIGTERM, _stop)
    sys.exit(run_daemon(os.environ))
