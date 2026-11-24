import time

from callapi.runner import FRAME_SECONDS, paced

# Real clock on purpose: sleep error is exactly what these tests guard against.
TOLERANCE = 0.04


async def arrivals(n: int, stall_at: int | None = None) -> tuple[list[float], float]:
    t0 = time.monotonic()
    seen: list[float] = []
    async for _ in paced(iter([b"x"] * n)):
        seen.append(time.monotonic() - t0)
        if len(seen) - 1 == stall_at:
            time.sleep(0.2)  # noqa: ASYNC251 - blocks the loop on purpose, like a GC pause
    return seen, time.monotonic() - t0


async def test_250_frames_take_5s_without_drift() -> None:
    seen, total = await arrivals(250)
    assert len(seen) == 250
    # Every frame is due at n * 20 ms; none early, none drifting late.
    lateness = [t - n * FRAME_SECONDS for n, t in enumerate(seen)]
    assert min(lateness) >= -0.001
    assert max(lateness) < TOLERANCE
    # The generator ends when the last frame has played, not when it was sent.
    assert abs(total - 5.0) < TOLERANCE


async def test_a_stall_is_caught_up_not_reanchored() -> None:
    seen, total = await arrivals(100, stall_at=25)
    # The ~10 frames due during the stall go out back to back...
    burst = seen[26:36]
    assert burst[-1] - burst[0] < 0.01
    # ...and the schedule is back on time afterwards: re-anchoring would end at 2.2s.
    assert seen[40] - 40 * FRAME_SECONDS < TOLERANCE
    assert abs(total - 2.0) < TOLERANCE
