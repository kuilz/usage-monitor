"""Unit tests for proxy.py — focusing on streaming / non-streaming conversion logic."""

import json

import pytest
import respx
from httpx import Response

from usage_monitor.config import settings
from usage_monitor.proxy import _flush_content_block

UPSTREAM_URL = f"{settings.anthropic_base_url}/v1/messages"


# ═══════════════════════════════════════════════════════════════════════
# _flush_content_block unit tests
# ═══════════════════════════════════════════════════════════════════════


class TestFlushContentBlock:
    def test_none_block_is_noop(self):
        blocks = []
        _flush_content_block(blocks, None, "text")
        assert blocks == []

    def test_text_block_appends_deltas(self):
        blocks = []
        block = {"type": "text", "text": ""}
        _flush_content_block(blocks, block, "Hello ")
        _flush_content_block(blocks, {"type": "text", "text": ""}, "World")
        assert blocks[0]["text"] == "Hello "
        assert blocks[1]["text"] == "World"

    def test_text_block_preserves_initial_text(self):
        blocks = []
        block = {"type": "text", "text": "Base"}
        _flush_content_block(blocks, block, " extra")
        assert blocks[0]["text"] == "Base extra"

    def test_thinking_block(self):
        blocks = []
        block = {"type": "thinking", "thinking": ""}
        _flush_content_block(blocks, block, "I think...")
        assert blocks[0]["thinking"] == "I think..."

    def test_tool_use_block_valid_json(self):
        blocks = []
        block = {"type": "tool_use", "id": "tu_1", "name": "bash"}
        _flush_content_block(blocks, block, '{"cmd": "ls"}')
        assert blocks[0]["input"] == {"cmd": "ls"}

    def test_tool_use_block_invalid_json(self):
        blocks = []
        block = {"type": "tool_use", "id": "tu_1", "name": "bash"}
        _flush_content_block(blocks, block, "not json")
        assert blocks[0]["input"] == {}

    def test_tool_use_block_empty_delta(self):
        blocks = []
        block = {"type": "tool_use", "id": "tu_1", "name": "bash"}
        _flush_content_block(blocks, block, "")
        assert blocks[0]["input"] == {}

    def test_unknown_block_type_still_appended(self):
        blocks = []
        block = {"type": "custom_type", "data": "x"}
        _flush_content_block(blocks, block, "ignored")
        assert len(blocks) == 1


# ═══════════════════════════════════════════════════════════════════════
# _build_upstream_headers
# ═══════════════════════════════════════════════════════════════════════


class TestBuildUpstreamHeaders:
    def test_strips_auth_and_injects_api_key(self):
        from unittest.mock import MagicMock

        from usage_monitor.proxy import _build_upstream_headers

        req = MagicMock()
        req.headers.items.return_value = [
            ("host", "localhost:8080"),
            ("content-length", "100"),
            ("authorization", "Bearer old"),
            ("x-api-key", "old-key"),
            ("content-type", "application/json"),
            ("anthropic-version", "2023-06-01"),
        ]
        headers = _build_upstream_headers(req)
        assert "host" not in headers
        assert "content-length" not in headers
        # Authorization is set to Bearer + api_key (overridden, not stripped)
        assert headers["authorization"] == f"Bearer {settings.anthropic_api_key}"
        assert headers["x-api-key"] == settings.anthropic_api_key
        assert headers["anthropic-version"] == "2023-06-01"


