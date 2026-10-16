# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

from collections.abc import Iterable
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from youtube_helper.login import DEVICE_CODE_URL, SCOPE, TOKEN_URL, run_login
from youtube_helper.state import StateStore

type Reply = tuple[int, dict[str, object]]

DEVICE_CODE_RESPONSE: dict[str, object] = {
    "device_code": "device-code",
    "user_code": "ABC-DEF-GHI",
    "verification_url": "https://www.google.com/device",
    "expires_in": 1800,
    "interval": 5,
}
APPROVED: Reply = (200, {"access_token": "access", "refresh_token": "refresh-token"})
PENDING: Reply = (428, {"error": "authorization_pending"})
SLOW_DOWN: Reply = (403, {"error": "slow_down"})
DENIED: Reply = (403, {"error": "access_denied"})
EXPIRED: Reply = (400, {"error": "expired_token"})


class FakeGoogle:
    """Answers the device code request, then each token poll with the next scripted response."""

    def __init__(
        self,
        token_responses: Iterable[Reply],
        device_code_response: Reply = (200, DEVICE_CODE_RESPONSE),
    ) -> None:
        self.device_code_response = device_code_response
        self.token_responses = list(token_responses)
        self.requests: list[tuple[str, dict[str, list[str]]]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((url, parse_qs(request.content.decode())))
        if url == DEVICE_CODE_URL:
            status, body = self.device_code_response
        elif url == TOKEN_URL:
            status, body = self.token_responses.pop(0)
        else:
            status, body = 404, {}
        return httpx.Response(status, json=body)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def token_requests(self) -> list[dict[str, list[str]]]:
        return [form for url, form in self.requests if url == TOKEN_URL]


class FakeTime:
    """A monotonic clock that only advances when slept on."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    (directory / "google_client_id").write_text("client-id.apps.googleusercontent.com\n")
    (directory / "google_client_secret").write_text("client-secret\n")
    return directory


def login(google: FakeGoogle, state_dir: Path, secrets_dir: Path, time: FakeTime) -> int:
    return run_login(
        {"STATE_DIR": str(state_dir)},
        secrets_dir=secrets_dir,
        http=google.client(),
        sleep=time.sleep,
        clock=time.clock,
    )


def stored_refresh_token(state_dir: Path) -> str | None:
    state = StateStore.open(state_dir)
    try:
        return state.refresh_token()
    finally:
        state.close()


def test_login_prints_code_and_stores_refresh_token_once_approved(
    state_dir: Path, secrets_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    google = FakeGoogle([PENDING, PENDING, APPROVED])
    time = FakeTime()

    status = login(google, state_dir, secrets_dir, time)

    assert status == 0
    out = capsys.readouterr().out
    assert "ABC-DEF-GHI" in out
    assert "https://www.google.com/device" in out
    assert "Logged in" in out
    assert stored_refresh_token(state_dir) == "refresh-token"
    assert time.sleeps == [5, 5, 5]


def test_login_requests_youtube_readonly_with_the_client_credentials_from_the_secrets(
    state_dir: Path, secrets_dir: Path
) -> None:
    google = FakeGoogle([APPROVED])

    login(google, state_dir, secrets_dir, FakeTime())

    device_code_form = google.requests[0][1]
    assert device_code_form == {
        "client_id": ["client-id.apps.googleusercontent.com"],
        "scope": [SCOPE],
    }
    assert google.token_requests() == [
        {
            "client_id": ["client-id.apps.googleusercontent.com"],
            "client_secret": ["client-secret"],
            "device_code": ["device-code"],
            "grant_type": ["urn:ietf:params:oauth:grant-type:device_code"],
        }
    ]


def test_login_polls_more_slowly_after_slow_down(state_dir: Path, secrets_dir: Path) -> None:
    google = FakeGoogle([PENDING, SLOW_DOWN, PENDING, SLOW_DOWN, APPROVED])
    time = FakeTime()

    status = login(google, state_dir, secrets_dir, time)

    assert status == 0
    assert time.sleeps == [5, 5, 10, 10, 15]


def test_denied_login_fails_with_a_clear_message_and_stores_nothing(
    state_dir: Path, secrets_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    google = FakeGoogle([PENDING, DENIED])

    status = login(google, state_dir, secrets_dir, FakeTime())

    assert status != 0
    assert "access was denied" in capsys.readouterr().err
    assert stored_refresh_token(state_dir) is None


def test_login_fails_when_google_reports_the_code_expired(
    state_dir: Path, secrets_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    google = FakeGoogle([PENDING, EXPIRED])

    status = login(google, state_dir, secrets_dir, FakeTime())

    assert status != 0
    assert "code expired" in capsys.readouterr().err
    assert stored_refresh_token(state_dir) is None


def test_login_gives_up_once_the_code_has_expired(
    state_dir: Path, secrets_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1800 s at 5 s per poll: Google never answers anything but "pending".
    google = FakeGoogle([PENDING] * 400)
    time = FakeTime()

    status = login(google, state_dir, secrets_dir, time)

    assert status != 0
    assert "code expired" in capsys.readouterr().err
    assert time.now == 1800
    assert stored_refresh_token(state_dir) is None


def test_login_fails_when_google_rejects_the_client(
    state_dir: Path, secrets_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    google = FakeGoogle(
        [],
        device_code_response=(
            401,
            {"error": "invalid_client", "error_description": "The OAuth client was not found."},
        ),
    )

    status = login(google, state_dir, secrets_dir, FakeTime())

    assert status != 0
    err = capsys.readouterr().err
    assert "invalid_client" in err
    assert "The OAuth client was not found." in err


def test_login_fails_naming_a_missing_secret(
    state_dir: Path, secrets_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (secrets_dir / "google_client_secret").unlink()

    status = login(FakeGoogle([APPROVED]), state_dir, secrets_dir, FakeTime())

    assert status != 0
    assert str(secrets_dir / "google_client_secret") in capsys.readouterr().err


def test_login_replaces_an_earlier_refresh_token(state_dir: Path, secrets_dir: Path) -> None:
    login(FakeGoogle([APPROVED]), state_dir, secrets_dir, FakeTime())
    renewed: Reply = (200, {"access_token": "access", "refresh_token": "renewed-token"})

    login(FakeGoogle([renewed]), state_dir, secrets_dir, FakeTime())

    assert stored_refresh_token(state_dir) == "renewed-token"
