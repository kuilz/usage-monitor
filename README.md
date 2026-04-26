# Usage Monitor

本地 API 代理，监控大模型调用的 token 用量明细，提供 Web Dashboard 可视化。

## 为什么做这个

智谱的用量监控面板只显示总 token 数，无法区分 prefill（输入）和 decode（输出）分别消耗了多少。这个工具通过本地代理拦截 API 调用，记录每次请求的详细用量。

## 监控指标

每次 API 调用记录以下数据：

| 指标 | 说明 |
|------|------|
| Input Tokens | Prefill 阶段消耗的输入 token |
| Output Tokens | Decode 阶段生成的输出 token |
| Cache Creation Tokens | 写入 Prompt Cache 的 token 数 |
| Cache Read Tokens | 命中 Prompt Cache 的 token 数 |

其中 Cache Read 在 Anthropic 和智谱的响应中均可获取。Cache Creation（缓存写入量）仅在 Anthropic 响应中返回，智谱虽然内部有缓存机制，但不在响应中暴露缓存写入数据，因此 Cache Creation 对智谱始终为 0。

### 这些指标是什么

**Prefill（输入处理）** — 模型收到请求后，需要先"阅读"一遍所有输入 token（system prompt + 历史 messages + 当前用户消息），这个过程叫 prefill。

**Decode（输出生成）** — prefill 完成后，模型开始逐个生成回复 token。每生成一个 token 都需要做一次前向传播，因此 decode 是最耗时的阶段。`output_tokens` 是模型生成的回复 token 数。

**Cache Creation（缓存写入）** — 当使用 Prompt Cache 时，系统会把输入中可缓存的部分（比如长 system prompt）存入缓存，下次请求可以复用。`cache_creation_input_tokens` 是本次写入缓存的 token 数。写入缓存有额外成本（比正常 input 贵）。

**Cache Read（缓存命中）** — 当后续请求的输入前缀与已缓存内容匹配时，可以直接从缓存读取，跳过这些 token 的 prefill 计算。`cache_read_input_tokens` 是命中缓存的 token 数，价格远低于正常 input。

**Input Tokens** — `input_tokens` 是最后一个缓存断点之后、未被缓存覆盖的 token 数。它**不包含** cache_read 和 cache_creation 的部分。

三者的关系：`input_tokens + cache_creation_input_tokens + cache_read_input_tokens = 实际总输入量`。

> **Anthropic 官方定义**（[Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)）：
>
> - `cache_creation_input_tokens`: Number of tokens written to the cache when creating a new entry.
> - `cache_read_input_tokens`: Number of tokens retrieved from the cache for this request.
> - `input_tokens`: Number of input tokens which were not read from or used to create a cache (that is, tokens after the last cache breakpoint).
>
> `total_input_tokens = cache_read_input_tokens + cache_creation_input_tokens + input_tokens`

## 工作原理

```
Client → Local Proxy (:8080) → 上游 API (Anthropic / 智谱)
                    ↓
                SQLite
                    ↓
              Web Dashboard
```

代理透明转发请求，同时从响应中提取用量信息存入本地数据库。兼容 Anthropic 和智谱的 Anthropic 兼容接口。

### 强制流式转发

无论客户端发起的是流式还是非流式请求，代理内部都会统一转为流式请求发送到上游 API。这是因为部分上游提供商（如智谱）的非流式接口返回不准确的 token 统计数据，而流式 SSE 接口的 usage 数据是准确的。

- 对客户端完全透明——非流式请求的客户端仍然收到非流式响应，客户端无需任何改动
- 数据库中会如实记录客户端的原始请求类型（streaming / sync）

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 配置
cp .env.example .env
# 编辑 .env，填入 API Key 和 Base URL

# 3. 启动
bash usage-monitor.sh start
```

服务默认运行在 `http://127.0.0.1:8080`。也支持 `stop`、`restart`、`status` 命令：

```bash
bash usage-monitor.sh status   # 查看运行状态
bash usage-monitor.sh restart  # 重启服务
bash usage-monitor.sh stop     # 停止服务
```

### 智谱配置示例

```env
ANTHROPIC_API_KEY=your-zhipu-api-key
ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic
```

