from __future__ import annotations

from unittest.mock import patch

import pytest

from ghosthands.integrations.database import Database


@pytest.mark.asyncio
async def test_connect_disables_statement_cache_for_pgbouncer_dsn():
    captured: dict[str, object] = {}

    async def fake_create_pool(dsn: str, **kwargs):
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return object()

    dsn = (
        "postgresql://user:pass@aws-1-us-east-1.pooler.supabase.com:6543/postgres"
        "?pgbouncer=true&connection_limit=5"
    )

    with patch("ghosthands.integrations.database.asyncpg.create_pool", side_effect=fake_create_pool):
        db = Database(dsn)
        await db.connect()

    assert captured["dsn"] == dsn
    assert captured["kwargs"]["statement_cache_size"] == 0


@pytest.mark.asyncio
async def test_connect_keeps_default_statement_cache_for_regular_dsn():
    captured: dict[str, object] = {}

    async def fake_create_pool(dsn: str, **kwargs):
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return object()

    dsn = "postgresql://user:pass@localhost:5432/postgres"

    with patch("ghosthands.integrations.database.asyncpg.create_pool", side_effect=fake_create_pool):
        db = Database(dsn)
        await db.connect()

    assert captured["dsn"] == dsn
    assert "statement_cache_size" not in captured["kwargs"]
