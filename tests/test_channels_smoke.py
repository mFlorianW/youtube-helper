# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""Opt-in: looks channels up on the real YouTube. Run with `pytest -m smoke`."""

import pytest

from youtube_helper.channels import NotAChannel, YtDlpResolver

# The channel of "Me at the zoo": public and unlikely ever to go away.
CHANNEL_ID = "UC4QobU6STFB0P71PMvOGN5A"


@pytest.mark.smoke
@pytest.mark.parametrize(
    "reference",
    [
        CHANNEL_ID,
        "@jawed",
        "youtube.com/@jawed",
        f"https://www.youtube.com/channel/{CHANNEL_ID}",
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://youtu.be/jNQXAC9IVRw",
    ],
)
def test_resolves_a_channel_from_whatever_the_browser_shows(reference: str) -> None:
    channel = YtDlpResolver().resolve(reference)

    assert channel.channel_id == CHANNEL_ID
    assert channel.display_name == "jawed"
    assert channel.avatar_url is not None
    assert channel.avatar_url.startswith("https://")


@pytest.mark.smoke
@pytest.mark.parametrize(
    "reference", ["@no-such-handle-for-youtube-helper", "https://example.com/@jawed", "jawed"]
)
def test_input_that_is_no_youtube_channel_is_rejected(reference: str) -> None:
    with pytest.raises(NotAChannel):
        YtDlpResolver().resolve(reference)