### Anthropic 配置示例

```env
ANTHROPIC_API_KEY=sk-ant-xxx
ANTHROPIC_BASE_URL=https://api.anthropic.com
```

## 使用方式

将客户端的 `base_url` 指向代理地址即可：

```bash
# 示例：用 curl 测试
curl http://127.0.0.1:8080/v1/messages \
  -H "x-api-key: any-value" \
  -H "content-type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "glm-5.1",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "Hi"}]
  }'
```

代理会自动将 `x-api-key` 替换为 `.env` 中配置的真实 key，客户端可以填任意值。

### Claude Code

在项目目录的 `.claude/settings.json` 中配置：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8080"
  }
}
```

### Python SDK

```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://127.0.0.1:8080",
    api_key="any-value",  # 代理会替换
)
```

## Dashboard

浏览器打开 `http://127.0.0.1:8080/`，查看：

- **Summary Cards** — 总请求数、Input Tokens、Output Tokens、Cache 命中量
- **Usage Trend** — Input/Output token 趋势折线图
- **By Model** — 按模型分组的请求分布饼图
- **Recent Requests** — 最近请求明细表

### 时间范围切换

Dashboard 顶部提供 6 个时间范围按钮：

| 按钮 | 范围 | 图表粒度 |
|------|------|---------|
| **1h** | 最近 1 小时 | 5 分钟 |
| **12h** | 最近 12 小时 | 30 分钟 |
| **Today** | 当天零点 ~ 次日零点 | 30 分钟 |
| **7d** | 最近 7 天 | 每天 |
| **30d** | 最近 30 天 | 每天 |
| **All** | 全部数据 | 每天 |

其中 **Today** 按钮使用绝对时间范围（当天本地零点到次日零点），适合查看当天的用量汇总。

### 请求明细表

Recent Requests 表格展示每次请求的详细信息，每页 10 条，支持分页浏览：

| 列 | 说明 |
|----|------|
| Time | 请求时间 |
| Model | 使用的模型 |
| Input | 输入 token 数 |
| Output | 输出 token 数 |
| Type | 请求类型：蓝色 `stream`（流式）或绿色 `sync`（同步） |
| Stop Reason | 停止原因（`end_turn`、`max_tokens` 等） |

每 30 秒自动刷新。

## 配置项

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `ANTHROPIC_API_KEY` | (必填) | 真实 API Key |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | 上游 API 地址 |
| `PROXY_HOST` | `127.0.0.1` | 代理监听地址 |
| `PROXY_PORT` | `8080` | 代理监听端口 |
| `DB_PATH` | `./data/usage.db` | SQLite 数据库路径 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## API Endpoints

| Method | Path | 参数 | 说明 |
|--------|------|------|------|
| POST | `/v1/messages` | — | 代理转发到上游 API |
| GET | `/` | — | Web Dashboard |
| GET | `/api/stats/summary` | `hours` (可选), `since`/`until` (可选) | 汇总统计（请求数、Input、Output、Cache） |
| GET | `/api/stats/usage` | `hours` (可选), `bucket` (分钟, 默认60), `since`/`until` (可选) | 时间趋势数据，自动填充空缺时间点 |
| GET | `/api/stats/by-model` | `hours` (可选), `since`/`until` (可选) | 按模型统计 |
| GET | `/api/stats/recent` | `limit` (默认50) | 最近请求列表 |

其中 `since`/`until` 参数为 ISO 格式时间戳（如 `2026-04-26T00:00:00`），用于绝对时间范围查询。传入 `since`/`until` 时，`hours` 参数会被忽略。

## Streaming 支持

代理使用 async generator 逐行转发 SSE 事件，零缓冲，不增加延迟。兼容 Anthropic 和智谱两种响应格式：

- **Anthropic**: `message_start` 包含 input_tokens，`message_delta` 包含 output_tokens
- **智谱**: 两个字段均在 `message_delta` 中返回

代理会自动从包含数据的字段中提取，无需手动配置。

## 技术栈

- **FastAPI** — 异步 Web 框架
- **httpx** — 异步 HTTP 客户端（转发请求）
- **aiosqlite** — 异步 SQLite 驱动
- **Chart.js** — Dashboard 图表
