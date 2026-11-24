"""A server dying mid-call, and one that comes back after peers wrote it off
(PLAN slice 8, ADR-003, ADR-004)."""

import asyncio
import time
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import psycopg
import pytest
from conftest import TO, FakeApp, Hook, insert, wait_for_status, write_wav
from psycopg_pool import AsyncConnectionPool

from callapi import db, runner
from callapi.runner import Supervisor


def server(pool: AsyncConnectionPool, tmp_path: Path, name: str, max_calls: int) -> Supervisor:
    return Supervisor(
        pool, server_id=name, max_calls=max_calls, media_dir=tmp_path, recordings_dir=tmp_path
    )


@asynccontextmanager
async def running(sup: Supervisor) -> AsyncIterator[None]:
    task = asyncio.create_task(sup.run())
    try:
        yield
    finally:
        tasks = (task, *sup.calls, *sup.callbacks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_sql(pool: AsyncConnectionPool, query: str, *args: object) -> None:
    async with pool.connection() as conn:
        await conn.execute(query, args)


async def heartbeat_of(pool: AsyncConnectionPool, name: str) -> datetime | None:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT heartbeat_at FROM servers WHERE id = %s", (name,))
        row = await cur.fetchone()
    return None if row is None else row[0]


async def test_only_a_heartbeat_older_than_10s_counts_as_dead(pool: AsyncConnectionPool) -> None:
    live = await insert(pool, "ws://unused/s")
    ringing = await insert(pool, "ws://unused/s")
    await db.claim_calls(pool, "server-a", 2)
    assert await db.answer_call(pool, live.sid, "server-a")
    await run_sql(pool, "INSERT INTO servers VALUES ('server-a', now() - interval '9 s')")

    assert await db.reap_dead_servers(pool) == []

    await run_sql(pool, "UPDATE servers SET heartbeat_at = now() - interval '11 s'")
    reaped = {c.sid: c.status for c in await db.reap_dead_servers(pool)}
    assert reaped == {live.sid: "completed", ringing.sid: "failed"}
    assert await heartbeat_of(pool, "server-a") is None
    assert await db.reap_dead_servers(pool) == []  # each dead server is reaped once


async def test_peer_completes_a_dead_servers_call_and_reports_it(
    pool: AsyncConnectionPool,
    fake_app: FakeApp,
    tmp_path: Path,
    hook: Hook,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "REAP_AFTER_BEATING", 0)  # b's warm-up has its own test
    write_wav(tmp_path / f"{TO}.wav", 10.0)
    a = server(pool, tmp_path, "server-a", max_calls=10)
    b = server(pool, tmp_path, "server-b", max_calls=0)  # never claims, so the call is a's
    async with running(b):
        async with running(a):
            events = ("answered", "completed")
            call = await insert(pool, fake_app.url, status_callback=hook.url, events=events)
            a.poke()
            await wait_for_status(pool, call.sid, "in-progress")
            await hook.wait_for(1)
            await asyncio.sleep(1.2)
        # a is gone without ending its call. Move its world 11s into the past so its
        # heartbeat is stale, and zero the WAV sizes as a crash before close() would.
        await run_sql(
            pool,
            "UPDATE servers SET heartbeat_at = heartbeat_at - interval '11 s' WHERE id = %s",
            "server-a",
        )
        await run_sql(
            pool,
            "UPDATE calls SET date_updated = date_updated - interval '11 s' WHERE sid = %s",
            call.sid,
        )
        last_beat = await heartbeat_of(pool, "server-a")
        with (tmp_path / f"{call.sid}.wav").open("r+b") as f:
            f.seek(4)
            f.write(b"\0\0\0\0")
            f.seek(40)
            f.write(b"\0\0\0\0")

        done = await wait_for_status(pool, call.sid, "completed")
        got = await hook.wait_for(2)

    assert done.end_time == last_beat
    assert done.duration == 1
    with wave.open(str(tmp_path / f"{call.sid}.wav")) as rec:
        assert rec.getnframes() >= 50 * 160  # the second a recorded is downloadable
    first, last = got[0].params, got[1].params
    assert (first["CallStatus"], first["SequenceNumber"]) == ("in-progress", "0")
    assert (last["CallStatus"], last["SequenceNumber"]) == ("completed", "1")
    assert last["CallDuration"] == "1" and last["RecordingSid"] == f"RE{call.sid[2:]}"
    assert got[1].signed


async def test_server_back_from_a_db_outage_waits_before_reaping(
    pool: AsyncConnectionPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An outage leaves every heartbeat stale, live peers' included. Reaping as soon as the
    # database returns would end all their calls, so b first beats as long as the threshold.
    monkeypatch.setattr(runner, "REAP_AFTER_BEATING", 1.5)
    call = await insert(pool, "ws://unused/s")
    await db.claim_calls(pool, "server-a", 1)
    assert await db.answer_call(pool, call.sid, "server-a")
    await run_sql(pool, "INSERT INTO servers VALUES ('server-a', now() - interval '11 s')")

    outage_ticks = 2
    real_heartbeat = db.heartbeat

    async def heartbeat_during_outage(pool: AsyncConnectionPool, server_id: str) -> bool:
        nonlocal outage_ticks
        if outage_ticks:
            outage_ticks -= 1
            raise psycopg.OperationalError("database is down")
        return await real_heartbeat(pool, server_id)

    monkeypatch.setattr(db, "heartbeat", heartbeat_during_outage)
    b = server(pool, tmp_path, "server-b", max_calls=0)
    async with running(b):
        await asyncio.sleep(3.5)  # back at ~2s, so ~1.5s of beating at most
        held = await db.get_call(pool, call.sid)
        assert held is not None and held.status == "in-progress"
        await wait_for_status(pool, call.sid, "completed")  # a never beat again: reaped


async def test_reaped_server_that_wakes_up_stops_its_calls(
    pool: AsyncConnectionPool,
    fake_app: FakeApp,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pause that stalls a's heartbeat (a GC pause, a partition) while its call streams on.
    paused, resume = asyncio.Event(), asyncio.Event()
    real_heartbeat = db.heartbeat

    async def stalled_heartbeat(pool: AsyncConnectionPool, server_id: str) -> bool:
        if paused.is_set():
            await resume.wait()
        return await real_heartbeat(pool, server_id)

    monkeypatch.setattr(db, "heartbeat", stalled_heartbeat)
    write_wav(tmp_path / f"{TO}.wav", 10.0)
    a = server(pool, tmp_path, "server-a", max_calls=10)
    b = server(pool, tmp_path, "server-b", max_calls=0)
    async with running(a):
        call = await insert(pool, fake_app.url)
        a.poke()
        await wait_for_status(pool, call.sid, "in-progress")
        paused.set()
        await asyncio.sleep(1.2)  # a's next tick is now stuck in its heartbeat

        await run_sql(pool, "UPDATE servers SET heartbeat_at = now() - interval '11 s'")
        await b.reap()
        reaped = await wait_for_status(pool, call.sid, "completed")
        assert fake_app.closed_at is None  # the zombie is still streaming

        woke_at = time.monotonic()
        resume.set()
        async with asyncio.timeout(2):
            while fake_app.closed_at is None:  # noqa: ASYNC110 - set by the app's handler
                await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)
    await b.http.aclose()

    assert fake_app.closed_at is not None and fake_app.closed_at - woke_at < 1.5
    assert fake_app.events()[-1] == "stop"
    after = await db.get_call(pool, call.sid)
    assert after is not None
    # Fenced: the zombie's own ending matched 0 rows, so the reaper's record stands.
    assert (after.status, after.end_time, after.duration) == (
        "completed",
        reaped.end_time,
        reaped.duration,
    )
    assert await heartbeat_of(pool, "server-a") is not None  # it rejoined as a live server
