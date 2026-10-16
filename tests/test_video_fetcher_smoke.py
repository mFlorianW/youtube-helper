# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""Opt-in: downloads a real video from YouTube. Run with `pytest -m smoke`."""

import json
import subprocess
from pathlib import Path

import pytest

from youtube_helper.video_fetcher import YtDlpFetcher

# "Me at the zoo": short, public and unlikely ever to go away.
VIDEO_ID = "jNQXAC9IVRw"


@pytest.mark.smoke
def test_downloads_a_public_video_as_mkv_with_metadata_and_thumbnail(tmp_path: Path) -> None:
    base = tmp_path / "video"

    metadata = YtDlpFetcher().fetch(VIDEO_ID, base)

    assert metadata.description

    assert sorted(p.name for p in tmp_path.iterdir()) == ["video-thumb.jpg", "video.mkv"]
    assert (tmp_path / "video-thumb.jpg").read_bytes().startswith(b"\xff\xd8")
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            base.with_suffix(".mkv"),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    media = json.loads(probe.stdout)
    assert "matroska" in media["format"]["format_name"]
    assert media["format"]["tags"]["title"] == "Me at the zoo"
    assert {s["codec_type"] for s in media["streams"]} >= {"video", "audio"}