# ═══════════════════════════════════════════════════════════════════════
# Non-streaming → internally streaming → aggregate back to JSON
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestNonStreamingProxy:
    """Client sends stream=false. Proxy forces stream=true upstream, then
    aggregates SSE back into a single JSON response."""

    @respx.mock
    async def test_simple_text_response(self, client, db):
        from tests.conftest import make_complete_text_stream

        upstream_sse = make_complete_text_stream(
            "Hello world",
            message_id="msg_001",
            input_tokens=100,
            output_tokens=50,
        )

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=upstream_sse.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "msg_001"
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert body["stop_reason"] == "end_turn"
        assert len(body["content"]) == 1
        assert body["content"][0]["type"] == "text"
        assert body["content"][0]["text"] == "Hello world"
        assert body["usage"]["input_tokens"] == 100
        assert body["usage"]["output_tokens"] == 50

    @respx.mock
    async def test_forced_stream_in_upstream_request(self, client, db):
        """Verify the proxy always sends stream=true to upstream even when client sent false."""
        from tests.conftest import make_complete_text_stream

        upstream_sse = make_complete_text_stream("ok")

        route = respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=upstream_sse.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        await client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
        )

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["stream"] is True, (
            "Proxy should force stream=true for upstream request"
        )

    @respx.mock
    async def test_multiple_content_blocks(self, client, db):
        from tests.conftest import (
            make_content_block_start,
            make_content_block_stop,
            make_message_delta,
            make_message_start,
            make_text_delta,
        )

        upstream_sse = (
            make_message_start(message_id="msg_multi", input_tokens=200)
            + make_content_block_start(0, {"type": "text", "text": ""})
            + make_text_delta("First")
            + make_content_block_stop(0)
            + make_content_block_start(1, {"type": "text", "text": ""})
            + make_text_delta("Second")
            + make_content_block_stop(1)
            + make_message_delta(output_tokens=30)
        )

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=upstream_sse.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        resp = await client.post(
            "/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "messages": []},
        )
        body = resp.json()
        assert len(body["content"]) == 2
        assert body["content"][0]["text"] == "First"
        assert body["content"][1]["text"] == "Second"

    @respx.mock
    async def test_thinking_block(self, client, db):
        from tests.conftest import (
            make_content_block_start,
            make_content_block_stop,
            make_message_delta,
            make_message_start,
            make_text_delta,
            make_thinking_delta,
        )

        upstream_sse = (
            make_message_start(message_id="msg_think", input_tokens=50)
            + make_content_block_start(0, {"type": "thinking", "thinking": ""})
            + make_thinking_delta("Let me think...")
            + make_content_block_stop(0)
            + make_content_block_start(1, {"type": "text", "text": ""})
            + make_text_delta("Answer")
            + make_content_block_stop(1)
            + make_message_delta(output_tokens=20)
        )

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=upstream_sse.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        resp = await client.post(
            "/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "messages": []},
        )
        body = resp.json()
        assert len(body["content"]) == 2
        assert body["content"][0]["type"] == "thinking"
        assert body["content"][0]["thinking"] == "Let me think..."
        assert body["content"][1]["text"] == "Answer"

    @respx.mock
    async def test_tool_use_block(self, client, db):
        from tests.conftest import (
            make_content_block_start,
            make_content_block_stop,
            make_input_json_delta,
            make_message_delta,
            make_message_start,
            make_text_delta,
        )

        upstream_sse = (
            make_message_start(message_id="msg_tool", input_tokens=80)
            + make_content_block_start(0, {"type": "text", "text": ""})
            + make_text_delta("I'll run that.")
            + make_content_block_stop(0)
            + make_content_block_start(
                1, {"type": "tool_use", "id": "tu_1", "name": "bash", "input": {}}
            )
            + make_input_json_delta('{"command": "ls')
            + make_input_json_delta(' -la"}')
            + make_content_block_stop(1)
            + make_message_delta(output_tokens=60, stop_reason="tool_use")
        )

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=upstream_sse.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        resp = await client.post(
            "/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "messages": []},
        )
        body = resp.json()
        assert len(body["content"]) == 2
        assert body["content"][1]["type"] == "tool_use"
        assert body["content"][1]["name"] == "bash"
        assert body["content"][1]["input"] == {"command": "ls -la"}
        assert body["stop_reason"] == "tool_use"

    @respx.mock
    async def test_cache_tokens_tracked(self, client, db):
        from tests.conftest import (
            make_content_block_start,
            make_content_block_stop,
            make_message_delta,
            make_message_start,
            make_text_delta,
        )

        upstream_sse = (
            make_message_start(
                message_id="msg_cache",
                input_tokens=50,
                cache_creation=30,
                cache_read=20,
            )
            + make_content_block_start(0, {"type": "text", "text": ""})
            + make_text_delta("cached")
            + make_content_block_stop(0)
            + make_message_delta(output_tokens=10)
        )

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=upstream_sse.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        resp = await client.post(
            "/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "messages": []},
        )
        body = resp.json()
        assert body["usage"]["cache_creation_input_tokens"] == 30
        assert body["usage"]["cache_read_input_tokens"] == 20

    @respx.mock
    async def test_usage_saved_to_db(self, client, db):
        from tests.conftest import make_complete_text_stream

        upstream_sse = make_complete_text_stream(
            "ok", message_id="msg_db", input_tokens=100, output_tokens=50
        )

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=upstream_sse.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        await client.post(
            "/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "messages": []},
        )

        async with db.execute("SELECT * FROM requests WHERE message_id = 'msg_db'") as cur:
            row = await cur.fetchone()
            assert row is not None
            assert row["model"] == "claude-sonnet-4-20250514"
            assert row["input_tokens"] == 100
            assert row["output_tokens"] == 50
            assert row["is_streaming"] == 0  # non-streaming as seen by client

    @respx.mock
    async def test_upstream_error_forwarded(self, client, db):
        """BUG: When upstream returns a JSON error (not SSE), the proxy
        returns a broken aggregated response with null fields instead of
        forwarding the actual error body."""
        error_body = {
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        }

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                401,
                json=error_body,
                headers={"content-type": "application/json"},
            )
        )

        resp = await client.post(
            "/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "messages": []},
        )

        assert resp.status_code == 401
        body = resp.json()
        # BUG: The proxy returns a broken aggregated response instead of the error.
        # Expected: {"type": "error", "error": {...}}
        # Actual:   {"id": null, "type": "message", "content": [], ...}
        assert body.get("type") == "error" or body.get("error") is not None, (
            f"BUG CONFIRMED: Proxy swallowed upstream error and returned: {body}"
        )

    @respx.mock
    async def test_empty_stream_no_db_record(self, client, db):
        """If upstream returns 200 but 0 tokens, no DB record should be saved."""
        from tests.conftest import (
            make_content_block_start,
            make_content_block_stop,
            make_message_delta,
            make_message_start,
        )

        upstream_sse = (
            make_message_start(message_id="msg_empty", input_tokens=0)
            + make_content_block_start(0, {"type": "text", "text": ""})
            + make_content_block_stop(0)
            + make_message_delta(output_tokens=0)
        )

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=upstream_sse.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        await client.post(
            "/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "messages": []},
        )

        async with db.execute("SELECT * FROM requests") as cur:
            rows = await cur.fetchall()
            assert len(rows) == 0, "Should not save request with 0 tokens"


