<!--
SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>

SPDX-License-Identifier: GPL-2.0-only
-->

# youtube-helper

Keeps a Media library stocked with New uploads from the YouTube channels you are subscribed to.
A media server such as Jellyfin can scan the Media library; youtube-helper knows nothing about it.

## Deployment

youtube-helper runs as its own Docker Compose project. Start from
[`examples/compose.yaml`](examples/compose.yaml): every setting there is commented, and the values
to adjust are marked `adjust`.

```sh
mkdir -p /srv/youtube-helper/secrets /srv/youtube-helper/state
cp examples/compose.yaml /srv/youtube-helper/compose.yaml
# write the Google OAuth client ID and secret into secrets/google_client_id and
# secrets/google_client_secret, then:
docker compose -f /srv/youtube-helper/compose.yaml up -d
docker compose -f /srv/youtube-helper/compose.yaml run --rm youtube-helper login
docker compose -f /srv/youtube-helper/compose.yaml logs -f
```

How to get the client ID and secret and what `login` does is explained in
[Logging in](#logging-in).

### With Docker Swarm

To keep the Google client ID and secret in Swarm's encrypted secret store rather than in files
on the host, deploy [`examples/stack.yaml`](examples/stack.yaml) as a stack on a single-node
swarm. The file's header lists the `docker secret create` and `docker stack deploy` commands.
The image and the settings are the same as with plain Compose.

## Logging in

youtube-helper reads your Subscriptions with your own Google account. It needs an OAuth client
that you create once in Google Cloud, and a one-time `login` that you approve from your phone or
any other device with a browser. Until then the daemon logs ``Not logged in, run `login` `` every
cycle and downloads nothing.

### 1. Create the OAuth client

1. Open the [Google Cloud console](https://console.cloud.google.com/) and create a new project,
   e.g. `youtube-helper`.
2. Under **APIs & Services → Library**, search for **YouTube Data API v3** and click **Enable**.
3. Under **Google Auth Platform** (formerly **OAuth consent screen**), click **Get started**:
   enter an app name and your email address, choose the audience **External**, and finish.
4. Under **Audience**, click **Publish app** so the publishing status becomes **In production**.
   Leave the app unverified; verification is only needed for apps used by other people.
   An app left in *Testing* gets refresh tokens that expire after 7 days, so you would have to
   log in again every week.
5. Under **Clients**, click **Create client**, choose the application type
   **TVs and Limited Input devices**, give it a name and click **Create**. Copy the client ID
   (`…apps.googleusercontent.com`) and the client secret.

### 2. Store the ID and secret as secrets

The ID and secret are passed to the container as the secrets `google_client_id` and
`google_client_secret` (see [Configuration](#configuration)). With
[`examples/compose.yaml`](examples/compose.yaml) they are files next to the compose file:

```sh
cd /srv/youtube-helper
printf '%s' '<client id>' > secrets/google_client_id
printf '%s' '<client secret>' > secrets/google_client_secret
chown 1000:1000 secrets/*   # the `user:` from compose.yaml
chmod 600 secrets/*
```

For Swarm, create them with `docker secret create` as described in
[`examples/stack.yaml`](examples/stack.yaml).

### 3. Run `login`

```sh
docker compose -f /srv/youtube-helper/compose.yaml run --rm youtube-helper login
```

`login` prints a web address and a code. Open the address on your phone, sign in with the Google
account whose Subscriptions you want, enter the code and allow read-only access to your YouTube
account. Because the app is unverified, Google first warns that it hasn't verified the app:
it is your own app, so choose **Advanced → Go to youtube-helper (unsafe)**.

`login` waits until you have approved, stores the resulting refresh token in the state
database and confirms with `Logged in`. A running daemon picks the login up at its next cycle;
there is no need to restart it. If you deny access or don't approve before the code expires
(after about 30 minutes), `login` says so and exits non-zero; just run it again. Running it
again later replaces the stored login, e.g. after you revoked access in your Google account.

## Subscriptions

Every cycle the daemon reads all your current Subscriptions from your Google account and logs
the active ones. Tracking a Subscription starts when the daemon first sees it, so on the very
first start nothing older than that is fetched; the same goes for a channel you subscribe to
later. When you unsubscribe, the channel stops being tracked but its files stay in the Media
library. When you subscribe again, tracking restarts from that moment and the gap is left alone.

If Google no longer accepts the login, e.g. because you revoked access, the daemon logs
``Login has lapsed, run `login` again`` every cycle and keeps using the Subscriptions it last
saw until you run `login` again.

## Downloads

Every cycle the daemon reads each active Subscription's public upload feed. Each New upload,
i.e. a video released after the Subscription's tracking start, is downloaded anonymously with
yt-dlp (never with your Google cookies) and filed into the Media library:

```text
<Media library>/<Channel folder>/Season <YYYY>/<Channel> - <YYYY-MM-DD HH-MM> - <Title> [<videoid>].mkv
<Media library>/<Channel folder>/Season <YYYY>/<Channel> - <YYYY-MM-DD HH-MM> - <Title> [<videoid>]-thumb.jpg
```

- The Channel folder is named after the channel when the daemon first sees the Subscription and
  keeps that name even if the channel is renamed later.
- The release time is in the container's `TZ`. Characters that aren't allowed in Windows/SMB
  filenames (colons among them) are replaced, and overlong titles are shortened.
- Videos are the best quality up to 1080p, H.264 + AAC where YouTube offers it at that height,
  merged into MKV with metadata and chapters embedded.
- Downloads go to the hidden `.youtube-helper-staging` directory in the Media library first and
  are moved into place only when complete, so a media server never sees a half-finished file.
- Each video is downloaded once, also across restarts. A failed download is tried again the
  next cycle.
- Before each download the daemon checks the free space in the Media library. Below 10 GiB it
  logs `Only … GiB free in the Media library …` and downloads nothing more that cycle. The New
  uploads stay pending and are downloaded automatically once there is enough space again;
  Subscriptions and upload feeds are still read in the meantime.

## Configuration

All configuration is passed to the container by Compose. There is no config file.

| Setting                        | Kind                 | Default  | Meaning                                                                                                           |
| ------------------------------ | -------------------- | -------- | ----------------------------------------------------------------------------------------------------------------- |
| `MEDIA_LIBRARY`                | environment          | required | Path of the Media library inside the container. Mount the host directory there read-write.                        |
| `STATE_DIR`                    | environment          | `/state` | Directory holding the SQLite state database `youtube-helper.sqlite3`. Mount a host directory there and back it up. |
| `TZ`                           | environment          | `UTC`    | IANA time zone, e.g. `Europe/Berlin`. Used for log times and release times. An unknown name stops the daemon.     |
| `google_client_id`             | secret               | none     | The Google OAuth client ID, mounted as a file at `/run/secrets/google_client_id`.                            |
| `google_client_secret`         | secret               | none     | The Google OAuth client secret, mounted as a file at `/run/secrets/google_client_secret`.                    |
| `user`                         | Compose service key  | root     | UID:GID the tool runs as. It must be able to write the Media library and state directory and read the secrets.    |

The daemon checks that the Media library is writable at startup and before every cycle. If it
isn't, the daemon exits non-zero with an error naming the directory. youtube-helper never
changes ownership or permissions; setting them up is the admin's job.

## Development

```sh
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest
```

`uv run pytest -m smoke` additionally downloads a short public video from YouTube for real; it
needs network access, `ffmpeg` and `deno`.
