# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The Media library: the admin-specified directory the tool files downloaded videos into.

Its layout is fixed:
`<Channel folder>/Season <YYYY>/<Channel> - <YYYY-MM-DD HH-MM> - <Title> [<videoid>].mkv`,
with the episode `.nfo` and the `-thumb.jpg` next to each video.
"""

import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from youtube_helper.upload_feed import Upload
from youtube_helper.video_fetcher import THUMBNAIL_SUFFIX, VIDEO_SUFFIX

# Hidden, so media servers skip it; inside the Media library, so filing is an atomic rename.
STAGING_DIR = ".youtube-helper-staging"

NFO_SUFFIX = ".nfo"

# Room for the longest suffix (`-thumb.jpg`) within the usual 255-byte filename limit.
MAX_BASENAME_BYTES = 240
MAX_CHANNEL_BYTES = 80

# Not allowed in filenames on Windows/SMB, plus every control character.
_FORBIDDEN = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')


class MediaLibraryUnwritable(Exception):
    def __init__(self, path: Path, reason: OSError) -> None:
        super().__init__(f"Media library {path} is not writable: {reason.strerror or reason}")


def ensure_writable(path: Path) -> None:
    """Prove `path` is writable by creating and removing a file in it.

    Permission bits alone can't tell (root, read-only mounts, ACLs), so this actually writes.
    Ownership and permissions are never changed; fixing them is the admin's job.
    """
    try:
        with tempfile.TemporaryFile(dir=path, prefix=".youtube-helper-write-check-"):
            pass
    except OSError as error:
        raise MediaLibraryUnwritable(path, error) from error


def channel_folder(display_name: str, channel_id: str) -> str:
    """The Channel folder's name for a channel display name (the one from first sight)."""
    return _sanitise(display_name, MAX_CHANNEL_BYTES) or channel_id


def video_base(media_library: Path, folder: str, upload: Upload, timezone: ZoneInfo) -> Path:
    """Where a video is filed, without the `.mkv` or `-thumb.jpg` suffix."""
    released = upload.released.astimezone(timezone)
    prefix = f"{folder} - {released:%Y-%m-%d %H-%M} - "
    suffix = f" [{upload.video_id}]"
    room = MAX_BASENAME_BYTES - len((prefix + suffix).encode())
    title = _sanitise(upload.title, room) or "Untitled"
    return media_library / folder / f"Season {released:%Y}" / f"{prefix}{title}{suffix}"


@contextmanager
def staged(media_library: Path, video_id: str) -> Iterator[Path]:
    """A base path in an empty staging directory to download to; removed afterwards."""
    directory = media_library / STAGING_DIR / video_id
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    try:
        yield directory / "video"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def file_video(staged_base: Path, base: Path) -> None:
    """Move a staged download to its final name, the video last.

    Each file appears complete under its final name or not at all.
    """
    base.parent.mkdir(parents=True, exist_ok=True)
    for suffix in (THUMBNAIL_SUFFIX, NFO_SUFFIX, VIDEO_SUFFIX):
        staged_file = staged_base.with_name(staged_base.name + suffix)
        if staged_file.exists():
            staged_file.replace(base.with_name(base.name + suffix))


def _sanitise(text: str, max_bytes: int) -> str:
    """`text` made safe as one path component on any filesystem, at most `max_bytes` long."""
    text = " ".join(text.split()).replace(": ", " - ")
    text = _FORBIDDEN.sub(lambda match: "-" if match[0] == ":" else "_", text)
    text = text.encode()[:max_bytes].decode(errors="ignore")
    # Leading dots hide files; trailing dots and spaces are dropped on Windows.
    return text.lstrip(". ").rstrip(". ")
