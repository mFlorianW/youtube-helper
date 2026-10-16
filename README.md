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
docker compose -f /srv/youtube-helper/compose.yaml logs -f
```

## Configuration

All configuration is passed to the container by Compose. There is no config file.

| Setting                        | Kind                 | Default  | Meaning                                                                                                           |
| ------------------------------ | -------------------- | -------- | ----------------------------------------------------------------------------------------------------------------- |
| `MEDIA_LIBRARY`                | environment          | required | Path of the Media library inside the container. Mount the host directory there read-write.                        |
| `STATE_DIR`                    | environment          | `/state` | Directory holding the SQLite state database `youtube-helper.sqlite3`. Mount a host directory there and back it up. |
| `TZ`                           | environment          | `UTC`    | IANA time zone, e.g. `Europe/Berlin`. Used for log times and release times. An unknown name stops the daemon.     |
| `google_client_id`             | Compose secret       | none     | File containing the Google OAuth client ID. Mounted at `/run/secrets/google_client_id`.                            |
| `google_client_secret`         | Compose secret       | none     | File containing the Google OAuth client secret. Mounted at `/run/secrets/google_client_secret`.                    |
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
