"""Every way a call can end (PLAN slice 5, ADR-010, ASSUMPTIONS 13)."""

import asyncio
import logging
import socket
import time
import wave
from pathlib import Path
from typing import Any

import psycopg
import pytest
from conftest import TO, FakeApp, insert, wait_for_status, write_wav
from psycopg_pool import AsyncConnectionPool
from websockets.asyncio.server import ServerConnection

from callapi import db, runner
from callapi.runner import Supervisor


async def start_call(
    pool: AsyncConnectionPool, sup: Supervisor, url: str, ring_timeout: int = 60
) -> db.Call:
    call = await insert(pool, url, ring_timeout=ring_timeout)
    sup.poke()
    return call


@pytest.mark.parametrize("local", [True, False], ids=["same-server", "other-server"])
async def test_api_hangup_mid_call_sends_stop_and_closes(
    pool: AsyncConnectionPool,
    fake_app: FakeApp,
    supervisor: Supervisor,
    tmp_path: Path,
    local: bool,
) -> None:
    write_wav(tmp_path / f"{TO}.wav", 10.0)
    call = await start_call(pool, supervisor, fake_app.url)
    await wait_for_status(pool, call.sid, "in-progress")
    await asyncio.sleep(0.5)

    hung_up_at = time.monotonic()
    await db.hang_up(pool, call.sid, "completed")
    if local:  # what the API route adds when it runs on the owning server
        supervisor.hang_up(call.sid)
    done = await wait_for_status(pool, call.sid, "completed")

    assert time.monotonic() - hung_up_at < (0.3 if local else 1.2)  # ≤1s via the poll
    await asyncio.sleep(0.1)
    assert fake_app.events()[-1] == "stop"
    assert fake_app.closed_at is not None
    assert done.duration is not None and done.duration < 10


async def test_hangup_while_ringing_cancels(
    pool: AsyncConnectionPool, supervisor: Supervisor, tmp_path: Path
) -> None:
    (tmp_path / f"{TO}.no-answer").touch()
    call = await start_call(pool, supervisor, "ws://unused/s", ring_timeout=30)
    await wait_for_status(pool, call.sid, "ringing")
    await db.hang_up(pool, call.sid, "canceled")
    supervisor.hang_up(call.sid)
    done = await wait_for_status(pool, call.sid, "canceled")
    assert done.end_time is None and done.duration is None


async def test_app_closing_the_socket_completes_without_stop(
    pool: AsyncConnectionPool, fake_app: FakeApp, supervisor: Supervisor, tmp_path: Path
) -> None:
    write_wav(tmp_path / f"{TO}.wav", 10.0)

    async def hang_up_after_10_frames(ws: ServerConnection, msg: dict[str, Any]) -> None:
        if msg["event"] == "media" and msg["media"]["chunk"] == "10":
            await ws.close()

    fake_app.on_message = hang_up_after_10_frames
    call = await start_call(pool, supervisor, fake_app.url)
    done = await wait_for_status(pool, call.sid, "completed")
    assert "stop" not in fake_app.events()
    assert done.duration == 0  # ~0.2s of a 10s file


