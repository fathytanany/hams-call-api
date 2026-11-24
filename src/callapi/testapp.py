"""A Media Streams app for demos and tests: saves what it hears to out/{CallSid}.wav and
echoes it back, with a mark each second.

Each call picks its behavior with <Parameter>s, which arrive in start.customParameters:
  mode=normal       echo every frame
  mode=slow         the same, but read at half speed
  mode=disconnect   close the socket after `disconnect_after` seconds of audio (default 1)

    python -m callapi.testapp    # listens on :8765
"""

import asyncio
import base64
import json
import logging
import os
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from callapi.audio import RecordingWriter

log = logging.getLogger(__name__)

OUT_DIR = Path(os.environ.get("OUT_DIR", "out"))
PORT = int(os.environ.get("PORT", "8765"))
FRAMES_PER_MARK = 50  # one second


async def handle(ws: ServerConnection) -> None:
    heard: RecordingWriter | None = None
    call_sid, mode, disconnect_after = "?", "normal", 1.0
    frames = marks = 0
    try:
        async for raw in ws:
            msg = json.loads(raw)
            match msg["event"]:
                case "start":
                    start = msg["start"]
                    call_sid, params = start["callSid"], start["customParameters"]
                    mode = params.get("mode", "normal")
                    disconnect_after = float(params.get("disconnect_after", "1"))
                    heard = RecordingWriter(OUT_DIR / f"{call_sid}.wav")
                case "media" if heard is not None:
                    payload = msg["media"]["payload"]
                    heard.write(base64.b64decode(payload))
                    frames += 1
                    if mode == "disconnect" and frames * 0.02 >= disconnect_after:
                        break  # returning closes the socket
                    if mode == "slow":
                        await asyncio.sleep(0.04)
                    reply = {"event": "media", "streamSid": msg["streamSid"]}
                    await ws.send(json.dumps(reply | {"media": {"payload": payload}}))
                    if frames % FRAMES_PER_MARK == 0:
                        mark = {"name": f"second-{frames // FRAMES_PER_MARK}"}
                        await ws.send(json.dumps(reply | {"event": "mark", "mark": mark}))
                case "mark":
                    marks += 1
                case "stop":
                    break
    except ConnectionClosed:
        pass
    finally:
        if heard is not None:
            heard.close()
        log.info("call %s (%s): heard %d frames, %d marks came back", call_sid, mode, frames, marks)


async def main() -> None:
    async with serve(handle, "0.0.0.0", PORT) as server:  # all interfaces: it runs in compose
        log.info("test app listening on :%d", PORT)
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.run(main())
