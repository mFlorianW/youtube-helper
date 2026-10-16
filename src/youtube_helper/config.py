# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""Configuration read from the environment once at startup."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_STATE_DIR = Path("/state")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    media_library: Path
    state_dir: Path
    timezone: ZoneInfo


def load_config(environ: Mapping[str, str]) -> Config:
    media_library = environ.get("MEDIA_LIBRARY")
    if not media_library:
        raise ConfigError("MEDIA_LIBRARY is not set; point it at the Media library root")
    state_dir = environ.get("STATE_DIR") or DEFAULT_STATE_DIR
    timezone_name = (environ.get("TZ") or "UTC").removeprefix(":")
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ConfigError(f"TZ {timezone_name!r} is not a known time zone") from error
    return Config(Path(media_library), Path(state_dir), timezone)
