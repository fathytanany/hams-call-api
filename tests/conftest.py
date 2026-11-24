import os
from collections.abc import Iterator

import psycopg
import pytest

# Tests get their own database so they never wipe calls made with `make run` / compose.
ADMIN_URL = "postgresql://callapi:callapi@localhost:5432/callapi"
os.environ.setdefault("DATABASE_URL", "postgresql://callapi:callapi@localhost:5432/callapi_test")

from fastapi.testclient import TestClient  # noqa: E402 - must follow the env setup

from callapi import config  # noqa: E402
from callapi.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def test_database() -> None:
    name = str(psycopg.conninfo.conninfo_to_dict(config.DATABASE_URL)["dbname"])
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:  # runs the lifespan, which applies the schema
        with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
            conn.execute("TRUNCATE calls, servers")
        c.auth = (config.ACCOUNT_SID, config.AUTH_TOKEN)
        yield c