def write_stereo(path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0" * 32000)


@pytest.mark.parametrize(
    ("setup", "status", "seconds"),
    [
        (lambda d: None, "failed", 0),  # no WAV for this number
        (lambda d: write_stereo(d / f"{TO}.wav"), "failed", 0),
        (lambda d: (d / f"{TO}.wav").write_bytes(b"not a wav"), "failed", 0),
        (lambda d: (d / f"{TO}.busy").touch(), "busy", 0),
        (lambda d: (d / f"{TO}.no-answer").touch(), "no-answer", 2),  # rings for Timeout
    ],
    ids=["missing", "stereo", "garbage", "busy", "no-answer"],
)
async def test_callee_outcomes(
    pool: AsyncConnectionPool,
    supervisor: Supervisor,
    tmp_path: Path,
    setup: Any,
    status: str,
    seconds: float,
) -> None:
    setup(tmp_path)
    started = time.monotonic()
    call = await start_call(pool, supervisor, "ws://unused/s", ring_timeout=2)
    done = await wait_for_status(pool, call.sid, status)
    assert abs(time.monotonic() - started - seconds) < 0.5
    assert done.end_time is None and done.duration is None  # only completed calls get them
    assert done.start_time is not None


def refused_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"ws://127.0.0.1:{port}/s"  # nothing listens there any more


async def test_app_refusing_the_connection_still_completes(
    pool: AsyncConnectionPool,
    supervisor: Supervisor,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    write_wav(tmp_path / f"{TO}.wav", 2.0)
    call = await start_call(pool, supervisor, refused_url())
    await wait_for_status(pool, call.sid, "completed")
    assert "stream to ws://127.0.0.1" in caplog.text


async def test_app_never_finishing_the_handshake_still_completes(
    pool: AsyncConnectionPool,
    supervisor: Supervisor,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "CONNECT_TIMEOUT", 0.5)
    write_wav(tmp_path / f"{TO}.wav", 2.0)

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()  # accepts TCP, never answers the HTTP upgrade
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        started = time.monotonic()
        call = await start_call(pool, supervisor, f"ws://127.0.0.1:{port}/s")
        await wait_for_status(pool, call.sid, "completed")
        assert time.monotonic() - started < 2
    assert "TimeoutError" in caplog.text


async def test_slow_app_does_not_slow_the_call(
    pool: AsyncConnectionPool, fake_app: FakeApp, supervisor: Supervisor, tmp_path: Path
) -> None:
    write_wav(tmp_path / f"{TO}.wav", 3.0)

    async def half_speed(ws: ServerConnection, msg: dict[str, Any]) -> None:
        await asyncio.sleep(runner.FRAME_SECONDS * 2)

    fake_app.on_message = half_speed
    started = time.monotonic()
    call = await start_call(pool, supervisor, fake_app.url)
    await wait_for_status(pool, call.sid, "completed")
    # On schedule: 3s of file plus at most the stop flush, not the ~6s the app needs.
    assert time.monotonic() - started < 3 + runner.FLUSH_TIMEOUT + 0.5
    with wave.open(str(tmp_path / f"{call.sid}.wav")) as w:
        assert w.getnframes() == 3 * 8000  # the recording kept the call's clock


async def test_dead_app_is_closed_by_keepalive(
    pool: AsyncConnectionPool,
    fake_app: FakeApp,
    supervisor: Supervisor,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(runner, "PING_INTERVAL", 0.2)
    monkeypatch.setattr(runner, "PING_TIMEOUT", 0.2)
    monkeypatch.setattr(runner, "CLOSE_TIMEOUT", 0.5)
    write_wav(tmp_path / f"{TO}.wav", 10.0)
    release = asyncio.Event()

    async def stop_reading(ws: ServerConnection, msg: dict[str, Any]) -> None:
        if msg["event"] == "start":
            await release.wait()  # socket stays open, nothing is read, pings go unanswered

    fake_app.on_message = stop_reading
    caplog.set_level(logging.INFO)
    started = time.monotonic()
    call = await start_call(pool, supervisor, fake_app.url)
    try:
        await wait_for_status(pool, call.sid, "completed")
    finally:
        release.set()  # or the fake app's teardown waits on this handler forever
    # ping interval + ping timeout + close timeout, not the 10s of the file.
    assert time.monotonic() - started < 3
    assert "stop" not in fake_app.events()


def flaky(monkeypatch: pytest.MonkeyPatch, name: str, failures: int) -> None:
    """Makes db.<name> raise like an unreachable database for its first `failures` calls."""
    real = getattr(db, name)
    left = [failures]

    async def write(*args: Any) -> Any:
        if left[0]:
            left[0] -= 1
            raise psycopg.OperationalError("database is down")
        return await real(*args)

    monkeypatch.setattr(db, name, write)


@pytest.mark.parametrize("write", ["answer_call", "complete_call"])
async def test_status_write_survives_a_database_outage(
    pool: AsyncConnectionPool,
    fake_app: FakeApp,
    supervisor: Supervisor,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write: str,
) -> None:
    monkeypatch.setattr(runner, "POLL_SECONDS", 0.5)
    flaky(monkeypatch, write, failures=3)
    write_wav(tmp_path / f"{TO}.wav", 1.0)
    call = await start_call(pool, supervisor, fake_app.url)
    done = await wait_for_status(pool, call.sid, "completed")
    # The 1.5s spent retrying is not call time.
    assert done.duration == 1
    assert done.end_time is not None and done.start_time is not None


async def test_unanswered_write_survives_a_database_outage(
    pool: AsyncConnectionPool,
    supervisor: Supervisor,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "POLL_SECONDS", 0.1)
    flaky(monkeypatch, "end_unanswered", failures=2)
    (tmp_path / f"{TO}.busy").touch()
    call = await start_call(pool, supervisor, "ws://unused/s")
    await wait_for_status(pool, call.sid, "busy")
