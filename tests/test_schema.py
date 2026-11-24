"""Servers starting together on an empty database (a first `docker compose up`)."""

import asyncio

import psycopg
from conftest import ADMIN_URL
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from callapi import config, db


async def test_servers_starting_at_once_on_a_fresh_database_all_start() -> None:
    url = psycopg.conninfo.make_conninfo(config.DATABASE_URL, dbname="callapi_schema_race")
    name = sql.Identifier("callapi_schema_race")
    for _ in range(5):  # the race is timing-dependent, so give it several chances
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
            conn.execute(sql.SQL("CREATE DATABASE {}").format(name))
        async with AsyncConnectionPool(url, min_size=4, open=False) as pool:
            await asyncio.gather(*(db.apply_schema(pool) for _ in range(4)))
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(name))
