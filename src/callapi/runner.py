"""Runs calls: claims them from Postgres and streams each callee WAV to the app in real time."""

import asyncio
import base64
import itertools
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg_pool import AsyncConnectionPool
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from callapi import callbacks, config, db
from callapi.audio import (
    MediaFormatError,
    MediaReader,
    Playout,
    RecordingWriter,
    patch_wav_header,
)

log = logging.getLogger(__name__)

FRAME_SECONDS = 0.02
# Claim, hangup-flag, heartbeat and reap interval when nobody pokes us (ADR-003).
POLL_SECONDS = 1.0
CONNECT_TIMEOUT = 10.0  # ASSUMPTIONS 12
# websockets' keepalive: an app that stops reading stops answering pings and is closed.
PING_INTERVAL = 20.0
PING_TIMEOUT = 20.0
# After a missed pong the close frame can't drain to an app that isn't reading, and the
# socket is only aborted after this; websockets' default of 10s would hold a dead call open.
CLOSE_TIMEOUT = 2.0
OUTBOX_MAX = 50  # ~1s of media (ADR-006)
FLUSH_TIMEOUT = 1.0  # how long `stop` may wait behind a slow app's backlog


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


class Outbox:
    """Service -> app messages after `connected`. The pacer puts without waiting and a
    sender task drains to the socket, so a slow app never delays the clock, marks or
    recording. Past ~1s of backlog the oldest media is dropped: stale audio is worthless
    to a voice agent, and marks and stop are never dropped (ADR-006)."""

    def __init__(self, stream_sid: str) -> None:
        self.stream_sid = stream_sid
        self.dropped = 0
        self._queue: deque[tuple[str, dict[str, Any]]] = deque()
        self._ready = asyncio.Event()

    def put(self, event: str, body: dict[str, Any]) -> None:
        if len(self._queue) >= OUTBOX_MAX:
            for i, (queued, _) in enumerate(self._queue):
                if queued == "media":
                    del self._queue[i]
                    self.dropped += 1
                    break
        self._queue.append((event, body))
        self._ready.set()

    async def drain(self, send: Callable[[str], Awaitable[None]]) -> None:
        """Sends in order until `stop` is out. Numbered here, so drops leave no gaps."""
        for seq in itertools.count(1):
            while not self._queue:
                self._ready.clear()
                await self._ready.wait()
            event, body = self._queue.popleft()
            msg = {"event": event, "sequenceNumber": str(seq), "streamSid": self.stream_sid}
            await send(json.dumps(msg | {event: body}))
            if event == "stop":
                return


async def receive(ws: ClientConnection, stream_sid: str, playout: Playout, outbox: Outbox) -> None:
    """App -> service messages: media and marks queue for playout, clear empties it.
    Returns when the app closes the socket."""
    with suppress(ConnectionClosed):
        async for raw in ws:
            try:
                msg = json.loads(raw)
                if msg.get("streamSid") != stream_sid:
                    raise ValueError(f"streamSid {msg.get('streamSid')!r}")
                match msg["event"]:
                    case "media":
                        playout.add_audio(base64.b64decode(msg["media"]["payload"], validate=True))
                    case "mark":
                        playout.add_mark(str(msg["mark"]["name"]))
                    case "clear":
                        for name in playout.clear():
                            outbox.put("mark", {"name": name})
                    case event:
                        raise ValueError(f"unknown event {event!r}")
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                # A bad message is ignored; the call goes on (ASSUMPTIONS 17).
                log.warning("stream %s: ignored app message: %r", stream_sid, e)


