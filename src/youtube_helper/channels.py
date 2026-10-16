# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The Channel resolver port: a YouTube channel's details, looked up anonymously with yt-dlp."""

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.utils import YoutubeDLError

log = logging.getLogger(__name__)

CHANNEL_URL = "https://www.youtube.com/channel/"

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
_CHANNEL_ID = re.compile(r"UC[\w-]{22}")
_HANDLE = re.compile(r"@[\w.\-·]+")
# Fragments of yt-dlp's errors, lower-cased, that mean the input names no channel at all.
_NOT_A_CHANNEL_REASONS = (
    "404",
    "does not exist",
    "unsupported url",
    "not a valid url",
    "incomplete youtube id",
    "video unavailable",
    "video is unavailable",
    "has been terminated",
)


class NotAChannel(Exception):
    """The input doesn't lead to a YouTube channel."""


class ChannelUnavailable(Exception):
    """The channel can't be looked up right now; worth retrying later."""


@dataclass(frozen=True)
class Channel:
    channel_id: str
    display_name: str
    avatar_url: str | None
    description: str


class ChannelResolver(Protocol):
    def resolve(self, reference: str) -> Channel:
        """The channel `reference` leads to: a channel ID, `@handle`, channel or video URL.

        Raises NotAChannel if it leads to none, ChannelUnavailable if YouTube can't be asked now.
        """
        ...


class YtDlpResolver:
    """Looks channels up with the embedded yt-dlp, anonymously: no cookies, ever."""

    def resolve(self, reference: str) -> Channel:
        info = self._extract(_url(reference))
        channel_id = info.get("channel_id")
        if not isinstance(channel_id, str) or not _CHANNEL_ID.fullmatch(channel_id):
            raise NotAChannel(f"{reference} leads to no YouTube channel")
        # A video or playlist names its channel; the channel's own page has its details.
        if info.get("id") != channel_id:
            info = self._extract(CHANNEL_URL + channel_id)
        return Channel(
            channel_id=channel_id,
            display_name=str(info.get("channel") or info.get("uploader") or channel_id),
            avatar_url=_avatar(info),
            description=str(info.get("description") or ""),
        )

    def _extract(self, url: str) -> dict[str, Any]:
        options = {"noplaylist": True, "cachedir": False, "logger": _Quiet(), "noprogress": True}
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                # Unprocessed, a channel's videos stay unfetched: only its details are needed.
                info: dict[str, Any] = ydl.extract_info(url, download=False, process=False)
        except YoutubeDLError as error:
            message = str(error).removeprefix("ERROR: ")
            if any(fragment in message.lower() for fragment in _NOT_A_CHANNEL_REASONS):
                raise NotAChannel(f"{url} leads to no YouTube channel ({message})") from error
            raise ChannelUnavailable(message) from error
        return info


class _Quiet:
    """Keeps yt-dlp's messages, errors included, out of the log: the caller reports errors."""

    def debug(self, message: str) -> None:
        log.debug("%s", message)

    info = warning = error = debug


def _url(reference: str) -> str:
    """The YouTube URL `reference` stands for; NotAChannel unless it's on YouTube."""
    reference = reference.strip()
    if _CHANNEL_ID.fullmatch(reference):
        return CHANNEL_URL + reference
    if _HANDLE.fullmatch(reference):
        return f"https://www.youtube.com/{reference}"
    url = reference if re.match(r"https?://", reference) else f"https://{reference}"
    if urlsplit(url).hostname not in _YOUTUBE_HOSTS:
        raise NotAChannel(f"{reference} is not a YouTube channel, handle or video")
    return url


def _avatar(info: dict[str, Any]) -> str | None:
    for thumbnail in info.get("thumbnails") or []:
        if thumbnail.get("id") == "avatar_uncropped" and thumbnail.get("url"):
            return str(thumbnail["url"])
    return None
