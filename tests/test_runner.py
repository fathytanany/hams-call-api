import asyncio
import base64
import struct
import uuid
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from conftest import FakeApp
from psycopg_pool import AsyncConnectionPool

from callapi import config, db
from callapi.main import call_json
from callapi.runner import Supervisor

TO = "+15550001"


def write_wav(path: Path, seconds: float, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 1000) * int(rate * seconds))


async def insert(pool: AsyncConnectionPool, url: str, to: str = TO) -> db.Call:
    return await db.insert_call(
        pool,
        sid=f"CA{uuid.uuid4().hex}",
        to=to,
        from_="+15550000",
        stream_url=url,
        stream_params={"mode": "normal"},
        status_callback=None,
        status_callback_method="POST",
        status_callback_events=["completed"],
        ring_timeout=60,
    )


async def wait_for_status(pool: AsyncConnectionPool, sid: str, status: str) -> db.Call:
    async with asyncio.timeout(10):
        while (call := await db.get_call(pool, sid)) is None or call.status != status:  # noqa: ASYNC110 - status lives in Postgres
            await asyncio.sleep(0.05)
    return call


@pytest.fixture
async def supervisor(pool: AsyncConnectionPool, tmp_path: Path) -> AsyncIterator[Supervisor]:
    sup = Supervisor(pool, server_id="test-server", max_calls=10, media_dir=tmp_path)
    task = asyncio.create_task(sup.run())
    yield sup
    for t in (task, *sup.calls):
        t.cancel()
    await asyncio.gather(task, *sup.calls, return_exceptions=True)


async def test_two_second_file_streams_100_frames_then_completes(
    pool: AsyncConnectionPool, fake_app: FakeApp, supervisor: Supervisor, tmp_path: Path
) -> None:
    write_wav(tmp_path / f"{TO}.wav", 2.0, rate=16000)
    call = await insert(pool, fake_app.url)
    supervisor.poke()

    done = await wait_for_status(pool, call.sid, "completed")
    await asyncio.sleep(0.05)  # let the app's last read land

    msgs = [m for _, m in fake_app.received]
    assert fake_app.events() == ["connected", "start", *["media"] * 100, "stop"]
    assert msgs[0] == {"event": "connected", "protocol": "Call", "version": "1.0.0"}

    start = msgs[1]
    stream_sid = start["streamSid"]
    assert stream_sid.startswith("MZ") and len(stream_sid) == 34
    assert start["sequenceNumber"] == "1"
    assert start["start"] == {
        "accountSid": config.ACCOUNT_SID,
        "streamSid": stream_sid,
        "callSid": call.sid,
        "tracks": ["inbound"],
        "customParameters": {"mode": "normal"},
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
    }

    media = msgs[2:102]
    for i, m in enumerate(media):
        assert m["sequenceNumber"] == str(i + 2)
        assert m["streamSid"] == stream_sid
        assert m["media"]["track"] == "inbound"
        assert m["media"]["chunk"] == str(i + 1)
        assert m["media"]["timestamp"] == str(i * 20)
        assert len(base64.b64decode(m["media"]["payload"])) == 160

    assert msgs[102] == {
        "event": "stop",
        "sequenceNumber": "102",
        "streamSid": stream_sid,
        "stop": {"accountSid": config.ACCOUNT_SID, "callSid": call.sid},
    }

    # Media arrived in real time: the last frame ~1.98s after the first.
    times = [t for t, m in fake_app.received if m["event"] == "media"]
    assert abs(times[-1] - times[0] - 1.98) < 0.04

    assert done.owner == "test-server"
    assert done.duration == 2
    assert done.start_time and done.end_time and done.queue_time is not None
    assert call_json(done)["duration"] == "2"


async def test_claims_never_overlap(pool: AsyncConnectionPool) -> None:
    sids = {(await insert(pool, "ws://unused/s", to=f"+1555000{i}")).sid for i in range(10)}
    async with (
        AsyncConnectionPool(config.DATABASE_URL) as a,
        AsyncConnectionPool(config.DATABASE_URL) as b,
    ):
        got_a, got_b = await asyncio.gather(
            db.claim_calls(a, "server-a", 6), db.claim_calls(b, "server-b", 6)
        )
    claimed_a, claimed_b = {c.sid for c in got_a}, {c.sid for c in got_b}
    assert not claimed_a & claimed_b
    assert claimed_a | claimed_b == sids
    assert {c.status for c in got_a + got_b} == {"ringing"}


async def test_capacity_leaves_extra_calls_queued(
    pool: AsyncConnectionPool, fake_app: FakeApp, tmp_path: Path
) -> None:
    write_wav(tmp_path / f"{TO}.wav", 1.0)
    sup = Supervisor(pool, server_id="small", max_calls=1, media_dir=tmp_path)
    first, second = await insert(pool, fake_app.url), await insert(pool, fake_app.url)
    task = asyncio.create_task(sup.run())
    try:
        await wait_for_status(pool, first.sid, "in-progress")
        assert (await db.get_call(pool, second.sid)).status == "queued"  # type: ignore[union-attr]
        # The finished call frees the slot and pokes the claimer: no 1s poll wait.
        done = await wait_for_status(pool, second.sid, "completed")
        assert done.queue_time is not None and done.queue_time >= 1000
    finally:
        task.cancel()
        await asyncio.gather(task, *sup.calls, return_exceptions=True)
