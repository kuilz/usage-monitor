import aiosqlite
import logging
from pathlib import Path


logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT,
    model           TEXT NOT NULL,
    is_streaming    INTEGER NOT NULL DEFAULT 0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER DEFAULT 0,
    cache_read_input_tokens     INTEGER DEFAULT 0,
    stop_reason     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'utc'))
);

CREATE INDEX IF NOT EXISTS idx_requests_created_at ON requests(created_at);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model);
"""


async def init_db(db_path: str) -> aiosqlite.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()
    return db


async def close_db(db: aiosqlite.Connection):
    await db.close()


async def save_request(
    db: aiosqlite.Connection,
    *,
    message_id: str | None,
    model: str,
    is_streaming: bool,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    stop_reason: str | None = None,
):
    try:
        await db.execute(
            """INSERT INTO requests
               (message_id, model, is_streaming, input_tokens, output_tokens,
                cache_creation_input_tokens, cache_read_input_tokens, stop_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message_id,
                model,
                int(is_streaming),
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
                stop_reason,
            ),
        )
        await db.commit()
    except Exception:
        # Best-effort: never block the proxy response
        logger.exception("Failed to save request usage record")
