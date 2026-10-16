# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""NFO files: the Kodi-style metadata Jellyfin and other media servers read without a plugin."""

import re
import xml.etree.ElementTree as ET
from datetime import date

# Characters XML 1.0 can't carry at all, not even escaped.
_NOT_XML = re.compile(r"[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def tvshow_nfo(*, title: str, plot: str, channel_id: str) -> bytes:
    """The `tvshow.nfo` that makes a Channel folder one series."""
    return _document(
        "tvshow",
        [("title", title), ("plot", plot)],
        unique_id=channel_id,
    )


def episode_nfo(
    *, title: str, plot: str, aired: date, season: int, episode: int, video_id: str
) -> bytes:
    """The `.nfo` next to a video that makes it one episode of its Channel folder's series."""
    return _document(
        "episodedetails",
        [
            ("title", title),
            ("plot", plot),
            ("aired", aired.isoformat()),
            ("season", str(season)),
            ("episode", str(episode)),
        ],
        unique_id=video_id,
    )


def _document(root_tag: str, fields: list[tuple[str, str]], *, unique_id: str) -> bytes:
    root = ET.Element(root_tag)
    for tag, text in fields:
        ET.SubElement(root, tag).text = _NOT_XML.sub("", text)
    ET.SubElement(root, "uniqueid", type="youtube", default="true").text = unique_id
    ET.indent(root)
    document: bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return document + b"\n"
