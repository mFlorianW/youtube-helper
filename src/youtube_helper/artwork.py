# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The Artwork port: images such as a channel's avatar, downloaded anonymously."""

from typing import Protocol

import httpx


class ArtworkUnavailable(Exception):
    """The image can't be downloaded right now; worth retrying later."""


class Artwork(Protocol):
    def download(self, url: str) -> bytes:
        """The image at `url`. Raises ArtworkUnavailable when it can't be downloaded."""
        ...


class HttpArtwork:
    """Downloads images over plain HTTP, without any login."""

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def download(self, url: str) -> bytes:
        try:
            response = self._http.get(url, follow_redirects=True)
        except httpx.HTTPError as error:
            raise ArtworkUnavailable(f"can't reach {url}: {error}") from error
        if response.status_code != httpx.codes.OK:
            raise ArtworkUnavailable(f"HTTP {response.status_code} for {url}")
        if not response.headers.get("content-type", "").startswith("image/"):
            raise ArtworkUnavailable(f"{url} is not an image")
        return response.content
