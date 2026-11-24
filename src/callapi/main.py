import asyncio
import re
import secrets
import socket
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Annotated, Any
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from psycopg_pool import AsyncConnectionPool
from starlette.exceptions import HTTPException

from callapi import config, db, twiml
from callapi.runner import Supervisor

API = "/2010-04-01"
ACCOUNT = f"{API}/Accounts/{{account_sid}}"
# `To` names the callee file (media/{To}.wav), so this is also the path-traversal guard.
PHONE = re.compile(r"\+?\d{1,15}")
EVENTS = ("initiated", "ringing", "answered", "completed")
MAX_TIMEOUT = 600
MAX_PAGE_SIZE = 1000


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with AsyncConnectionPool(config.DATABASE_URL, open=False) as pool:
        await db.apply_schema(pool)
        # A random suffix: a restarted container keeps its hostname and pid but must not
        # inherit the dead instance's calls.
        server_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        supervisor = Supervisor(
            pool, server_id=server_id, max_calls=config.MAX_CALLS, media_dir=config.MEDIA_DIR
        )
        app.state.pool, app.state.supervisor = pool, supervisor
        task = asyncio.create_task(supervisor.run())
        try:
            yield
        finally:
            # Calls still running are left for the reaper (ADR-004).
            for t in (task, *supervisor.calls):
                t.cancel()
            await asyncio.gather(task, *supervisor.calls, return_exceptions=True)


app = FastAPI(title="Twilio-compatible Call API", lifespan=lifespan)


class TwilioError(Exception):
    def __init__(self, status: int, message: str, code: int | None = None) -> None:
        self.status, self.message, self.code = status, message, code


def error_body(status: int, message: str, code: int | None) -> dict[str, Any]:
    if code is None:
        return {"message": message, "status": status}
    more_info = f"https://www.twilio.com/docs/errors/{code}"
    return {"code": code, "message": message, "more_info": more_info, "status": status}


@app.exception_handler(TwilioError)
async def twilio_error(request: Request, exc: TwilioError) -> JSONResponse:
    return JSONResponse(error_body(exc.status, exc.message, exc.code), exc.status)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    # Starlette's own 401/404/405s (bad auth header, unknown route, wrong method) in Twilio's shape.
    code = {401: 20003, 404: 20404}.get(exc.status_code)
    message = {
        401: "Authenticate",
        404: f"The requested resource {request.url.path} was not found",
    }.get(exc.status_code, str(exc.detail))
    return JSONResponse(error_body(exc.status_code, message, code), exc.status_code, exc.headers)


basic = HTTPBasic(auto_error=False)


def authenticate(
    account_sid: str, creds: Annotated[HTTPBasicCredentials | None, Depends(basic)]
) -> None:
    ok = (
        creds is not None
        and secrets.compare_digest(creds.username.encode(), config.ACCOUNT_SID.encode())
        and secrets.compare_digest(creds.password.encode(), config.AUTH_TOKEN.encode())
    )
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="Twilio API"'})
    # Single account: any other AccountSid is "not found" so we don't reveal what exists.
    if account_sid != config.ACCOUNT_SID:
        raise HTTPException(404)


Auth = Depends(authenticate)


def pool(request: Request) -> AsyncConnectionPool:
    p: AsyncConnectionPool = request.app.state.pool
    return p


Pool = Annotated[AsyncConnectionPool, Depends(pool)]


def rfc2822(dt: datetime | None) -> str | None:
    return None if dt is None else format_datetime(dt.astimezone(UTC))


def str_or_none(n: int | None) -> str | None:
    return None if n is None else str(n)


def call_json(c: db.Call) -> dict[str, Any]:
    uri = f"{API}/Accounts/{config.ACCOUNT_SID}/Calls/{c.sid}"
    subresources = {
        "notifications": "Notifications",
        "recordings": "Recordings",
        "payments": "Payments",
        "events": "Events",
        "siprec": "Siprec",
        "streams": "Streams",
        "transcriptions": "Transcriptions",
        "user_defined_message_subscriptions": "UserDefinedMessageSubscriptions",
        "user_defined_messages": "UserDefinedMessages",
    }
    return {
        "sid": c.sid,
        "account_sid": config.ACCOUNT_SID,
        "parent_call_sid": None,
        "phone_number_sid": None,
        "group_sid": None,
        "trunk_sid": None,
        "forwarded_from": None,
        "caller_name": None,
        "answered_by": None,
        "stir_verstat": None,
        "price": None,
        "to": c.to_number,
        "to_formatted": c.to_number,
        "from": c.from_number,
        "from_formatted": c.from_number,
        "status": c.status,
        "date_created": rfc2822(c.date_created),
        "date_updated": rfc2822(c.date_updated),
        "start_time": rfc2822(c.start_time),
        "end_time": rfc2822(c.end_time),
        "duration": str_or_none(c.duration),
        "queue_time": str_or_none(c.queue_time),
        "price_unit": "USD",
        "direction": "outbound-api",
        "api_version": "2010-04-01",
        "uri": f"{uri}.json",
        "subresource_uris": {k: f"{uri}/{v}.json" for k, v in subresources.items()},
    }


