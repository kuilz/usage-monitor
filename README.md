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

其中 Cache Creation / Cache Read 依赖于上游 API 是否支持 Prompt Cache（Anthropic 支持，智谱目前不支持）。

### 这些指标是什么

**Prefill（输入处理）** — 模型收到请求后，需要先"阅读"一遍所有输入 token（system prompt + 历史 messages + 当前用户消息），这个过程叫 prefill。`input_tokens` 就是每次请求的输入 token 总数。

**Decode（输出生成）** — prefill 完成后，模型开始逐个生成回复 token。每生成一个 token 都需要做一次前向传播，因此 decode 是最耗时的阶段。`output_tokens` 是模型生成的回复 token 数。

**Cache Creation（缓存写入）** — 当使用 Prompt Cache 时，系统会把输入中可缓存的部分（比如长 system prompt）存入缓存，下次请求可以复用。`cache_creation_input_tokens` 是首次写入缓存的 token 数。写入缓存有额外成本（比正常 input 贵）。

**Cache Read（缓存命中）** — 当后续请求的输入前缀与已缓存内容匹配时，可以直接从缓存读取，跳过这些 token 的 prefill 计算。`cache_read_input_tokens` 是命中缓存的 token 数，价格远低于正常 input。

三者的关系：`input_tokens = cache_read_tokens + cache_creation_tokens + 需要重新 prefill 的新 token`。

## 工作原理

```
Client → Local Proxy (:8080) → 上游 API (Anthropic / 智谱)
                    ↓
                SQLite
                    ↓
              Web Dashboard
```

代理透明转发请求，同时从响应中提取用量信息存入本地数据库。兼容 Anthropic 和智谱的 Anthropic 兼容接口。

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 配置
cp .env.example .env
# 编辑 .env，填入 API Key 和 Base URL

# 3. 启动
uv run uvicorn usage_monitor.main:app --reload
```

服务默认运行在 `http://127.0.0.1:8080`。

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
- **Daily Usage Chart** — 每日 input/output token 折线图
- **By Model Chart** — 按模型分组的请求分布
- **Recent Requests** — 最近 50 条请求明细

支持 24h / 7d / 30d / All 时间范围切换，每 30 秒自动刷新。

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

| Method | Path | 说明 |
|--------|------|---------|
| POST | `/v1/messages` | 代理转发到上游 API |
| GET | `/` | Web Dashboard |
| GET | `/api/stats/summary` | 汇总统计 |
| GET | `/api/stats/daily?days=7` | 每日统计 |
| GET | `/api/stats/by-model` | 按模型统计 |
| GET | `/api/stats/recent?limit=50` | 最近请求列表 |

## Streaming 支持

完整支持 SSE 流式调用。代理使用 async generator 逐行转发 SSE 事件，零缓冲，不增加延迟。兼容 Anthropic 和智谱两种响应格式：

- **Anthropic**: `message_start` 包含 input_tokens，`message_delta` 包含 output_tokens
- **智谱**: 两个字段均在 `message_delta` 中返回

代理会自动从包含数据的字段中提取，无需手动配置。

## 技术栈

- **FastAPI** — 异步 Web 框架
- **httpx** — 异步 HTTP 客户端（转发请求）
- **aiosqlite** — 异步 SQLite 驱动
- **Chart.js** — Dashboard 图表
