# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

from collections.abc import Callable

import httpx
import pytest

from youtube_helper.artwork import ArtworkUnavailable, HttpArtwork

AVATAR_URL = "https://yt3.ggpht.com/UC1=s800"


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_downloads_the_image() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == AVATAR_URL
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8")

    assert HttpArtwork(client(handler)).download(AVATAR_URL) == b"\xff\xd8"


def test_follows_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AVATAR_URL:
            return httpx.Response(302, headers={"location": "https://yt3.ggpht.com/moved"})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8")

    assert HttpArtwork(client(handler)).download(AVATAR_URL) == b"\xff\xd8"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(404), "HTTP 404"),
        (httpx.Response(200, headers={"content-type": "text/html"}), "not an image"),
    ],
)
def test_refused_download_is_unavailable(response: httpx.Response, message: str) -> None:
    with pytest.raises(ArtworkUnavailable, match=message):
        HttpArtwork(client(lambda request: response)).download(AVATAR_URL)


def test_unreachable_server_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ArtworkUnavailable, match="can't reach"):
        HttpArtwork(client(handler)).download(AVATAR_URL)
