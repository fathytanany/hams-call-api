"""The demo app (PLAN slice 7) driven by a real Supervisor."""

import wave
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from conftest import TO, insert, wait_for_status, write_wav
from psycopg_pool import AsyncConnectionPool
from websockets.asyncio.server import serve

from callapi import testapp
from callapi.runner import Supervisor


@pytest.fixture
async def app_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    monkeypatch.setattr(testapp, "OUT_DIR", tmp_path / "out")
    (tmp_path / "out").mkdir()
    async with serve(testapp.handle, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}/stream"


def frames(path: Path) -> int:
    with wave.open(str(path)) as w:
        return int(w.getnframes()) // 160


async def test_normal_mode_saves_what_it_hears_and_echoes_it(
    pool: AsyncConnectionPool, supervisor: Supervisor, tmp_path: Path, app_url: str
) -> None:
    write_wav(tmp_path / f"{TO}.wav", 2.0)
    call = await insert(pool, app_url)
    supervisor.poke()
    done = await wait_for_status(pool, call.sid, "completed")

    assert frames(tmp_path / "out" / f"{call.sid}.wav") == 100
    # The echo comes back a tick or two late, so the recording ends slightly short of it.
    with wave.open(str(tmp_path / f"{call.sid}.wav")) as rec:
        pcm = rec.readframes(rec.getnframes())
    assert rec.getnframes() == 100 * 160
    assert pcm.count(b"\x00\x00") < len(pcm) // 4  # mostly echoed audio, not silence
    assert done.duration == 2


async def test_disconnect_mode_hangs_up_after_the_given_seconds(
    pool: AsyncConnectionPool, supervisor: Supervisor, tmp_path: Path, app_url: str
) -> None:
    write_wav(tmp_path / f"{TO}.wav", 10.0)
    params = {"mode": "disconnect", "disconnect_after": "0.5"}
    call = await insert(pool, app_url, params=params)
    supervisor.poke()
    done = await wait_for_status(pool, call.sid, "completed")

    assert frames(tmp_path / "out" / f"{call.sid}.wav") == 25
    assert done.duration is not None and done.duration <= 1
