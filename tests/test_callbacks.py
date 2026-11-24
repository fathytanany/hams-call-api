"""Status callbacks (PLAN slice 6, TWILIO_CONTRACT §6, ADR-009)."""

import asyncio
import base64
import hashlib
import hmac
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from conftest import TO, FakeApp, insert, wait_for_status, write_wav
from psycopg_pool import AsyncConnectionPool

from callapi import callbacks, config
from callapi.runner import Supervisor

ALL_EVENTS = ("initiated", "ringing", "answered", "completed")


def twilio_signature(token: str, url: str, params: dict[str, str]) -> str:
    """TWILIO_CONTRACT §6, written out independently of callapi.callbacks."""
    payload = url + "".join(name + params[name] for name in sorted(params))
    return base64.b64encode(
        hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()


def test_signature_matches_twilios_published_example() -> None:
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+14158675309",
        "Digits": "1234",
        "From": "+14158675309",
        "To": "+18005551212",
    }
    expected = "RSOYDt4T1cUTdK1PDd93/VVr8B8="
    assert twilio_signature("12345", url, params) == expected
    assert callbacks.signature("12345", url, params) == expected


@dataclass
class Received:
    method: str
    params: dict[str, str]
    signed: bool  # X-Twilio-Signature matched our own HMAC


@dataclass
class Hook:
    """A StatusCallback receiver that checks each request's signature."""

    @property
    def url(self) -> str:
        return f"{self.base}/hook"

    base: str = ""  # scheme and host, which the request line leaves out
    received: list[Received] = field(default_factory=list)
    delay: float = 0.0

    async def wait_for(self, n: int) -> list[Received]:
        async with asyncio.timeout(10):
            while len(self.received) < n:  # noqa: ASYNC110 - filled by the server thread
                await asyncio.sleep(0.05)
        return self.received


@pytest.fixture
def hook() -> Iterator[Hook]:
    hook = Hook()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            url = hook.base + self.path
            if self.command == "POST":
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                params = dict(parse_qsl(body))
                signed_params = params
            else:  # GET signs the URL alone, query string included
                params, signed_params = dict(parse_qsl(urlsplit(self.path).query)), {}
            expected = twilio_signature(config.AUTH_TOKEN, url, signed_params)
            signed = self.headers["X-Twilio-Signature"] == expected
            hook.received.append(Received(self.command, params, signed))
            time.sleep(hook.delay)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    hook.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield hook
    server.shutdown()
    server.server_close()


async def test_all_four_events_arrive_in_order_numbered_and_signed(
    pool: AsyncConnectionPool, fake_app: FakeApp, supervisor: Supervisor, tmp_path: Path, hook: Hook
) -> None:
    write_wav(tmp_path / f"{TO}.wav", 1.0)
    call = await insert(pool, fake_app.url, status_callback=hook.url, events=ALL_EVENTS)
    supervisor.poke()

    got = await hook.wait_for(4)
    assert [r.params["CallStatus"] for r in got] == [
        "initiated",
        "ringing",
        "in-progress",
        "completed",
    ]
    assert [r.params["SequenceNumber"] for r in got] == ["0", "1", "2", "3"]
    assert all(r.signed and r.method == "POST" for r in got)
    first = got[0].params
    assert first["CallSid"] == call.sid
    assert first["AccountSid"] == config.ACCOUNT_SID
    assert first["From"] == first["Caller"] == "+15550000"
    assert first["To"] == first["Called"] == TO
    assert first["Direction"] == "outbound-api"
    assert first["ApiVersion"] == "2010-04-01"
    assert first["CallbackSource"] == "call-progress-events"
    assert first["Timestamp"].endswith("+0000")
    assert "CallDuration" not in first


async def test_default_sends_only_completed_with_duration_and_recording(
    pool: AsyncConnectionPool, fake_app: FakeApp, supervisor: Supervisor, tmp_path: Path, hook: Hook
) -> None:
    write_wav(tmp_path / f"{TO}.wav", 2.0)
    # GET with a query string of its own: Twilio signs the full URL it requests.
    url = f"{hook.url}?tenant=a"
    call = await insert(pool, fake_app.url, status_callback=url, method="GET")
    supervisor.poke()

    [done] = await hook.wait_for(1)
    await asyncio.sleep(0.2)
    assert len(hook.received) == 1
    assert done.method == "GET" and done.signed
    recording_sid = f"RE{call.sid[2:]}"
    assert done.params | {"Timestamp": ""} == {
        "tenant": "a",
        "CallSid": call.sid,
        "AccountSid": config.ACCOUNT_SID,
        "From": "+15550000",
        "To": TO,
        "Caller": "+15550000",
        "Called": TO,
        "CallStatus": "completed",
        "ApiVersion": "2010-04-01",
        "Direction": "outbound-api",
        "CallbackSource": "call-progress-events",
        "SequenceNumber": "0",
        "Timestamp": "",
        "CallDuration": "2",
        "Duration": "1",
        "SipResponseCode": "200",
        "RecordingSid": recording_sid,
        "RecordingUrl": (
            f"{config.PUBLIC_URL}/2010-04-01/Accounts/{config.ACCOUNT_SID}"
            f"/Recordings/{recording_sid}"
        ),
        "RecordingDuration": "2",
    }


async def test_busy_ends_with_its_own_status_and_sip_code(
    pool: AsyncConnectionPool, supervisor: Supervisor, tmp_path: Path, hook: Hook
) -> None:
    (tmp_path / f"{TO}.busy").touch()
    await insert(pool, "ws://unused/s", status_callback=hook.url, events=ALL_EVENTS)
    supervisor.poke()

    got = await hook.wait_for(3)
    assert [(r.params["CallStatus"], r.params["SequenceNumber"]) for r in got] == [
        ("initiated", "0"),
        ("ringing", "1"),
        ("busy", "2"),
    ]
    busy = got[-1].params
    assert busy["CallDuration"] == "0" and busy["SipResponseCode"] == "486"
    assert "Duration" not in busy and "RecordingSid" not in busy


async def test_slow_receiver_does_not_delay_the_call(
    pool: AsyncConnectionPool, fake_app: FakeApp, supervisor: Supervisor, tmp_path: Path, hook: Hook
) -> None:
    hook.delay = 2.0
    write_wav(tmp_path / f"{TO}.wav", 1.0)
    t0 = time.monotonic()
    call = await insert(pool, fake_app.url, status_callback=hook.url, events=ALL_EVENTS)
    supervisor.poke()

    await wait_for_status(pool, call.sid, "completed")
    assert time.monotonic() - t0 < 1.8
    assert len(hook.received) == 1  # still stuck on `initiated`


async def test_connect_failure_is_retried_once() -> None:
    attempts: list[httpx.Request] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        with pytest.raises(httpx.ConnectError):
            await callbacks.deliver(client, "POST", "http://hook.test/cb", {"CallSid": "CA1"})
    assert len(attempts) == 2


async def test_error_response_is_not_retried() -> None:
    attempts: list[httpx.Request] = []

    def fail(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        await callbacks.deliver(client, "POST", "http://hook.test/cb", {"CallSid": "CA1"})
    assert len(attempts) == 1
