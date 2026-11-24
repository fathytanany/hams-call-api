"""Runs calls: claims them from Postgres and streams each callee WAV to the app in real time."""

import asyncio
import base64
import itertools
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from callapi import config, db
from callapi.audio import MediaReader

log = logging.getLogger(__name__)

FRAME_SECONDS = 0.02
POLL_SECONDS = 1.0  # claim interval when nobody pokes us (ADR-003)
CONNECT_TIMEOUT = 10  # ASSUMPTIONS 12


async def paced(frames: Iterator[bytes]) -> AsyncIterator[bytes]:
    """Yields frame n at t0 + n * 20 ms, then waits out the last frame's 20 ms.

    Deadlines are absolute, so sleep error never accumulates; after a stall the late
    frames go out back to back until we are on schedule again (ADR-005).
    """
    t0 = time.monotonic()
    n = 0
    for n, frame in enumerate(frames, 1):
        if (delay := t0 + (n - 1) * FRAME_SECONDS - time.monotonic()) > 0:
            await asyncio.sleep(delay)
        yield frame
    if (delay := t0 + n * FRAME_SECONDS - time.monotonic()) > 0:
        await asyncio.sleep(delay)


async def stream(ws: ClientConnection, call: db.Call, media: MediaReader) -> None:
    """Media Streams messages, TWILIO_CONTRACT §5."""
    stream_sid = f"MZ{uuid.uuid4().hex}"
    seq = itertools.count(1)

    async def send(event: str, **body: Any) -> None:
        msg = {"event": event, "sequenceNumber": str(next(seq)), "streamSid": stream_sid}
        await ws.send(json.dumps(msg | body))

    await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
    await send(
        "start",
        start={
            "accountSid": config.ACCOUNT_SID,
            "streamSid": stream_sid,
            "callSid": call.sid,
            "tracks": ["inbound"],
            "customParameters": call.stream_params,
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        },
    )
    chunk = 0
    async for frame in paced(iter(media.read_frame, None)):
        chunk += 1
        media_body = {
            "track": "inbound",
            "chunk": str(chunk),
            "timestamp": str((chunk - 1) * 20),
            "payload": base64.b64encode(frame).decode(),
        }
        await send("media", media=media_body)
    await send("stop", stop={"accountSid": config.ACCOUNT_SID, "callSid": call.sid})


async def run_call(pool: AsyncConnectionPool, call: db.Call, owner: str, media_dir: Path) -> None:
    with MediaReader(media_dir / f"{call.to_number}.wav") as media:
        answered = await db.answer_call(pool, call.sid, owner)
        if answered is None:
            return  # another server reaped it while we were ringing
        try:
            async with connect(call.stream_url, open_timeout=CONNECT_TIMEOUT) as ws:
                await stream(ws, call, media)
        except (OSError, TimeoutError, WebSocketException) as e:
            # The callee already answered, so a broken stream still ends `completed` (ADR-010).
            log.warning("call %s: stream to %s ended early: %r", call.sid, call.stream_url, e)
        await db.complete_call(pool, call.sid, owner, answered)


class Supervisor:
    """Claims queued calls up to max_calls and runs one task per call."""

    def __init__(
        self, pool: AsyncConnectionPool, *, server_id: str, max_calls: int, media_dir: Path
    ) -> None:
        self.pool, self.server_id, self.max_calls, self.media_dir = (
            pool,
            server_id,
            max_calls,
            media_dir,
        )
        self.calls: set[asyncio.Task[None]] = set()
        self._poke = asyncio.Event()

    def poke(self) -> None:
        """Claim now instead of on the next poll: a call created here starts immediately."""
        self._poke.set()

    def _done(self, task: asyncio.Task[None]) -> None:
        self.calls.discard(task)
        if not task.cancelled() and (e := task.exception()):
            log.error("call task %s crashed", task.get_name(), exc_info=e)
        self.poke()  # a slot is free

    async def run(self) -> None:
        while True:
            self._poke.clear()  # before claiming, so a poke during the claim isn't lost
            if (free := self.max_calls - len(self.calls)) > 0:
                try:
                    claimed = await db.claim_calls(self.pool, self.server_id, free)
                except psycopg.Error:
                    log.exception("claiming calls failed; retrying")
                    claimed = []
                for call in claimed:
                    task = asyncio.create_task(
                        run_call(self.pool, call, self.server_id, self.media_dir), name=call.sid
                    )
                    self.calls.add(task)
                    task.add_done_callback(self._done)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._poke.wait(), POLL_SECONDS)
