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


class NotRegularVideo(Exception):
    """The video is a Short or a livestream, running or recorded: never for the Media library."""


class NotAiredYet(Exception):
    """The video is a premiere that hasn't aired yet; it can be fetched once it has."""


class VideoFetcher(Protocol):
    def fetch(self, video_id: str, base: Path) -> None:
        """Download the video as `<base>.mkv`, its thumbnail as `<base>-thumb.jpg`.

        Only regular videos are downloaded: raises NotRegularVideo for a Short or a livestream,
        NotAiredYet for a premiere that hasn't aired yet. The thumbnail may be missing if YouTube
        has none. Raises FetchFailed when the download fails.
        """
        ...


class YtDlpFetcher:
    """Downloads with the embedded yt-dlp, anonymously: no cookies, ever."""

    def fetch(self, video_id: str, base: Path) -> None:
        try:
            # Look before downloading. Without formats to tolerate, an upcoming premiere would
            # fail extraction before telling what it is.
            with yt_dlp.YoutubeDL(_options(base) | {"ignore_no_formats_error": True}) as ydl:
                info = ydl.extract_info(WATCH_URL + video_id, download=False, process=False)
            _ensure_regular(info)
            with yt_dlp.YoutubeDL(_options(base)) as ydl:
                ydl.process_ie_result(info, download=True)
        except YoutubeDLError as error:
            raise FetchFailed(str(error)) from error
        if not base.with_name(base.name + VIDEO_SUFFIX).is_file():
            raise FetchFailed(f"yt-dlp produced no {VIDEO_SUFFIX} file")


def _ensure_regular(info: dict[str, Any]) -> None:
    """Raise unless yt-dlp's metadata `info` describes a regular video that has aired."""
    media_type = info.get("media_type")
    live_status = info.get("live_status")
    if media_type == "short":
        raise NotRegularVideo("a Short")
    # YouTube marks scheduled, running and recorded live streams alike as live content.
    if media_type == "livestream":
        if live_status in ("is_live", "is_upcoming", "post_live"):
            raise NotRegularVideo("a running live stream")
        raise NotRegularVideo("a livestream recording")
    # Premieres aren't live content, yet are upcoming and then live until they have aired.
    if live_status == "is_upcoming":
        raise NotAiredYet("upcoming")
    if live_status in ("is_live", "post_live"):
        raise NotAiredYet("still airing")


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
