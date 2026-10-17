# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

import logging
from datetime import timedelta
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from youtube_helper.daemon import run_daemon


class StopDaemon(Exception):
    pass


class FakeSleep:
    """Records requested sleeps and stops the daemon after a given number of them."""

    def __init__(self, stop_after: int) -> None:
        self.stop_after = stop_after
        self.durations: list[timedelta] = []

    def __call__(self, seconds: float) -> None:
        self.durations.append(timedelta(seconds=seconds))
        if len(self.durations) >= self.stop_after:
            raise StopDaemon


class FakeServe:
    """Records the port and front page of the web UI served, instead of listening on the port."""

    def __init__(self, error: OSError | None = None) -> None:
        self.error = error
        self.served: list[tuple[int, str]] = []

    def __call__(self, app: Starlette, port: int) -> None:
        if self.error is not None:
            raise self.error
        self.served.append((port, TestClient(app).get("/").text))


@pytest.fixture(autouse=True)
def serve(monkeypatch: pytest.MonkeyPatch) -> FakeServe:
    serve = FakeServe()
    monkeypatch.setattr("youtube_helper.daemon.serve", serve)
    return serve


def environment(media_library: Path, state_dir: Path) -> dict[str, str]:
    return {"MEDIA_LIBRARY": str(media_library), "STATE_DIR": str(state_dir), "TZ": "Europe/Berlin"}


def test_daemon_runs_a_cycle_every_30_minutes(
    media_library: Path, state_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    sleep = FakeSleep(stop_after=2)

    with pytest.raises(StopDaemon):
        run_daemon(environment(media_library, state_dir), sleep=sleep)

    assert sleep.durations == [timedelta(minutes=30), timedelta(minutes=30)]
    assert caplog.text.count("Cycle finished") == 2


def test_daemon_exits_non_zero_when_media_library_is_unwritable_at_startup(
    tmp_path: Path, state_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing = tmp_path / "missing"

    status = run_daemon(environment(missing, state_dir), sleep=FakeSleep(stop_after=1))

    assert status != 0
    assert f"Media library {missing} is not writable" in caplog.text
    assert "Cycle started" not in caplog.text


def test_daemon_exits_non_zero_when_media_library_becomes_unwritable_before_a_cycle(
    media_library: Path, state_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    def sleep_while_library_disappears(seconds: float) -> None:
        media_library.rmdir()

    status = run_daemon(environment(media_library, state_dir), sleep=sleep_while_library_disappears)

    assert status != 0
    assert caplog.text.count("Cycle finished") == 1
    assert f"Media library {media_library} is not writable" in caplog.text


def test_daemon_exits_non_zero_without_media_library_setting(
    state_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    status = run_daemon({"STATE_DIR": str(state_dir)}, sleep=FakeSleep(stop_after=1))

    assert status != 0
    assert "MEDIA_LIBRARY is not set" in caplog.text


def test_daemon_exits_non_zero_with_unknown_time_zone(
    media_library: Path, state_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = environment(media_library, state_dir) | {"TZ": "Mars/Olympus_Mons"}

    status = run_daemon(env, sleep=FakeSleep(stop_after=1))

    assert status != 0
    assert "Mars/Olympus_Mons" in caplog.text


def test_daemon_exits_non_zero_naming_an_unusable_state_directory(
    media_library: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing = tmp_path / "missing-state"

    status = run_daemon(environment(media_library, missing), sleep=FakeSleep(stop_after=1))

    assert status != 0
    assert str(missing) in caplog.text


def test_daemon_serves_the_web_ui_on_port_8080_by_default(
    media_library: Path, state_dir: Path, serve: FakeServe
) -> None:
    with pytest.raises(StopDaemon):
        run_daemon(environment(media_library, state_dir), sleep=FakeSleep(stop_after=1))

    [(port, page)] = serve.served
    assert port == 8080
    assert "No Subscriptions yet" in page


def test_daemon_serves_the_web_ui_on_the_configured_port(
    media_library: Path, state_dir: Path, serve: FakeServe
) -> None:
    env = environment(media_library, state_dir) | {"WEB_PORT": "9000"}

    with pytest.raises(StopDaemon):
        run_daemon(env, sleep=FakeSleep(stop_after=1))

    assert [port for port, _ in serve.served] == [9000]


@pytest.mark.parametrize("port", ["http", "0", "65536"])
def test_daemon_exits_non_zero_with_an_invalid_web_port(
    media_library: Path, state_dir: Path, caplog: pytest.LogCaptureFixture, port: str
) -> None:
    env = environment(media_library, state_dir) | {"WEB_PORT": port}

    status = run_daemon(env, sleep=FakeSleep(stop_after=1))

    assert status != 0
    assert f"WEB_PORT {port!r} is not a port number (1-65535)" in caplog.text


def test_daemon_exits_non_zero_when_the_web_port_is_taken(
    media_library: Path, state_dir: Path, serve: FakeServe, caplog: pytest.LogCaptureFixture
) -> None:
    serve.error = OSError(98, "Address already in use")

    status = run_daemon(environment(media_library, state_dir), sleep=FakeSleep(stop_after=1))

    assert status != 0
    assert "Can't serve the web UI on port 8080: [Errno 98] Address already in use" in caplog.text
    assert "Cycle started" not in caplog.text
