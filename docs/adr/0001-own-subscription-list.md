<!--
SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>

SPDX-License-Identifier: GPL-2.0-only
-->

# The tool keeps its own list of Subscriptions instead of reading the Google account

The Subscriptions were first read from the owner's Google account over the YouTube Data API with OAuth (#3, #6). That needed a Google Cloud project, a published but unverified OAuth app with a privacy policy and terms of service, and a `login` that could lapse. We dropped Google entirely. The owner now maintains the Subscriptions in the tool's web UI, and the tool only talks to YouTube anonymously: per-channel RSS feeds for New uploads, yt-dlp for downloads and channel metadata. Subscribing on YouTube itself therefore has no effect.

## Considered Options

- **Public subscriptions + Data API key**: no login, but it still needs a Google Cloud project, and the owner's subscriptions have to be public.
- **Exported browser cookies + yt-dlp** (`feed/channels`): this does act as the account, but YouTube rotates the cookies unpredictably and risks flagging the owner's account. It would bring back the lapsing login in a worse form.
- **Google Takeout import**: manual and always stale.
