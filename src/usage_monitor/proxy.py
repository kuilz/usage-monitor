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
        if key.lower() in ("host", "content-length", "transfer-encoding", "authorization", "x-api-key"):
            continue
        headers[key] = value
    headers["x-api-key"] = settings.anthropic_api_key
    headers["authorization"] = f"Bearer {settings.anthropic_api_key}"
    return headers


@router.post("/v1/messages")
async def proxy_messages(request: Request):
    body = await request.body()
    headers = _build_upstream_headers(request)
    url = f"{settings.anthropic_base_url}/v1/messages"
    db: aiosqlite.Connection = request.app.state.db

    try:
        body_json = json.loads(body)
        original_is_streaming = body_json.get("stream", False)
    except (json.JSONDecodeError, AttributeError):
        body_json = {}
        original_is_streaming = False

    # 强制使用流式请求以获取准确的 usage 数据
    # （部分上游提供商的非流式接口返回不准确的 token 统计）
    body_json["stream"] = True
    body = json.dumps(body_json).encode()

    client = _get_client()
    if original_is_streaming:
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
    """内部用流式请求获取准确的 usage，聚合后返回完整的非流式 JSON 响应。"""
    request_model = body_json.get("model", "")

    # 聚合状态
    message_id = None
    model = request_model
    input_tokens = 0
    output_tokens = 0
    cache_creation = 0
    cache_read = 0
    stop_reason = None
    stop_sequence = None
    content_blocks: list[dict] = []
    current_block: dict | None = None
    current_block_delta_text = ""
    state = _ParseState.IDLE

    status_code = 200
    resp_content_type = "application/json"

    async with client.stream("POST", url, headers=headers, content=body) as resp:
        status_code = resp.status_code

        # If upstream didn't return SSE, forward the raw response as-is
        if "text/event-stream" not in resp.headers.get("content-type", ""):
            raw_body = await resp.aread()
            return Response(
                content=raw_body,
                status_code=status_code,
                media_type=resp.headers.get("content-type", "application/json"),
            )

        async for line in resp.aiter_lines():
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith("event: message_start"):
                state = _ParseState.EXPECT_MSG_START
            elif stripped.startswith("event: message_delta"):
                state = _ParseState.EXPECT_MSG_DELTA
            elif stripped.startswith("event: content_block_start"):
                # 开始新的 content block
                _flush_content_block(content_blocks, current_block, current_block_delta_text)
                current_block = None
                current_block_delta_text = ""
                state = _ParseState.IDLE
                # 解析 data 行（紧跟在 event 行后面）
            elif stripped.startswith("event: content_block_delta"):
                state = _ParseState.IDLE  # 用特殊方式处理
            elif stripped.startswith("event: content_block_stop"):
                state = _ParseState.IDLE
            elif stripped.startswith("event:"):
                state = _ParseState.IDLE
            elif stripped.startswith("data: "):
                try:
                    payload = json.loads(stripped[6:])
                except json.JSONDecodeError:
                    state = _ParseState.IDLE
                    continue

                event_type = payload.get("type", "")

                if event_type == "message_start":
                    msg = payload.get("message", {})
                    message_id = msg.get("id")
                    model = msg.get("model", model)
                    u = msg.get("usage", {})
                    if u.get("input_tokens", 0) > 0:
                        input_tokens = u["input_tokens"]
                    cache_creation = u.get("cache_creation_input_tokens", 0)
                    cache_read = u.get("cache_read_input_tokens", 0)

                elif event_type == "content_block_start":
                    current_block = payload.get("content_block", {}).copy()

                elif event_type == "content_block_delta":
                    delta = payload.get("delta", {})
                    delta_type = delta.get("type", "")
                    if delta_type == "text_delta":
                        current_block_delta_text += delta.get("text", "")
                    elif delta_type == "thinking_delta":
                        current_block_delta_text += delta.get("thinking", "")
                    elif delta_type == "input_json_delta":
                        current_block_delta_text += delta.get("partial_json", "")

                elif event_type == "content_block_stop":
                    _flush_content_block(content_blocks, current_block, current_block_delta_text)
                    current_block = None
                    current_block_delta_text = ""

                elif event_type == "message_delta":
                    delta = payload.get("delta", {})
                    stop_reason = delta.get("stop_reason")
                    stop_sequence = delta.get("stop_sequence")
                    u = payload.get("usage", {})
                    output_tokens = u.get("output_tokens", 0)
                    if u.get("input_tokens", 0) > 0:
                        input_tokens = u["input_tokens"]
                    if u.get("cache_read_input_tokens", 0) > 0:
                        cache_read = u["cache_read_input_tokens"]

                state = _ParseState.IDLE

    # 构建非流式响应 JSON
    result = {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": stop_sequence,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        },
    }

    result_bytes = json.dumps(result).encode()

    # 保存用量
    if input_tokens > 0 or output_tokens > 0:
        await save_request(
            db,
            message_id=message_id,
            model=model,
            is_streaming=False,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
            stop_reason=stop_reason,
        )

    return Response(
        content=result_bytes,
        status_code=status_code,
        media_type="application/json",
    )


def _flush_content_block(blocks: list[dict], block: dict | None, delta_text: str):
    """将累积的 delta 文本刷入 content block 并添加到列表。"""
    if block is None:
        return
    block_type = block.get("type", "")
    if block_type == "text":
        block["text"] = block.get("text", "") + delta_text
    elif block_type == "thinking":
        block["thinking"] = block.get("thinking", "") + delta_text
    elif block_type == "tool_use":
        try:
            block["input"] = json.loads(delta_text) if delta_text else {}
        except json.JSONDecodeError:
            block["input"] = {}
    blocks.append(block)


async def _handle_streaming(
    client: httpx.AsyncClient,
    db: aiosqlite.Connection,
    url: str,
    headers: dict,
    body: bytes,
    body_json: dict,
) -> StreamingResponse:
    request_model = body_json.get("model", "")

    # Send request outside the generator to capture status code upfront
    upstream_req = client.build_request("POST", url, headers=headers, content=body)
    resp = await client.send(upstream_req, stream=True)

    content_type = resp.headers.get("content-type", "")

    # If upstream returned a non-SSE error, forward it directly
    if "text/event-stream" not in content_type:
        raw_body = await resp.aread()
        await resp.aclose()
        return Response(
            content=raw_body,
            status_code=resp.status_code,
            media_type=content_type or "application/json",
        )

    status_code = resp.status_code

    async def generate():
        input_tokens = 0
        output_tokens = 0
        cache_creation = 0
        cache_read = 0
        message_id = None
        model = request_model
        stop_reason = None
        state = _ParseState.IDLE

        try:
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
        finally:
            await resp.aclose()

        if input_tokens > 0 or output_tokens > 0:
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
        status_code=status_code,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