async def stream(
    ws: ClientConnection,
    call: db.Call,
    media: MediaReader,
    recording: RecordingWriter,
    hangup: asyncio.Event,
) -> None:
    """Media Streams messages, TWILIO_CONTRACT §5. Each tick sends one frame of the file
    and plays one frame of the app's audio into the recording. Ends when the file ends,
    the call is hung up via the API, or the app goes away (ASSUMPTIONS 13)."""
    stream_sid = f"MZ{uuid.uuid4().hex}"
    await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
    outbox, playout = Outbox(stream_sid), Playout()
    outbox.put(
        "start",
        {
            "accountSid": config.ACCOUNT_SID,
            "streamSid": stream_sid,
            "callSid": call.sid,
            "tracks": ["inbound"],
            "customParameters": call.stream_params,
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        },
    )
    sender = asyncio.create_task(outbox.drain(ws.send))
    receiver = asyncio.create_task(receive(ws, stream_sid, playout, outbox))
    chunk, late, t0 = 0, 0.0, time.monotonic()
    try:
        async for frame in paced(iter(media.read_frame, None)):
            # How far behind schedule the clock ran: the real-time number the demo reports.
            late = max(late, time.monotonic() - t0 - chunk * FRAME_SECONDS)
            if hangup.is_set() or sender.done() or receiver.done():
                break
            chunk += 1
            outbox.put(
                "media",
                {
                    "track": "inbound",
                    "chunk": str(chunk),
                    "timestamp": str((chunk - 1) * 20),
                    "payload": base64.b64encode(frame).decode(),
                },
            )
            played, marks = playout.play()
            recording.write(played)
            for name in marks:
                outbox.put("mark", {"name": name})
        if sender.done() or receiver.done():
            log.info("call %s: the app closed the stream", call.sid)
        else:
            outbox.put("stop", {"accountSid": config.ACCOUNT_SID, "callSid": call.sid})
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(sender), FLUSH_TIMEOUT)
    finally:
        sender.cancel()
        receiver.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)
        log.info(
            "call %s: streamed %d frames, max pacing lateness %.1f ms", call.sid, chunk, late * 1000
        )
        if outbox.dropped or playout.dropped_bytes:
            log.warning(
                "call %s: dropped %d media frames to a slow app, %d bytes over the playout cap",
                call.sid,
                outbox.dropped,
                playout.dropped_bytes,
            )