def bad_request(message: str, code: int | None = None) -> TwilioError:
    return TwilioError(400, message, code)


def parse_int(name: str, raw: str, minimum: int) -> int:
    try:
        n = int(raw)
    except ValueError:
        raise bad_request(f"{name} must be an integer, got {raw!r}") from None
    if n < minimum:
        raise bad_request(f"{name} must be at least {minimum}, got {n}")
    return n


@app.post(f"{ACCOUNT}/Calls.json", status_code=201, dependencies=[Auth])
async def create_call(request: Request, pool: Pool) -> dict[str, Any]:
    # Parsed by hand: StatusCallbackEvent may repeat, and this avoids python-multipart.
    try:
        form = parse_qs((await request.body()).decode(), keep_blank_values=True)
    except UnicodeDecodeError:
        raise bad_request("Request body must be UTF-8 form data") from None

    def one(name: str) -> str:
        return form.get(name, [""])[-1].strip()

    to, from_ = one("To"), one("From")
    if not to:
        raise bad_request("No 'To' number is specified", 21201)
    if not from_:
        raise bad_request("Caller phone number is required", 21213)
    if not PHONE.fullmatch(to):
        raise bad_request(f"The 'To' number {to} is not a valid phone number.", 21211)
    if not PHONE.fullmatch(from_):
        raise bad_request(f"The 'From' number {from_} is not a valid phone number.", 21212)

    for unsupported in ("Url", "ApplicationSid"):
        if one(unsupported):
            raise bad_request(f"{unsupported} is not supported; pass the instructions as Twiml")
    if not one("Twiml"):
        raise bad_request("Twiml is required", 21205)
    try:
        stream = twiml.parse(one("Twiml"))
    except twiml.TwimlError as e:
        raise bad_request(str(e)) from None

    callback = one("StatusCallback") or None
    if callback is not None:
        parts = urlsplit(callback)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise bad_request(f"StatusCallback must be an absolute http(s) URL, got {callback!r}")
    method = (one("StatusCallbackMethod") or "POST").upper()
    if method not in ("GET", "POST"):
        raise bad_request(f"StatusCallbackMethod must be GET or POST, got {method!r}")
    events = [e for raw in form.get("StatusCallbackEvent", []) for e in raw.split()]
    if unknown := sorted(set(events) - set(EVENTS)):
        raise bad_request(f"Unknown StatusCallbackEvent value(s): {', '.join(unknown)}")
    timeout = min(parse_int("Timeout", one("Timeout") or "60", 1), MAX_TIMEOUT)

    call = await db.insert_call(
        pool,
        sid=f"CA{uuid.uuid4().hex}",
        to=to,
        from_=from_,
        stream_url=stream.url,
        stream_params=stream.parameters,
        status_callback=callback,
        status_callback_method=method,
        # Keep Twilio's event order whatever order the client sent them in.
        status_callback_events=[e for e in EVENTS if e in events] or ["completed"],
        ring_timeout=timeout,
    )
    request.app.state.supervisor.poke()
    return call_json(call)


@app.get(f"{ACCOUNT}/Calls/{{call_sid}}.json", dependencies=[Auth])
async def fetch_call(call_sid: str, pool: Pool) -> dict[str, Any]:
    call = await db.get_call(pool, call_sid)
    if call is None:
        raise HTTPException(404)
    return call_json(call)


@app.get(f"{ACCOUNT}/Calls.json", dependencies=[Auth])
async def list_calls(request: Request, pool: Pool) -> dict[str, Any]:
    q = request.query_params
    filters = {k: q[k] for k in ("To", "From", "Status") if q.get(k)}
    size = min(parse_int("PageSize", q.get("PageSize") or "50", 1), MAX_PAGE_SIZE)
    page = parse_int("Page", q.get("Page") or "0", 0)
    # One extra row tells us whether a next page exists without a COUNT.
    rows = await db.list_calls(
        pool,
        to=filters.get("To"),
        from_=filters.get("From"),
        status=filters.get("Status"),
        limit=size + 1,
        offset=page * size,
    )
    calls = rows[:size]

    def page_uri(n: int) -> str:
        # Filters ride along so following next_page_uri stays inside the filtered set.
        return f"{request.url.path}?{urlencode({**filters, 'PageSize': size, 'Page': n})}"

    start = page * size
    return {
        "calls": [call_json(c) for c in calls],
        "page": page,
        "page_size": size,
        "start": start,
        "end": start + max(len(calls) - 1, 0),
        "uri": page_uri(page),
        "first_page_uri": page_uri(0),
        "next_page_uri": page_uri(page + 1) if len(rows) > size else None,
        "previous_page_uri": page_uri(page - 1) if page > 0 else None,
    }
