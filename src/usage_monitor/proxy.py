import json
from enum import Enum, auto

import aiosqlite
import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from .config import settings
from .database import save_request

router = APIRouter()

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
    return _client


class _ParseState(Enum):
    IDLE = auto()
    EXPECT_MSG_START = auto()
    EXPECT_MSG_DELTA = auto()


def _build_upstream_headers(request: Request) -> dict:
    headers = {}
    for key, value in request.headers.items():
        if key.lower() in ("host", "content-length", "transfer-encoding"):
            continue
        headers[key] = value
    headers["x-api-key"] = settings.anthropic_api_key
    return headers


@router.post("/v1/messages")
async def proxy_messages(request: Request):
    body = await request.body()
    headers = _build_upstream_headers(request)
    url = f"{settings.anthropic_base_url}/v1/messages"
    db: aiosqlite.Connection = request.app.state.db

    try:
        body_json = json.loads(body)
        is_streaming = body_json.get("stream", False)
    except (json.JSONDecodeError, AttributeError):
        body_json = {}
        is_streaming = False

    client = _get_client()

    if is_streaming:
        return await _handle_streaming(client, db, url, headers, body, body_json)
    else:
        return await _handle_non_streaming(client, db, url, headers, body, body_json)


async def _handle_non_streaming(
    client: httpx.AsyncClient,
    db: aiosqlite.Connection,
    url: str,
    headers: dict,
    body: bytes,
    body_json: dict,
) -> Response:
    resp = await client.post(url, headers=headers, content=body)
    data = resp.json()

    usage = data.get("usage", {})
    await save_request(
        db,
        message_id=data.get("id"),
        model=data.get("model", body_json.get("model", "")),
        is_streaming=False,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
        stop_reason=data.get("stop_reason"),
    )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in ("content-encoding", "transfer-encoding", "content-length")
        },
    )


async def _handle_streaming(
    client: httpx.AsyncClient,
    db: aiosqlite.Connection,
    url: str,
    headers: dict,
    body: bytes,
    body_json: dict,
) -> StreamingResponse:
    request_model = body_json.get("model", "")

    async def generate():
        input_tokens = 0
        output_tokens = 0
        cache_creation = 0
        cache_read = 0
        message_id = None
        model = request_model
        stop_reason = None
        state = _ParseState.IDLE

        async with client.stream("POST", url, headers=headers, content=body) as resp:
            async for line in resp.aiter_lines():
                yield f"{line}\n"

                stripped = line.strip()

                if stripped == "event: message_start":
                    state = _ParseState.EXPECT_MSG_START
                elif stripped == "event: message_delta":
                    state = _ParseState.EXPECT_MSG_DELTA
                elif stripped.startswith("event:"):
                    state = _ParseState.IDLE
                elif stripped.startswith("data: ") and state != _ParseState.IDLE:
                    try:
                        payload = json.loads(stripped[6:])
                    except json.JSONDecodeError:
                        state = _ParseState.IDLE
                        continue

                    if state == _ParseState.EXPECT_MSG_START:
                        msg = payload.get("message", {})
                        message_id = msg.get("id")
                        model = msg.get("model", model)
                        u = msg.get("usage", {})
                        if u.get("input_tokens", 0) > 0:
                            input_tokens = u.get("input_tokens", 0)
                        cache_creation = u.get("cache_creation_input_tokens", 0)
                        cache_read = u.get("cache_read_input_tokens", 0)
                    elif state == _ParseState.EXPECT_MSG_DELTA:
                        u = payload.get("usage", {})
                        output_tokens = u.get("output_tokens", 0)
                        # Some providers (e.g. Zhipu) put input_tokens here too
                        if u.get("input_tokens", 0) > 0:
                            input_tokens = u["input_tokens"]
                        if u.get("cache_read_input_tokens", 0) > 0:
                            cache_read = u["cache_read_input_tokens"]
                        delta = payload.get("delta", {})
                        stop_reason = delta.get("stop_reason")

                    state = _ParseState.IDLE

        if input_tokens > 0 and output_tokens > 0:
            await save_request(
                db,
                message_id=message_id,
                model=model,
                is_streaming=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_creation,
                cache_read_input_tokens=cache_read,
                stop_reason=stop_reason,
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
