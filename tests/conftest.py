import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
from websockets.asyncio.server import ServerConnection, serve

# Tests get their own database so they never wipe calls made with `make run` / compose.
ADMIN_URL = "postgresql://callapi:callapi@localhost:5432/callapi"
os.environ.setdefault("DATABASE_URL", "postgresql://callapi:callapi@localhost:5432/callapi_test")
# The API tests' app runs no calls, so a created call stays `queued`; runner tests start
# their own Supervisor.
os.environ.setdefault("MAX_CALLS", "0")

from fastapi.testclient import TestClient  # noqa: E402 - must follow the env setup

from callapi import config, db  # noqa: E402
from callapi.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def test_database() -> None:
    # Recreated every run, so schema.sql edits apply without migrations.
    name = sql.Identifier(str(psycopg.conninfo.conninfo_to_dict(config.DATABASE_URL)["dbname"]))
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(name))
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(db.SCHEMA.read_text())


@pytest.fixture
def clean_db() -> None:
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute("TRUNCATE calls, servers")


@pytest.fixture
def client(clean_db: None) -> Iterator[TestClient]:
    with TestClient(app) as c:
        c.auth = (config.ACCOUNT_SID, config.AUTH_TOKEN)
        yield c


@pytest.fixture
async def pool(clean_db: None) -> AsyncIterator[AsyncConnectionPool]:
    async with AsyncConnectionPool(config.DATABASE_URL, open=False) as p:
        yield p


@dataclass
class FakeApp:
    """A Media Streams app that records every message with its arrival time."""

    url: str
    received: list[tuple[float, dict[str, Any]]] = field(default_factory=list)
    # Set by a test to answer messages, e.g. echo audio or send marks.
    on_message: Callable[[ServerConnection, dict[str, Any]], Awaitable[None]] | None = None

    def first(self, event: str) -> tuple[float, dict[str, Any]]:
        return next((t, m) for t, m in self.received if m["event"] == event)

    def events(self) -> list[str]:
        return [m["event"] for _, m in self.received]


@pytest.fixture
async def fake_app() -> AsyncIterator[FakeApp]:
    app = FakeApp("")

    async def handler(ws: ServerConnection) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            app.received.append((time.monotonic(), msg))
            if app.on_message:
                await app.on_message(ws, msg)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        app.url = f"ws://127.0.0.1:{port}/stream"
        yield app
