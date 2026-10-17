# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""Command line entry point: runs the daemon."""

import argparse
import logging
import os
import signal
import sys
from collections.abc import Sequence
from types import FrameType
from typing import NoReturn

from youtube_helper.daemon import run_daemon

log = logging.getLogger(__name__)


def _stop(signum: int, frame: FrameType | None) -> NoReturn:
    log.info("Stopping")
    sys.exit(0)


def main(argv: Sequence[str] | None = None) -> NoReturn:
    parser = argparse.ArgumentParser(
        prog="youtube-helper",
        description="Run the daemon: the cycles and the web UI for managing the Subscriptions.",
    )
    parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # As PID 1 in a container, SIGTERM is ignored unless handled explicitly.
    signal.signal(signal.SIGTERM, _stop)
    sys.exit(run_daemon(os.environ))
