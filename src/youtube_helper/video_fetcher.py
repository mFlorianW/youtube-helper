# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The Video fetcher port: downloads one video, anonymously, with yt-dlp."""

import logging
from pathlib import Path
from typing import Any, Protocol

import yt_dlp
from yt_dlp.utils import YoutubeDLError

log = logging.getLogger(__name__)

WATCH_URL = "https://www.youtube.com/watch?v="

VIDEO_SUFFIX = ".mkv"
THUMBNAIL_SUFFIX = "-thumb.jpg"


class FetchFailed(Exception):
    """The video couldn't be downloaded."""


class VideoFetcher(Protocol):
    def fetch(self, video_id: str, base: Path) -> None:
        """Download the video as `<base>.mkv`, its thumbnail as `<base>-thumb.jpg`.

        The thumbnail may be missing if YouTube has none. Raises FetchFailed otherwise.
        """
        ...


class YtDlpFetcher:
    """Downloads with the embedded yt-dlp, anonymously: no cookies, ever."""

    def fetch(self, video_id: str, base: Path) -> None:
        try:
            with yt_dlp.YoutubeDL(_options(base)) as ydl:
                ydl.download([WATCH_URL + video_id])
        except YoutubeDLError as error:
            raise FetchFailed(str(error)) from error
        if not base.with_name(base.name + VIDEO_SUFFIX).is_file():
            raise FetchFailed(f"yt-dlp produced no {VIDEO_SUFFIX} file")


def _options(base: Path) -> dict[str, Any]:
    template = str(base).replace("%", "%%")
    return {
        "outtmpl": {
            "default": f"{template}.%(ext)s",
            # yt-dlp swaps in the thumbnail's own extension, then the convertor makes it .jpg.
            "thumbnail": f"{template}{THUMBNAIL_SUFFIX.removesuffix('.jpg')}.%(ext)s",
        },
        "noplaylist": True,
        # Best up to 1080p; at that height H.264 + AAC wins, otherwise whatever is best.
        "format": "bv*+ba/b",
        "format_sort": ["res:1080", "vcodec:h264", "acodec:aac"],
        "merge_output_format": "mkv",
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            {"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True},
        ],
        # The container may run without a writable home directory.
        "cachedir": False,
        "logger": log,
        "noprogress": True,
    }