# ═══════════════════════════════════════════════════════════════════════
# Streaming passthrough
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestStreamingProxy:
    """Client sends stream=true. Proxy passes SSE through while tracking usage."""

    @respx.mock
    async def test_sse_passthrough(self, client, db):
        from tests.conftest import make_complete_text_stream

        upstream_sse = make_complete_text_stream(
            "Hello from stream",
            message_id="msg_stream_001",
            input_tokens=120,
            output_tokens=60,
        )

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=upstream_sse.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        )

        assert resp.status_code == 200
        raw = resp.text
        assert "event: message_start" in raw
        assert "event: content_block_start" in raw
        assert "event: content_block_delta" in raw
        assert "event: content_block_stop" in raw
        assert "event: message_delta" in raw
        # Text is split into SSE chunks, so check individual pieces
        assert "Hello" in raw
        assert "stream" in raw

    @respx.mock
    async def test_streaming_usage_saved_to_db(self, client, db):
        from tests.conftest import make_complete_text_stream

        upstream_sse = make_complete_text_stream(
            "test",
            message_id="msg_stream_db",
            input_tokens=200,
            output_tokens=80,
        )

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=upstream_sse.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        await client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        )

        async with db.execute("SELECT * FROM requests WHERE message_id = 'msg_stream_db'") as cur:
            row = await cur.fetchone()
            assert row is not None
            assert row["is_streaming"] == 1
            assert row["input_tokens"] == 200
            assert row["output_tokens"] == 80
            assert row["stop_reason"] == "end_turn"

    @respx.mock
    async def test_streaming_error_passthrough(self, client, db):
        """BUG: Streaming mode always returns 200, even when upstream returns 429."""
        error_body = {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": "Too many requests"},
        }

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                429,
                json=error_body,
                headers={"content-type": "application/json"},
            )
        )

        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        )

        # BUG: Status code is always 200 in streaming mode
        assert resp.status_code == 429, (
            f"BUG CONFIRMED: Streaming response status is {resp.status_code}, expected 429"
        )

    @respx.mock
    async def test_ping_event_passthrough(self, client, db):
        from tests.conftest import (
            make_content_block_start,
            make_content_block_stop,
            make_message_delta,
            make_message_start,
            make_ping,
            make_text_delta,
        )

        upstream_sse = (
            make_message_start(message_id="msg_ping", input_tokens=10)
            + make_content_block_start(0, {"type": "text", "text": ""})
            + make_text_delta("hi")
            + make_ping()
            + make_content_block_stop(0)
            + make_message_delta(output_tokens=5)
        )

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=upstream_sse.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [],
                "stream": True,
            },
        )

        assert "event: ping" in resp.text

    @respx.mock
    async def test_streaming_cache_tokens(self, client, db):
        from tests.conftest import (
            make_content_block_start,
            make_content_block_stop,
            make_message_delta,
            make_message_start,
            make_text_delta,
        )

        upstream_sse = (
            make_message_start(
                message_id="msg_st_cache",
                input_tokens=100,
                cache_creation=40,
                cache_read=30,
            )
            + make_content_block_start(0, {"type": "text", "text": ""})
            + make_text_delta("hi")
            + make_content_block_stop(0)
            + make_message_delta(output_tokens=10)
        )

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=upstream_sse.encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        await client.post(
            "/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "messages": [], "stream": True},
        )

        async with db.execute("SELECT * FROM requests WHERE message_id = 'msg_st_cache'") as cur:
            row = await cur.fetchone()
            assert row["cache_creation_input_tokens"] == 40
            assert row["cache_read_input_tokens"] == 30
