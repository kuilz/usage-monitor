"""Shared fixtures for usage-monitor tests."""

import json
import asyncio
from typing import AsyncIterator

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from usage_monitor.database import init_db
from usage_monitor.main import app


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ── SSE event helpers ──────────────────────────────────────────────────


def sse_event(event: str, data: dict) -> str:
    """Build a single SSE event string."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def make_message_start(
    *,
    message_id: str = "msg_test",
    model: str = "claude-sonnet-4-20250514",
    input_tokens: int = 100,
    output_tokens: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> str:
    return sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                },
            },
        },
    )


def make_content_block_start(index: int = 0, block: dict | None = None) -> str:
    if block is None:
        block = {"type": "text", "text": ""}
    return sse_event(
        "content_block_start",
        {"type": "content_block_start", "index": index, "content_block": block},
    )


def make_text_delta(text: str, index: int = 0) -> str:
    return sse_event(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    )


def make_thinking_delta(text: str, index: int = 0) -> str:
    return sse_event(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "thinking_delta", "thinking": text},
        },
    )


def make_input_json_delta(partial: str, index: int = 0) -> str:
    return sse_event(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": partial},
        },
    )


def make_content_block_stop(index: int = 0) -> str:
    return sse_event(
        "content_block_stop",
        {"type": "content_block_stop", "index": index},
    )


def make_message_delta(
    *,
    stop_reason: str = "end_turn",
    stop_sequence: str | None = None,
    output_tokens: int = 50,
) -> str:
    return sse_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": stop_sequence},
            "usage": {"output_tokens": output_tokens},
        },
    )


def make_ping() -> str:
    return sse_event("ping", {"type": "ping"})


def make_complete_text_stream(
    text: str = "Hello world",
    *,
    message_id: str = "msg_test",
    model: str = "claude-sonnet-4-20250514",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> str:
    """Build a complete SSE stream for a simple text response."""
    parts = [
        make_message_start(
            message_id=message_id,
            model=model,
            input_tokens=input_tokens,
        ),
        make_content_block_start(0, {"type": "text", "text": ""}),
    ]
    # Split text into chunks to simulate real streaming
    chunk_size = max(1, len(text) // 3)
    for i, chunk_start in enumerate(range(0, len(text), chunk_size)):
        parts.append(make_text_delta(text[chunk_start : chunk_start + chunk_size]))
    parts += [
        make_content_block_stop(0),
        make_message_delta(output_tokens=output_tokens, stop_reason="end_turn"),
    ]
    return "".join(parts)


# ── App + DB fixtures ──────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db(tmp_path):
    """Create a fresh in-memory DB for each test."""
    db_path = str(tmp_path / "test.db")
    conn = await init_db(db_path)
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def client(db):
    """Async test client with a real DB attached."""
    app.state.db = db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
