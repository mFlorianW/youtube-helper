# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The Upload feed port: a channel's recent uploads, read from its public RSS feed."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

FEED_URL = "https://www.youtube.com/feeds/videos.xml"

_NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


class FeedUnavailable(Exception):
    """The channel's uploads can't be read right now; worth retrying later."""


@dataclass(frozen=True)
class Upload:
    video_id: str
    title: str
    released: datetime


class UploadFeed(Protocol):
    def uploads(self, channel_id: str) -> list[Upload]:
        """The channel's recent uploads, newest first.

        Raises FeedUnavailable when they can't be read.
        """
        ...


class YouTubeFeed:
    """Reads a channel's uploads anonymously from YouTube's public RSS feed."""

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def uploads(self, channel_id: str) -> list[Upload]:
        try:
            response = self._http.get(FEED_URL, params={"channel_id": channel_id})
        except httpx.HTTPError as error:
            raise FeedUnavailable(f"can't reach {FEED_URL}: {error}") from error
        if response.status_code != httpx.codes.OK:
            raise FeedUnavailable(
                f"YouTube refused the feed of {channel_id}: HTTP {response.status_code}"
            )
        try:
            feed = ET.fromstring(response.content)
        except ET.ParseError as error:
            raise FeedUnavailable(f"can't parse the feed of {channel_id}: {error}") from error
        try:
            return [_upload(entry) for entry in feed.iterfind("atom:entry", _NAMESPACES)]
        except (AttributeError, ValueError) as error:
            raise FeedUnavailable(f"unexpected entry in the feed of {channel_id}") from error


def _upload(entry: ET.Element) -> Upload:
    def text(path: str) -> str:
        element = entry.find(path, _NAMESPACES)
        if element is None or not element.text:
            raise ValueError(f"no {path}")
        return element.text

    released = datetime.fromisoformat(text("atom:published"))
    if released.tzinfo is None:
        raise ValueError("release time without time zone")
    return Upload(text("yt:videoId"), text("atom:title"), released)
