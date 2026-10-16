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
    """The video couldn't be downloaded this time; trying again later may work."""


class BotCheck(FetchFailed):
    """YouTube asked to confirm that the download isn't made by a bot; may pass later."""


class VideoUnavailable(FetchFailed):
    """The video can never be downloaded: removed, private, members-only or age-gated."""


class VideoFetcher(Protocol):
    def fetch(self, video_id: str, base: Path) -> None:
        """Download the video as `<base>.mkv`, its thumbnail as `<base>-thumb.jpg`.

        The thumbnail may be missing if YouTube has none. Raises VideoUnavailable if the video
        can never be downloaded, BotCheck if YouTube asked for a bot check, FetchFailed otherwise.
        """
        ...


class YtDlpFetcher:
    """Downloads with the embedded yt-dlp, anonymously: no cookies, ever."""

    def fetch(self, video_id: str, base: Path) -> None:
        try:
            with yt_dlp.YoutubeDL(_options(base)) as ydl:
                ydl.download([WATCH_URL + video_id])
        except YoutubeDLError as error:
            raise _classify(str(error)) from error
        if not base.with_name(base.name + VIDEO_SUFFIX).is_file():
            raise FetchFailed(f"yt-dlp produced no {VIDEO_SUFFIX} file")


# Fragments of YouTube's reasons, as yt-dlp passes them on, lower-cased.
_BOT_CHECK_REASONS = ("not a bot", "captcha")
# YouTube's rate limit also starts with "Video unavailable", but passes.
_TEMPORARY_REASONS = ("try again later",)
_PERMANENT_REASONS = (
    # removed
    "video unavailable",
    "has been removed",
    "no longer available",
    "has been terminated",
    "copyright",
    # private
    "private video",
    "video is private",
    # members-only
    "members-only",
    "join this channel",
    # age-gated
    "confirm your age",
    "age-restricted",
    "inappropriate for some users",
)


def _classify(message: str) -> FetchFailed:
    """The FetchFailed fitting yt-dlp's error `message`."""
    reason = message.lower()
    if any(fragment in reason for fragment in _BOT_CHECK_REASONS):
        return BotCheck(message)
    if any(fragment in reason for fragment in _TEMPORARY_REASONS):
        return FetchFailed(message)
    if any(fragment in reason for fragment in _PERMANENT_REASONS):
        return VideoUnavailable(message)
    return FetchFailed(message)


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
