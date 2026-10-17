# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""Email Notifications: their settings, the SMTP password's encryption and the test email."""

import base64
import hashlib
import re
import smtplib
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from enum import Enum
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken


class Security(Enum):
    STARTTLS = "starttls"
    TLS = "tls"
    NONE = "none"

    @property
    def default_port(self) -> int:
        return {Security.STARTTLS: 587, Security.TLS: 465, Security.NONE: 25}[self]


@dataclass(frozen=True)
class EmailSettings:
    """Whether and what to notify about, and how to reach the owner; the password is kept apart."""

    enabled: bool = False
    new_videos: bool = True
    warnings: bool = True
    host: str = ""
    port: int = Security.STARTTLS.default_port
    security: Security = Security.STARTTLS
    username: str = ""
    from_address: str = ""
    recipients: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether there is a server to send over, a sender and someone to send to."""
        return bool(self.host and self.from_address and self.recipients)

    def same_server(self, other: EmailSettings) -> bool:
        """Whether `other` logs into the same server as the same user: the password still fits."""
        return (self.host, self.port, self.security, self.username) == (
            other.host,
            other.port,
            other.security,
            other.username,
        )


class PasswordState(Enum):
    UNSET = "unset"
    SET = "set"
    # Encrypted under another key than the current one, or there is no key now.
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class SavedEmailSettings:
    """The email settings with the SMTP password, which is None unless `password_state` is SET."""

    settings: EmailSettings
    password_state: PasswordState = PasswordState.UNSET
    password: str | None = None


UNREADABLE_PASSWORD = (
    "The stored password can't be decrypted with the current secret key; please enter it again"
)


@dataclass(frozen=True)
class EmailForm:
    """The email settings as entered on the notifications page, and what is wrong with them.

    `password` is None when the field was left blank, which keeps the stored password.
    """

    settings: EmailSettings
    password: str | None
    errors: tuple[str, ...]


ADDRESS = re.compile(r'[^\s@,;<>"]+@[\w-]+(\.[\w-]+)*')


def read_email_form(form: Mapping[str, str], *, can_store_password: bool) -> EmailForm:
    """The email settings entered in `form`; a blank port is the security setting's default.

    Switched-off Notifications may be incomplete, but what is entered must be well-formed.
    """
    errors = []
    try:
        security = Security(form.get("security", ""))
    except ValueError:
        errors.append("Choose a security setting")
        security = Security.STARTTLS
    port = form.get("port", "").strip() or str(security.default_port)
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        errors.append(f"Port {port} is not a port number (1-65535)")
    settings = EmailSettings(
        enabled="enabled" in form,
        new_videos="new_videos" in form,
        warnings="warnings" in form,
        host=form.get("host", "").strip(),
        port=int(port) if port.isdigit() else security.default_port,
        security=security,
        username=form.get("username", "").strip(),
        from_address=form.get("from_address", "").strip(),
        recipients=tuple(
            line.strip() for line in form.get("recipients", "").splitlines() if line.strip()
        ),
    )
    password = form.get("password") or None
    if password and not can_store_password:
        errors.append("Set SECRET_KEY_FILE to store a password")
    if password and not settings.username:
        errors.append("A password is only used with a username; enter the username too")
    if settings.from_address and not ADDRESS.fullmatch(settings.from_address):
        errors.append(f"From address {settings.from_address} is not an email address")
    errors.extend(
        f"Recipient {recipient} is not an email address (one address per line)"
        for recipient in settings.recipients
        if not ADDRESS.fullmatch(recipient)
    )
    if settings.enabled and not settings.complete:
        errors.append("Email Notifications need an SMTP host, a From address and a recipient")
    if security is Security.NONE and (settings.username or password):
        errors.append(
            "Security “none” would send the username and password unencrypted:"
            " choose STARTTLS or implicit TLS, or leave both blank"
        )
    return EmailForm(settings, password, tuple(errors))


class PasswordCipher:
    """Encrypts the SMTP password under a key derived from the deployment's secret (ADR 0002)."""

    # The secret is the deployment's, not a password a human picked, so a fixed salt will do.
    _SALT = b"youtube-helper SMTP password"

    def __init__(self, secret: bytes) -> None:
        key = hashlib.scrypt(secret, salt=self._SALT, n=2**14, r=8, p=1, dklen=32)
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, password: str) -> str:
        return self._fernet.encrypt(password.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        """The password, or None if it was encrypted under another key."""
        try:
            return self._fernet.decrypt(token).decode()
        except InvalidToken:
            return None


class MailError(Exception):
    """An email couldn't be sent; the message says why, for the owner."""


class Mailer(Protocol):
    def send(self, settings: EmailSettings, password: str | None, message: EmailMessage) -> None:
        """Send `message` over the SMTP server in `settings`, logging in if there is a username.

        Raises MailError if it can't be sent.
        """


class SmtpMailer:
    """Sends over SMTP with smtplib, verifying the server's certificate unless security is none."""

    def __init__(self, timeout: float = 30) -> None:
        self._timeout = timeout

    def send(self, settings: EmailSettings, password: str | None, message: EmailMessage) -> None:
        if settings.username and password is None:
            raise MailError(f"No password set for {settings.username}")
        context = ssl.create_default_context()
        try:
            if settings.security is Security.TLS:
                smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                    settings.host, settings.port, timeout=self._timeout, context=context
                )
            else:
                smtp = smtplib.SMTP(settings.host, settings.port, timeout=self._timeout)
            with smtp:
                if settings.security is Security.STARTTLS:
                    smtp.starttls(context=context)
                if settings.username and password is not None:
                    smtp.login(settings.username, password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as error:
            raise MailError(str(error) or type(error).__name__) from error


def send_test_email(saved: SavedEmailSettings, mailer: Mailer, *, can_store_password: bool) -> str:
    """Send a test email with the saved settings; return what to tell the owner.

    Raises MailError if it can't be sent, also when the settings aren't complete enough to try.
    """
    settings = saved.settings
    if not settings.complete:
        raise MailError("Save an SMTP host, a From address and a recipient first")
    if saved.password_state is PasswordState.UNREADABLE:
        if not can_store_password:
            raise MailError("Set SECRET_KEY_FILE to use the stored password")
        raise MailError(UNREADABLE_PASSWORD)
    message = EmailMessage()
    message["Subject"] = "youtube-helper: test email"
    message["From"] = settings.from_address
    message["To"] = ", ".join(settings.recipients)
    message.set_content(
        "This is a test email from youtube-helper. If you can read it, its email settings work."
    )
    mailer.send(settings, saved.password, message)
    return f"Test email sent to {', '.join(settings.recipients)}."
