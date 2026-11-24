"""Places concurrent calls against a running API, the way a Twilio app would, and checks
how each one ends. Exits 1 if any call ends in an unexpected status.

The first call is hung up through the API after a second and the second dials a number
with no WAV; the rest cycle through the test app's normal, slow and disconnect modes.

    uv run python scripts/demo.py --calls 20
"""

import argparse
import asyncio
import itertools
import sys
import time

import httpx

from callapi import config

ENDED = ("completed", "busy", "no-answer", "failed", "canceled")
MODES = [
    ("normal", {"mode": "normal"}),
    ("slow", {"mode": "slow"}),
    ("disconnect", {"mode": "disconnect", "disconnect_after": "1"}),
]


def twiml(app_url: str, params: dict[str, str]) -> str:
    tags = "".join(f'<Parameter name="{k}" value="{v}"/>' for k, v in params.items())
    return f'<Response><Connect><Stream url="{app_url}">{tags}</Stream></Connect></Response>'


async def place(
    api: httpx.AsyncClient, app_url: str, label: str, to: str, params: dict[str, str]
) -> tuple[str, str]:
    r = await api.post(
        "Calls.json", data={"To": to, "From": "+15550000", "Twiml": twiml(app_url, params)}
    )
    r.raise_for_status()
    return label, r.json()["sid"]


async def wait_until_ended(api: httpx.AsyncClient, sid: str) -> dict[str, str]:
    # Polling the API, as a Twilio client without a StatusCallback would.
    while (call := (await api.get(f"Calls/{sid}.json")).json())["status"] not in ENDED:  # noqa: ASYNC110
        await asyncio.sleep(0.5)
    return dict(call)


async def recording_bytes(api: httpx.AsyncClient, sid: str) -> int | None:
    recordings = (await api.get(f"Calls/{sid}/Recordings.json")).json()["recordings"]
    if not recordings:
        return None
    return len((await api.get(f"Recordings/{recordings[0]['sid']}.wav")).content)


async def main(args: argparse.Namespace) -> int:
    base = f"{args.api}/2010-04-01/Accounts/{config.ACCOUNT_SID}/"
    auth = (config.ACCOUNT_SID, config.AUTH_TOKEN)
    async with httpx.AsyncClient(base_url=base, auth=auth, timeout=30) as api:
        plan = [("hangup", args.to, MODES[0][1], "completed")]
        plan.append(("no-file", "+15559999", MODES[0][1], "failed"))
        for (label, params), _ in zip(itertools.cycle(MODES), range(args.calls - 2), strict=False):
            plan.append((label, args.to, params, "completed"))

        t0 = time.monotonic()
        placed = await asyncio.gather(*(place(api, args.app_url, *p[:3]) for p in plan))
        print(f"placed {len(placed)} calls in {time.monotonic() - t0:.1f}s")

        await asyncio.sleep(1)
        hangup = await api.post(f"Calls/{placed[0][1]}.json", data={"Status": "completed"})
        hangup.raise_for_status()

        calls = await asyncio.gather(*(wait_until_ended(api, sid) for _, sid in placed))
        sizes = await asyncio.gather(*(recording_bytes(api, sid) for _, sid in placed))

    failures = 0
    header = ("call", "scenario", "status", "expected", "secs", "recording")
    print("{:<36} {:<11} {:<10} {:<10} {:>4} {:>10}".format(*header))
    for (label, sid), call, size, (*_, expected) in zip(placed, calls, sizes, plan, strict=True):
        ok = call["status"] == expected
        failures += not ok
        recording = "-" if size is None else f"{size // 1024} KB"
        print(
            f"{sid:<36} {label:<11} {call['status']:<10} {expected:<10} "
            f"{call['duration'] or '-':>4} {recording:>10}{'' if ok else '  <-- unexpected'}"
        )
    print(f"{len(plan) - failures}/{len(plan)} calls ended as expected")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run concurrent demo calls.")
    parser.add_argument("--calls", type=int, default=20)
    parser.add_argument("--api", default="http://localhost:8000")
    # As the API server sees it: compose's service name by default.
    parser.add_argument("--app-url", default="ws://testapp:8765/stream")
    parser.add_argument("--to", default="+15550001")
    sys.exit(asyncio.run(main(parser.parse_args())))
