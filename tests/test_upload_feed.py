# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

from datetime import UTC, datetime

import httpx
import pytest

from youtube_helper.upload_feed import FEED_URL, FeedUnavailable, Upload, YouTubeFeed

# Trimmed from a real https://www.youtube.com/feeds/videos.xml?channel_id=… response.
FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <link rel="self" href="http://www.youtube.com/feeds/videos.xml?channel_id=UC1"/>
 <id>yt:channel:UC1</id>
 <yt:channelId>UC1</yt:channelId>
 <title>First Channel</title>
 <published>2010-01-01T00:00:00+00:00</published>
 <entry>
  <id>yt:video:vid00000002</id>
  <yt:videoId>vid00000002</yt:videoId>
  <yt:channelId>UC1</yt:channelId>
  <title>Second: the sequel &amp; more</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=vid00000002"/>
  <published>2026-10-16T14:05:09+00:00</published>
  <updated>2026-10-16T15:00:00+00:00</updated>
  <media:group><media:title>Second: the sequel &amp; more</media:title></media:group>
 </entry>
 <entry>
  <id>yt:video:vid00000001</id>
  <yt:videoId>vid00000001</yt:videoId>
  <yt:channelId>UC1</yt:channelId>
  <title>First</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=vid00000001"/>
  <published>2026-10-15T08:00:00+00:00</published>
  <updated>2026-10-15T08:00:00+00:00</updated>
 </entry>
</feed>
"""


def feed_client(
    status: int, body: str, requests: list[httpx.Request] | None = None
) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(status, text=body)

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_reads_the_channel_feed_into_uploads_with_release_times() -> None:
    requests: list[httpx.Request] = []

    uploads = YouTubeFeed(feed_client(200, FEED, requests)).uploads("UC1")

    assert uploads == [
        Upload(
            "vid00000002", "Second: the sequel & more", datetime(2026, 10, 16, 14, 5, 9, tzinfo=UTC)
        ),
        Upload("vid00000001", "First", datetime(2026, 10, 15, 8, 0, tzinfo=UTC)),
    ]
    [request] = requests
    assert str(request.url.copy_with(query=None)) == FEED_URL
    assert dict(request.url.params) == {"channel_id": "UC1"}


def test_feed_without_entries_has_no_uploads() -> None:
    empty = FEED.split("<entry>")[0] + "</feed>"

    assert YouTubeFeed(feed_client(200, empty)).uploads("UC1") == []


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        pytest.param(404, "Not Found", "HTTP 404", id="not found"),
        pytest.param(200, "<feed", "can't parse", id="malformed XML"),
        pytest.param(
            200,
            FEED.replace("<yt:videoId>vid00000001</yt:videoId>", ""),
            "unexpected",
            id="entry without video ID",
        ),
        pytest.param(
            200,
            FEED.replace("2026-10-15T08:00:00+00:00</published>", "yesterday</published>"),
            "unexpected",
            id="unparseable release time",
        ),
    ],
)
def test_unusable_feed_is_unavailable(status: int, body: str, reason: str) -> None:
    with pytest.raises(FeedUnavailable, match=reason):
        YouTubeFeed(feed_client(status, body)).uploads("UC1")


def test_unreachable_youtube_makes_the_feed_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known", request=request)

    http = httpx.Client(transport=httpx.MockTransport(refuse))

    with pytest.raises(FeedUnavailable, match="Name or service not known"):
        YouTubeFeed(http).uploads("UC1")
