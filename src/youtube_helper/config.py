# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""Configuration read from the environment once at startup."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_STATE_DIR = Path("/state")
DEFAULT_WEB_PORT = 8080


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    media_library: Path
    state_dir: Path
    timezone: ZoneInfo
    web_port: int
    # The deployment's secret the SMTP password is encrypted with; None if there is none.
    secret_key: bytes | None = None


def load_config(environ: Mapping[str, str]) -> Config:
    media_library = environ.get("MEDIA_LIBRARY")
    if not media_library:
        raise ConfigError("MEDIA_LIBRARY is not set; point it at the Media library root")
    timezone_name = (environ.get("TZ") or "UTC").removeprefix(":")
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ConfigError(f"TZ {timezone_name!r} is not a known time zone") from error
    state_dir = Path(environ.get("STATE_DIR") or DEFAULT_STATE_DIR)
    return Config(
        Path(media_library), state_dir, timezone, _web_port(environ), _secret_key(environ)
    )


def _web_port(environ: Mapping[str, str]) -> int:
    value = environ.get("WEB_PORT") or str(DEFAULT_WEB_PORT)
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ConfigError(f"WEB_PORT {value!r} is not a port number (1-65535)")
    return int(value)


def _secret_key(environ: Mapping[str, str]) -> bytes | None:
    path = environ.get("SECRET_KEY_FILE")
    if not path:
        return None
    try:
        secret = Path(path).read_bytes().strip()
    except OSError as error:
        raise ConfigError(f"SECRET_KEY_FILE {path} can't be read: {error.strerror}") from error
    if not secret:
        raise ConfigError(f"SECRET_KEY_FILE {path} is empty")
    return secret
