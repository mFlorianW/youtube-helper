# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The Media library: the admin-specified directory the tool files downloaded videos into."""

import tempfile
from pathlib import Path


class MediaLibraryUnwritable(Exception):
    def __init__(self, path: Path, reason: OSError) -> None:
        super().__init__(f"Media library {path} is not writable: {reason.strerror or reason}")


def ensure_writable(path: Path) -> None:
    """Prove `path` is writable by creating and removing a file in it.

    Permission bits alone can't tell (root, read-only mounts, ACLs), so this actually writes.
    Ownership and permissions are never changed; fixing them is the admin's job.
    """
    try:
        with tempfile.TemporaryFile(dir=path, prefix=".youtube-helper-write-check-"):
            pass
    except OSError as error:
        raise MediaLibraryUnwritable(path, error) from error
