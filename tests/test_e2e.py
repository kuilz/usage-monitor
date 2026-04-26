"""End-to-end tests exercising the full proxy pipeline."""

import json

import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient, Response

from usage_monitor.config import settings
from usage_monitor.database import init_db
from usage_monitor.main import app

UPSTREAM_URL = f"{settings.anthropic_base_url}/v1/messages"


def _make_sse_stream(
    text: str = "Hello",
    *,
    message_id: str = "msg_e2e",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation: int = 0,
    cache_read: int = 0,
    stop_reason: str = "end_turn",
) -> str:
    """Build a minimal valid SSE stream for testing."""
    parts = [
        f'event: message_start\ndata: {json.dumps({"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant", "model": "claude-sonnet-4-20250514", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": input_tokens, "output_tokens": 0, "cache_creation_input_tokens": cache_creation, "cache_read_input_tokens": cache_read}}})}\n\n',
        f'event: content_block_start\ndata: {json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})}\n\n',
        f'event: content_block_delta\ndata: {json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})}\n\n',
        f'event: content_block_stop\ndata: {json.dumps({"type": "content_block_stop", "index": 0})}\n\n',
        f'event: message_delta\ndata: {json.dumps({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": output_tokens}})}\n\n',
    ]
    return "".join(parts)


@pytest_asyncio.fixture
async def e2e_client(tmp_path):
    db_path = str(tmp_path / "e2e.db")
    db = await init_db(db_path)
    app.state.db = db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, db
    await db.close()


@pytest.mark.asyncio
class TestE2ENonStreaming:
    @respx.mock
    async def test_full_pipeline_non_streaming(self, e2e_client):
        client, db = e2e_client

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=_make_sse_stream("E2E test response", message_id="msg_e2e_ns").encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "test"}],
            },
        )

        # Response checks
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"
        body = resp.json()
        assert body["content"][0]["text"] == "E2E test response"

        # DB checks
        async with db.execute("SELECT * FROM requests WHERE message_id = 'msg_e2e_ns'") as cur:
            row = await cur.fetchone()
            assert row is not None
            assert row["is_streaming"] == 0


@pytest.mark.asyncio
class TestE2EStreaming:
    @respx.mock
    async def test_full_pipeline_streaming(self, e2e_client):
        client, db = e2e_client

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=_make_sse_stream("E2E stream", message_id="msg_e2e_s").encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "test"}],
                "stream": True,
            },
        )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert "E2E stream" in resp.text

        # DB checks
        async with db.execute("SELECT * FROM requests WHERE message_id = 'msg_e2e_s'") as cur:
            row = await cur.fetchone()
            assert row is not None
            assert row["is_streaming"] == 1


@pytest.mark.asyncio
class TestE2EEdgeCases:
    @respx.mock
    async def test_request_without_stream_field(self, e2e_client):
        """Client doesn't include 'stream' field — should default to non-streaming."""
        client, db = e2e_client

        respx.post(UPSTREAM_URL).mock(
            return_value=Response(
                200,
                content=_make_sse_stream("no stream field", message_id="msg_nosf").encode(),
                headers={"content-type": "text/event-stream"},
            )
        )

        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "test"}],
                # No "stream" field
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["content"][0]["text"] == "no stream field"

    @respx.mock
    async def test_request_with_invalid_json_body(self, e2e_client):
        """Client sends invalid JSON body — proxy should reject it."""
        client, _db = e2e_client

        resp = await client.post(
            "/v1/messages",
            content=b"not json at all",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json() == {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Request body must be valid JSON.",
            },
        }

    async def test_usage_api_rejects_unsupported_bucket(self, e2e_client):
        client, _db = e2e_client

        resp = await client.get("/api/stats/usage?bucket=120")

        assert resp.status_code == 400
        assert "bucket must be one of" in resp.json()["detail"]

    @respx.mock
    async def test_multiple_requests_same_model(self, e2e_client):
        """Multiple requests should each create a DB record."""
        client, db = e2e_client

        for i in range(3):
            respx.post(UPSTREAM_URL).mock(
                return_value=Response(
                    200,
                    content=_make_sse_stream(
                        f"msg_{i}",
                        message_id=f"msg_multi_{i}",
                        input_tokens=10 * (i + 1),
                        output_tokens=5 * (i + 1),
                    ).encode(),
                    headers={"content-type": "text/event-stream"},
                )
            )
            await client.post(
                "/v1/messages",
                json={"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "messages": []},
            )

        async with db.execute("SELECT COUNT(*) as cnt FROM requests") as cur:
            row = await cur.fetchone()
            assert row["cnt"] == 3
