<!--
SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>

SPDX-License-Identifier: GPL-2.0-only
-->

# The SMTP password is encrypted in the state database with a key from the deployment

All email settings, the SMTP password included, are edited in the web UI. The password is stored in the state database encrypted with Fernet (`cryptography`), under a key derived from a secret the deployment provides (`SECRET_KEY_FILE`, ideally a Docker secret). A copy of `/state` alone therefore doesn't reveal it; only a copy together with the deployment's secret does. Without the secret, email still works for relays that need no login. The password field in the web UI is write-only, and changing the host, port, security or username clears the stored password. The tool decrypts the password to send it, so without that rule anyone on the LAN could point it at their own server and receive the password.

## Considered Options

- **SMTP connection and password set in the deployment, the rest in the web UI**: the safest option, but the owner wants all email settings in the web UI.
- **Plaintext in the state database**: anyone with a copy of `/state` could read the password.
- **Encryption with a key the tool generates itself**: the key would have to live in `/state` next to the ciphertext, because the container is recreated on every update. That only hides the password.
- **`keyring` with `keyrings.cryptfile`**: the same security model, but unmaintained since 2022, and it adds three dependencies plus keyring's global backend selection to store a single secret.
- **Unlocking with a master password typed into the web UI after each start**: email would stop working after every daily auto-update until someone unlocks it.
