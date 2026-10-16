# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The daemon: runs one Cycle every 30 minutes until stopped or it can no longer continue."""

import logging
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import timedelta

import httpx

from youtube_helper.account import GoogleAccount
from youtube_helper.artwork import HttpArtwork
from youtube_helper.config import ConfigError, load_config
from youtube_helper.cycle import Cycle
from youtube_helper.google import HTTP_TIMEOUT
from youtube_helper.media_library import MediaLibraryUnwritable, ensure_writable
from youtube_helper.state import StateStore, StateUnavailable
from youtube_helper.upload_feed import YouTubeFeed
from youtube_helper.video_fetcher import YtDlpFetcher

CYCLE_INTERVAL = timedelta(minutes=30)

log = logging.getLogger(__name__)


def run_daemon(environ: Mapping[str, str], *, sleep: Callable[[float], None] = time.sleep) -> int:
    """Run cycles forever; return a non-zero exit status when the daemon can't continue."""
    try:
        config = load_config(environ)
        ensure_writable(config.media_library)
        log.info(
            "Starting with Media library %s, state in %s, time zone %s",
            config.media_library,
            config.state_dir,
            config.timezone,
        )
        with (
            closing(StateStore.open(config.state_dir)) as state,
            httpx.Client(timeout=HTTP_TIMEOUT) as http,
        ):
            cycle = Cycle(
                config.media_library,
                state,
                account=GoogleAccount(state, http),
                feed=YouTubeFeed(http),
                fetcher=YtDlpFetcher(),
                artwork=HttpArtwork(http),
                timezone=config.timezone,
            )
            while True:
                cycle.run()
                sleep(CYCLE_INTERVAL.total_seconds())
    except (ConfigError, MediaLibraryUnwritable, StateUnavailable) as error:
        log.error("%s", error)
        return 1
