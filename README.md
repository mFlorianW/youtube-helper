<!--
SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>

SPDX-License-Identifier: GPL-2.0-only
-->

# youtube-helper

Keeps a Media library stocked with New uploads from the YouTube channels you subscribe to in its
web UI. A media server such as Jellyfin can scan the Media library; youtube-helper knows nothing
about it. It needs no Google account, login or API key: it only talks to YouTube anonymously.

## Deployment

youtube-helper runs as its own Docker Compose project. Start from
[`examples/compose.yaml`](examples/compose.yaml): every setting there is commented, and the values
to adjust are marked `adjust`.

```sh
mkdir -p /srv/youtube-helper/state
cp examples/compose.yaml /srv/youtube-helper/compose.yaml
docker compose -f /srv/youtube-helper/compose.yaml up -d
docker compose -f /srv/youtube-helper/compose.yaml logs -f
```

Then open the [web UI](#web-ui) at `http://<host>:8080` and add the channels to follow.

### With Docker Swarm

If the host runs a swarm anyway, deploy [`examples/stack.yaml`](examples/stack.yaml) as a stack
on a single-node swarm. The file's header lists the `docker stack deploy` command. The image and
the settings are the same as with plain Compose.

## Updates

YouTube changes its site often, and yt-dlp keeps up with frequent releases. An outdated yt-dlp
soon fails every download, so updates reach the host without manual work:

1. The [Renovate](https://docs.renovatebot.com/) GitHub app opens a pull request when yt-dlp,
   yt-dlp-ejs or Deno (the JavaScript runtime yt-dlp needs for YouTube) has a new release, and
   merges it itself once CI has passed. yt-dlp-ejs updates come with the yt-dlp update that
   needs them; Deno major releases wait for a human. PRs for other dependencies are opened
   too, but wait for a human. See [`renovate.json5`](renovate.json5).
2. CI checks that yt-dlp in the built image finds Deno and yt-dlp-ejs. Every merge to `main`
   publishes the image as `ghcr.io/mflorianw/youtube-helper:latest`.
3. On the host, a daily job runs `docker compose pull && docker compose up -d` in the
   compose project's directory. It recreates the container only if a new image was pulled,
   then runs `docker image prune --force` to remove the image it replaced.

For step 3, install either the systemd timer or the cron job from
[`examples/update/`](examples/update/); each file's header says how. Adjust the directory if
your `compose.yaml` isn't in `/srv/youtube-helper`.

A yt-dlp fix thus usually reaches the host within a day. `docker compose pull` keeps the old
image, untagged; the prune removes it so images don't pile up. It removes every dangling image
on the host, not only youtube-helper's; drop it from the job if you keep untagged images on
purpose. With Docker Swarm, change the job's commands to
`docker stack deploy -c stack.yaml youtube-helper` plus the prune, run in the directory holding
`stack.yaml`: it pulls `latest` again and updates the service if the image changed.

## Web UI

The daemon serves a small web UI on port 8080 (`WEB_PORT`) for managing the Subscriptions and
checking on the tool. It has no login of its own: keep the port inside your LAN, or put a reverse proxy with
authentication in front of it if you expose it any further.

To add a channel, paste its `@handle`, its channel URL or the URL of any of its videos and click
**Look up**. youtube-helper looks the channel up on YouTube and shows its avatar and name;
**Subscribe** adds it. **Remove** next to a Subscription removes it again. These are the only
actions on the Subscriptions page; everything else there is information. The
[Notifications](#notifications) page, linked from the header, holds the email settings.

### Status

The page shows what the last cycle did, refreshed on its own every 30 seconds:

- **Last cycle**: when it started and finished, and how many New uploads it downloaded.
- Warnings that need your attention. Each stays until a later cycle no longer finds the problem:
  - **All downloads failing**: every download of the last cycle that tried any failed in
    yt-dlp, which is likely outdated; pull the latest image. If YouTube asked for bot checks,
    the warning says so: a PO-token provider is needed if this keeps happening.
  - **Low free space**: the free space in the Media library and the 10 GiB threshold below
    which downloads are paused.
  - **Media library not writable**, naming the directory: cycles are skipped until it is.

Each Subscription lists its own problems:

- videos being retried, with their last error and when they first failed;
- videos given up on after 24 hours or skipped for good, with their errors, for 30 days;
- a note when the channel's upload feed couldn't be read in the last cycle.

Videos excluded on purpose (Shorts, livestreams) are not shown. The status is kept in the state
database, so it survives restarts.

## Notifications

The **Notifications** page of the web UI holds all email settings; none of them are set in the
deployment. They are kept in the state database. Notifications themselves aren't sent yet:
for now the page only stores the settings and sends a test email.

- **Email Notifications** switches them on or off; off by default. **New videos** and
  **Warnings** choose what they report; both are ticked by default.
- **SMTP server**: host, security (STARTTLS, implicit TLS or none) and port. A blank port is
  the usual one for the security setting: 587, 465 or 25. With STARTTLS and implicit TLS the
  server's certificate is verified.
- **Username** and **password**, both optional: leave them blank for a relay that needs no
  login; a password needs a username. Security "none" is refused together with either, as it
  would send them unencrypted.
- **From address** and one or more **recipients**, one per line.

Saving checks that the addresses are well-formed and, while Email Notifications are switched
on, that a host, a From address and a recipient are set. **Send test email** sends one right
away with the saved settings and shows the result or the SMTP server's error.

### The SMTP password

The password field is write-only: the page only says whether a password is set, and leaving the
field blank keeps the stored one. Changing the host, port, security or username clears it, so
that nobody can point the tool at their own server and receive the password; enter it again
with the new settings.

The password is stored in the state database encrypted (Fernet) under a key derived from the
secret in `SECRET_KEY_FILE`, ideally a Docker secret (see the examples). A copy of `/state`,
e.g. a backup, thus doesn't reveal the password; a copy together with the secret does, and
so does anyone who can read the secret inside the running container. The tool has to decrypt
the password to log in, so the encryption can't protect it from the host itself. Use an
app-specific password or a relay's password rather than your main mailbox password.

Generate the secret once and keep it, e.g. with `openssl rand -base64 32 > secret_key`.
Without `SECRET_KEY_FILE` the daemon still starts: the password field is disabled, and email
works only with servers that need no login. If the secret changes, or `/state` is restored on a
deployment with another secret, the stored password can't be decrypted any more: the page says
so and asks for it again.

## Subscriptions

youtube-helper follows the channels you subscribe to in its web UI, and only those: your
subscriptions on YouTube itself play no part.

- Tracking a Subscription starts when you add it, so nothing it published before is fetched.
  Its first upload feed check happens in the next regular cycle, within 30 minutes.
- When you remove a Subscription, the channel stops being tracked but its files stay in the
  Media library; the web UI never deletes anything there. When you add it again, tracking
  restarts from that moment and the gap is left alone.
- The channel's description and avatar are looked up again once a day, for `tvshow.nfo`. If
  that fails, the last known ones are kept and the lookup is tried again next cycle.

Every cycle the daemon logs the active Subscriptions.

Upgrading from a version that read the Subscriptions from your Google account keeps them, with
their tracking start, Channel folder and episode numbers. The Google secrets and the
`login` command are gone; remove the secrets from your `compose.yaml` and revoke the
youtube-helper app in your Google account if you like.

## Downloads

Every cycle the daemon reads each active Subscription's public upload feed. Each New upload,
i.e. a video released after the Subscription's tracking start, is downloaded anonymously with
yt-dlp (never with any cookies) and filed into the Media library:

```text
<Media library>/<Channel folder>/tvshow.nfo
<Media library>/<Channel folder>/poster.jpg
<Media library>/<Channel folder>/Season <YYYY>/<Channel> - <YYYY-MM-DD HH-MM> - <Title> [<videoid>].mkv
<Media library>/<Channel folder>/Season <YYYY>/<Channel> - <YYYY-MM-DD HH-MM> - <Title> [<videoid>].nfo
<Media library>/<Channel folder>/Season <YYYY>/<Channel> - <YYYY-MM-DD HH-MM> - <Title> [<videoid>]-thumb.jpg
```

- The Channel folder is named after the channel when you first add the Subscription and keeps
  that name even if the channel is renamed later.
- The release time is in the container's `TZ`. Characters that aren't allowed in Windows/SMB
  filenames (colons among them) are replaced, and overlong titles are shortened.
- Videos are the best quality up to 1080p, H.264 + AAC where YouTube offers it at that height,
  merged into MKV with metadata and chapters embedded.
- Downloads go to the hidden `.youtube-helper-staging` directory in the Media library first and
  are moved into place only when complete, so a media server never sees a half-finished file.
- Only regular videos are downloaded. Shorts, running live streams and livestream recordings
  are logged once as `Excluded` and never looked at again. A premiere is left alone while it is
  upcoming or still airing and downloaded once it has aired, like any regular video.
- Each video is downloaded once, also across restarts.
- Every cycle and before each download the daemon checks the free space in the Media library.
  Below 10 GiB it logs `Only … GiB free in the Media library …` and downloads nothing more that
  cycle. The New uploads stay pending and are downloaded automatically once there is enough
  space again; upload feeds are still read in the meantime.

### When downloads fail

- A video that can never be downloaded anonymously (removed, private, members-only or
  age-restricted) is skipped: the daemon logs `Skipped <title> (<videoid>) for good` once and
  never tries it again.
- Any other failure is tried again every cycle. If a video still fails 24 hours after its first
  failure, the daemon logs `Gave up on <title> (<videoid>) after failing for 24 hours` once and
  never tries it again.
- When YouTube asks for a bot check, the daemon says so and tries again next cycle. If this keeps
  happening, a PO-token provider is needed.
- When every download attempt of a cycle fails in yt-dlp and none of the videos was skipped for
  good, yt-dlp has most likely fallen behind a change on YouTube's side. Instead of one error per video, the daemon then logs
  a single `All downloads failing …; yt-dlp is likely outdated, pull the latest image`. Pulling
  the latest image brings the newest yt-dlp; the videos are tried again on the next cycle.

### Metadata for the media server

The Media library carries its metadata as NFO files, so Jellyfin (a **Shows** library pointed
at the Media library) or any other NFO-aware media server shows each Subscription as a show
with its videos in release order, without any plugin:

- `tvshow.nfo` in each Channel folder holds the channel's name (the one from first sight, like
  the Channel folder), its description and its channel ID; `poster.jpg` is the channel's avatar.
  Both appear with the channel's first video. The description follows the channel; the poster
  is downloaded once, and tried again every cycle until that succeeds.
- The `.nfo` next to each video holds its title, description, release date, season (the
  release year) and episode number, and its video ID.
- Episodes are numbered per Channel folder and season in the order they are downloaded,
  normally release order; each new year starts again at 1. Numbers never change, so a video
  that arrives late, e.g. a premiere airing after newer videos or a video whose download failed
  at first, gets the next number.

## Configuration

All configuration is passed to the container by Compose. There is no config file. The email
settings are the exception: they are made in the web UI (see [Notifications](#notifications)).

| Setting                        | Kind                 | Default  | Meaning                                                                                                           |
| ------------------------------ | -------------------- | -------- | ----------------------------------------------------------------------------------------------------------------- |
| `MEDIA_LIBRARY`                | environment          | required | Path of the Media library inside the container. Mount the host directory there read-write.                        |
| `STATE_DIR`                    | environment          | `/state` | Directory holding the SQLite state database `youtube-helper.sqlite3`. Mount a host directory there and back it up. |
| `TZ`                           | environment          | `UTC`    | IANA time zone, e.g. `Europe/Berlin`. Used for log times, release times and the web UI. An unknown name stops the daemon. |
| `WEB_PORT`                     | environment          | `8080`   | Port of the web UI inside the container. Publish it with `ports:`. A taken or invalid port stops the daemon.      |
| `SECRET_KEY_FILE`              | environment          | none     | File holding the secret the SMTP password is encrypted with, e.g. `/run/secrets/secret_key`. See [The SMTP password](#the-smtp-password). Set but unreadable or empty, it stops the daemon. |
| `user`                         | Compose service key  | root     | UID:GID the tool runs as. It must be able to write the Media library and state directory.                         |

The daemon checks that the Media library is writable at startup and before every cycle. If it
isn't, the daemon logs an error naming the directory, skips the cycle and tries again 30 minutes
later; it keeps running, and the web UI shows the problem. A missing `MEDIA_LIBRARY`, an unknown
`TZ`, an unreadable `SECRET_KEY_FILE` or a state database that can't be opened stop the daemon
at startup. youtube-helper never
changes ownership or permissions; setting them up is the admin's job.

## Development

```sh
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest
```

`uv run pytest -m smoke` additionally looks up a channel and downloads a short public video from
YouTube for real; it needs network access, `ffmpeg` and `deno`.
