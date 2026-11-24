"""Status callbacks: TWILIO_CONTRACT §6, ADR-009."""

import asyncio
import base64
import hashlib
import hmac
import logging
import math
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path

import httpx

from callapi import config, db
from callapi.audio import wav_seconds

log = logging.getLogger(__name__)

# Twilio's webhook defaults: ct=5000, rt=15000, tt=15000, rc=1, rp=ct.
TIMEOUT = httpx.Timeout(15.0, connect=5.0)
TOTAL_SECONDS = 15.0
RETRIES = 1
SIP_CODES = {
    "busy": "486",
    "no-answer": "487",
    "canceled": "487",
    "failed": "404",
}  # ASSUMPTIONS 24


def signature(token: str, url: str, params: dict[str, str]) -> str:
    """X-Twilio-Signature: HMAC-SHA1 of the URL plus each sorted name and value."""
    payload = url + "".join(name + params[name] for name in sorted(params))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


async def deliver(client: httpx.AsyncClient, method: str, url: str, params: dict[str, str]) -> None:
    """One callback. Only a failed connection is retried; any response, even a 5xx, is final."""
    if method == "GET":  # params go in the query string, and the signature covers the URL alone
        url, params = str(httpx.URL(url).copy_merge_params(params)), {}
    headers = {"X-Twilio-Signature": signature(config.AUTH_TOKEN, url, params)}
    async with asyncio.timeout(TOTAL_SECONDS):
        for attempt in range(RETRIES + 1):
            try:
                response = await client.request(method, url, data=params or None, headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if attempt == RETRIES:
                    raise
                continue
            if response.is_error:
                log.warning("callback %s answered %d", url, response.status_code)
            return


def unanswered(status: str) -> dict[str, str]:
    return {"CallDuration": "0", "SipResponseCode": SIP_CODES[status]}


def completed(call_sid: str, duration: int, recording: Path) -> dict[str, str]:
    params = {
        "CallDuration": str(duration),
        "Duration": str(math.ceil(duration / 60)),  # billed minutes (ASSUMPTIONS 29)
        "SipResponseCode": "200",
    }
    if recording.exists():  # none if the stream never opened
        recording_sid = f"RE{call_sid[2:]}"  # ADR-008
        params["RecordingSid"] = recording_sid
        params["RecordingUrl"] = (
            f"{config.PUBLIC_URL}/2010-04-01/Accounts/{config.ACCOUNT_SID}"
            f"/Recordings/{recording_sid}"
        )
        params["RecordingDuration"] = str(round(wav_seconds(recording)))
    return params


class StatusCallbacks:
    """One call's callbacks. A single task sends them in order, so `ringing` never overtakes
    `initiated`, and the call never waits on the receiver (ADR-009)."""

    def __init__(self, call: db.Call, client: httpx.AsyncClient) -> None:
        self._call, self._client = call, client
        self._seq = 0  # SequenceNumber: callbacks fired for this call, from 0
        self._queue: asyncio.Queue[dict[str, str] | None] = asyncio.Queue()
        self.task = asyncio.create_task(self._send(), name=f"callbacks-{call.sid}")

    def fire(self, event: str, status: str, extra: dict[str, str] | None = None) -> None:
        c = self._call
        if c.status_callback is None or event not in c.status_callback_events:
            return
        params = {
            "CallSid": c.sid,
            "AccountSid": config.ACCOUNT_SID,
            "From": c.from_number,
            "To": c.to_number,
            "Caller": c.from_number,
            "Called": c.to_number,
            "CallStatus": status,
            "ApiVersion": "2010-04-01",
            "Direction": "outbound-api",
            "CallbackSource": "call-progress-events",
            "SequenceNumber": str(self._seq),
            "Timestamp": format_datetime(datetime.now(UTC)),
        }
        self._queue.put_nowait(params | (extra or {}))
        self._seq += 1

    def close(self) -> None:
        """Nothing more will fire; the task ends once the queue is sent."""
        self._queue.put_nowait(None)

    async def _send(self) -> None:
        while (params := await self._queue.get()) is not None:
            url = self._call.status_callback
            assert url is not None  # fire() queues nothing without one
            try:
                await deliver(self._client, self._call.status_callback_method, url, params)
            except (httpx.HTTPError, TimeoutError) as e:
                log.warning(
                    "call %s: %s callback to %s failed: %r",
                    self._call.sid,
                    params["CallStatus"],
                    url,
                    e,
                )
