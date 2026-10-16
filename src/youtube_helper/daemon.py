# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The daemon: serves the web UI and runs one Cycle every 30 minutes until stopped or it can no
longer continue."""

import logging
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import timedelta

import httpx

from youtube_helper.artwork import HttpArtwork
from youtube_helper.channels import YtDlpResolver
from youtube_helper.config import ConfigError, load_config
from youtube_helper.cycle import Cycle
from youtube_helper.media_library import MediaLibraryUnwritable, ensure_writable
from youtube_helper.state import StateStore, StateUnavailable
from youtube_helper.upload_feed import YouTubeFeed
from youtube_helper.video_fetcher import YtDlpFetcher
from youtube_helper.web import create_app, serve

CYCLE_INTERVAL = timedelta(minutes=30)

HTTP_TIMEOUT = 30


class WebUnavailable(Exception):
    def __init__(self, port: int, reason: OSError) -> None:
        super().__init__(f"Can't serve the web UI on port {port}: {reason}")


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
        resolver = YtDlpResolver()
        with (
            closing(StateStore.open(config.state_dir)) as state,
            httpx.Client(timeout=HTTP_TIMEOUT) as http,
        ):
            try:
                serve(create_app(state, resolver, timezone=config.timezone), config.web_port)
            except OSError as error:
                raise WebUnavailable(config.web_port, error) from error
            cycle = Cycle(
                config.media_library,
                state,
                resolver=resolver,
                feed=YouTubeFeed(http),
                fetcher=YtDlpFetcher(),
                artwork=HttpArtwork(http),
                timezone=config.timezone,
            )
            while True:
                cycle.run()
                sleep(CYCLE_INTERVAL.total_seconds())
    except (ConfigError, MediaLibraryUnwritable, StateUnavailable, WebUnavailable) as error:
        log.error("%s", error)
        return 1