class Supervisor:
    """Claims queued calls up to max_calls and runs one task per call."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        server_id: str,
        max_calls: int,
        media_dir: Path,
        recordings_dir: Path,
    ) -> None:
        self.pool = pool
        self.server_id = server_id
        self.max_calls = max_calls
        self.media_dir = media_dir
        self.recordings_dir = recordings_dir
        self.calls: set[asyncio.Task[None]] = set()
        # Callback senders outlive their call, so a slow receiver never holds a call slot.
        self.callbacks: set[asyncio.Task[None]] = set()
        self.http = httpx.AsyncClient(timeout=callbacks.TIMEOUT)
        self._hangups: dict[str, asyncio.Event] = {}  # by CallSid, for calls we run
        self._poke = asyncio.Event()

    def poke(self) -> None:
        """Claim now instead of on the next poll: a call created here starts immediately."""
        self._poke.set()

    def hang_up(self, sid: str) -> None:
        """Ends a call this server runs at once; other servers see the flag on their poll."""
        if event := self._hangups.get(sid):
            event.set()

    def _callbacks(self, call: db.Call, seq: int = 0) -> callbacks.StatusCallbacks:
        status_callbacks = callbacks.StatusCallbacks(call, self.http, seq)
        self.callbacks.add(status_callbacks.task)
        status_callbacks.task.add_done_callback(self.callbacks.discard)
        return status_callbacks

    def _done(self, task: asyncio.Task[None]) -> None:
        self.calls.discard(task)
        self._hangups.pop(task.get_name(), None)
        if not task.cancelled() and (e := task.exception()):
            log.error("call task %s crashed", task.get_name(), exc_info=e)
        self.poke()  # a slot is free

    async def run(self) -> None:
        try:
            await self._claim_forever()
        finally:
            await self.http.aclose()

    async def _claim_forever(self) -> None:
        while True:
            self._poke.clear()  # before claiming, so a poke during the claim isn't lost
            try:
                if not await db.heartbeat(self.pool, self.server_id) and self._hangups:
                    # Peers wrote us off (a long pause or a partition) and already ended
                    # these calls; our own writes would match 0 rows, so stop streaming.
                    log.error("declared dead by peers; stopping %d calls", len(self._hangups))
                    for event in self._hangups.values():
                        event.set()
                if (free := self.max_calls - len(self.calls)) > 0:
                    for call in await db.claim_calls(self.pool, self.server_id, free):
                        self._hangups[call.sid] = asyncio.Event()
                        task = asyncio.create_task(self._run_call(call), name=call.sid)
                        self.calls.add(task)
                        task.add_done_callback(self._done)
                if self._hangups:
                    for sid in await db.hangups_requested(self.pool, self.server_id):
                        self.hang_up(sid)
                await self.reap()
            except psycopg.Error:
                log.exception("supervisor tick failed; retrying")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._poke.wait(), POLL_SECONDS)

    async def reap(self) -> None:
        """Ends the calls of dead peers as their owner would have (ADR-004)."""
        for call in await db.reap_dead_servers(self.pool):
            log.warning("call %s: its server died; ended as %s", call.sid, call.status)
            answered = call.status == "completed"
            # Callbacks the dead owner fired; events go in a fixed order, so the count
            # follows from how far the call got (ADR-009).
            fired = ("initiated", "ringing", "answered") if answered else ("initiated", "ringing")
            seq = sum(e in call.status_callback_events for e in fired)
            status_callbacks = self._callbacks(call, seq)
            if answered:
                assert call.duration is not None  # set by the reap for answered calls
                recording = self.recordings_dir / f"{call.sid}.wav"
                try:
                    patch_wav_header(recording)  # the owner died before close() fixed it
                except OSError as e:  # never opened, or unreadable: the call still ends
                    log.warning("call %s: no recording to patch: %s", call.sid, e)
                extra = callbacks.completed(call.sid, call.duration, recording)
                status_callbacks.fire("completed", "completed", extra)
            else:
                status_callbacks.fire("completed", call.status, callbacks.unanswered(call.status))
            status_callbacks.close()

    async def _run_call(self, call: db.Call) -> None:
        status_callbacks = self._callbacks(call)
        # Claimed means dialing and ringing at once (TWILIO_CONTRACT §2).
        status_callbacks.fire("initiated", "initiated")
        status_callbacks.fire("ringing", "ringing")
        try:
            await self._ring_and_stream(call, status_callbacks)
        finally:
            status_callbacks.close()

    async def _end_unanswered(
        self, sid: str, status: str, status_callbacks: callbacks.StatusCallbacks
    ) -> None:
        # A callback only follows our own write, so a reaped call is never reported twice.
        if await db.end_unanswered(self.pool, sid, self.server_id, status):
            status_callbacks.fire("completed", status, callbacks.unanswered(status))

    async def _ring_and_stream(
        self, call: db.Call, status_callbacks: callbacks.StatusCallbacks
    ) -> None:
        sid, owner, hangup = call.sid, self.server_id, self._hangups[call.sid]
        # The callee is a file: markers decide busy and no-answer (ADR-010).
        callee = self.media_dir / call.to_number
        if callee.with_name(f"{call.to_number}.busy").exists():
            await self._end_unanswered(sid, "busy", status_callbacks)
            return
        if callee.with_name(f"{call.to_number}.no-answer").exists():
            with suppress(TimeoutError):
                await asyncio.wait_for(hangup.wait(), call.ring_timeout)
            status = "canceled" if hangup.is_set() else "no-answer"
            await self._end_unanswered(sid, status, status_callbacks)
            return
        try:
            media = MediaReader(callee.with_name(f"{call.to_number}.wav"))
        except (OSError, MediaFormatError) as e:
            log.warning("call %s failed: %s", sid, e)
            await self._end_unanswered(sid, "failed", status_callbacks)
            return

        recording_path = self.recordings_dir / f"{sid}.wav"
        with media:
            answered = await db.answer_call(self.pool, sid, owner)
            if answered is None:
                # Hung up while ringing (a no-op if another server reaped the call instead).
                await self._end_unanswered(sid, "canceled", status_callbacks)
                return
            status_callbacks.fire("answered", "in-progress")
            try:
                async with connect(
                    call.stream_url,
                    open_timeout=CONNECT_TIMEOUT,
                    ping_interval=PING_INTERVAL,
                    ping_timeout=PING_TIMEOUT,
                    close_timeout=CLOSE_TIMEOUT,
                ) as ws:
                    # Opened at `start`, so the recording lines up with the stream's timestamps.
                    with RecordingWriter(recording_path) as recording:
                        await stream(ws, call, media, recording, hangup)
            except (OSError, TimeoutError, WebSocketException) as e:
                # The callee already answered, so a failed stream still ends `completed`
                # (ADR-010).
                log.warning("call %s: stream to %s failed: %r", sid, call.stream_url, e)
            duration = await db.complete_call(self.pool, sid, owner, answered)
            if duration is not None:
                status_callbacks.fire(
                    "completed", "completed", callbacks.completed(sid, duration, recording_path)
                )
