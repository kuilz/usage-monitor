"""End-to-end and database tests."""

import pytest
import pytest_asyncio

from usage_monitor.database import init_db, save_request


@pytest_asyncio.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "e2e_test.db")
    conn = await init_db(db_path)
    yield conn
    await conn.close()


@pytest.mark.asyncio
class TestDatabase:
    async def test_schema_created(self, db):
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='requests'") as cur:
            row = await cur.fetchone()
            assert row is not None

    async def test_save_and_retrieve_request(self, db):
        await save_request(
            db,
            message_id="msg_001",
            model="claude-sonnet-4-20250514",
            is_streaming=True,
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=10,
            cache_read_input_tokens=20,
            stop_reason="end_turn",
        )
        async with db.execute("SELECT * FROM requests WHERE message_id = 'msg_001'") as cur:
            row = await cur.fetchone()
            assert row["message_id"] == "msg_001"
            assert row["model"] == "claude-sonnet-4-20250514"
            assert row["is_streaming"] == 1
            assert row["input_tokens"] == 100
            assert row["output_tokens"] == 50
            assert row["cache_creation_input_tokens"] == 10
            assert row["cache_read_input_tokens"] == 20
            assert row["stop_reason"] == "end_turn"

    async def test_save_request_with_none_values(self, db):
        await save_request(
            db,
            message_id=None,
            model="claude-sonnet-4-20250514",
            is_streaming=False,
            input_tokens=50,
            output_tokens=25,
        )
        async with db.execute("SELECT * FROM requests WHERE message_id IS NULL") as cur:
            row = await cur.fetchone()
            assert row is not None
            assert row["is_streaming"] == 0
            assert row["cache_creation_input_tokens"] == 0
            assert row["cache_read_input_tokens"] == 0
            assert row["stop_reason"] is None

    async def test_db_path_created(self, tmp_path):
        db_path = str(tmp_path / "subdir" / "deep" / "test.db")
        conn = await init_db(db_path)
        await conn.close()
        import os
        assert os.path.exists(db_path)
