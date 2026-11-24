import asyncio
import json
import time
from typing import Any

from callapi.runner import FRAME_SECONDS, OUTBOX_MAX, Outbox, paced


async def test_slow_app_loses_oldest_media_but_never_marks_stop_or_schedule() -> None:
    sent: list[dict[str, Any]] = []

    async def slow_send(raw: str) -> None:
        await asyncio.sleep(2 * FRAME_SECONDS)  # the app reads at half speed
        sent.append(json.loads(raw))

    outbox = Outbox("MZ1")
    sender = asyncio.create_task(outbox.drain(slow_send))
    t0 = time.monotonic()
    late, n = 0.0, 0
    async for _ in paced(iter([b"x"] * 150)):
        n += 1
        late = max(late, time.monotonic() - t0 - (n - 1) * FRAME_SECONDS)
        outbox.put("media", {"chunk": str(n)})
        if n == 100:
            outbox.put("mark", {"name": "m"})
    outbox.put("stop", {})
    await asyncio.wait_for(sender, 5)  # drain returns once stop is out

    assert late < 0.04  # the pacer never waited on the app
    assert outbox.dropped > 0
    chunks = [int(m["media"]["chunk"]) for m in sent if m["event"] == "media"]
    assert chunks == sorted(chunks) and len(chunks) == 150 - outbox.dropped
    assert chunks[-1] == 150  # the newest audio survives; stale audio is what goes
    assert [m["event"] for m in sent if m["event"] != "media"] == ["mark", "stop"]
    assert sent[-1]["event"] == "stop"
    # Numbered at send time, so the app sees no gaps even when media was dropped.
    assert [m["sequenceNumber"] for m in sent] == [str(i) for i in range(1, len(sent) + 1)]
    assert all(m["streamSid"] == "MZ1" for m in sent)


def test_full_outbox_of_marks_still_accepts_marks() -> None:
    outbox = Outbox("MZ1")
    for i in range(OUTBOX_MAX + 5):
        outbox.put("mark", {"name": str(i)})
    assert outbox.dropped == 0
