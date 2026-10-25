# API 参考

DeeperSeeker 暴露的全部端点，各自需要什么鉴权，以及返回什么形状。请求在内部怎么走见
[ARCHITECTURE.zh-CN.md](ARCHITECTURE.zh-CN.md)。

示例里的基础地址是 `http://127.0.0.1:4000`（`HOST`/`PORT` 的默认值）。

## 目录

- [鉴权](#鉴权)
- [OpenAI 兼容面](#openai-兼容面)
- [Anthropic 兼容面](#anthropic-兼容面)
- [文件](#文件)
- [模型](#模型)
- [健康检查](#健康检查)
- [审计](#审计)
- [管理界面](#管理界面)
- [错误格式](#错误格式)

## 鉴权

客户端出示的是**桥自己的** `DEEPSEEKER_API_KEY`，绝不是 DeepSeek 上游凭据。三种头写法都接受，
按此顺序检查：

| 头 | 示例 |
|---|---|
| `Authorization: Bearer <key>` | `Authorization: Bearer dseeker` |
| `Authorization: <key>` | `Authorization: dseeker` |
| `x-api-key: <key>` | `x-api-key: dseeker` |

比较是常数时间的（`secrets.compare_digest`）。缺失或错误返回 `401 {"error": "Invalid API key"}`。

**管理界面**是另一套鉴权域：`POST /login` 拿到 `session_id` cookie，受 `SESSION_TTL` 限制，
带暴力破解锁定（5 次失败 -> 5 分钟）以及变更类请求上的 Origin/CSRF 校验。会话缺失或过期时，
管理路由会重定向到 `/login`。

有两个端点**两者皆可**：`GET /v1/audit/perf` 与 `GET /v1/audit/rate-limits` 接受 API key（供抓取）
或活跃的管理会话（供 `/audit` 页面）。

## OpenAI 兼容面

### `POST /v1/chat/completions`

Chat Completions，流式与非流式。

| 字段 | 说明 |
|---|---|
| `model` | 任意字符串；会被解析到唯一的上游模型（见[模型](#模型)） |
| `messages` | OpenAI 消息数组；图片部分（`image_url`，base64 或 URL）会被提取 |
| `stream` | `true` 走 SSE（`data: {...}` 块，以 `data: [DONE]` 结束） |
| `tools` / `tool_choice` | OpenAI function schema；回复里的文本方言调用会被还原为 `tool_calls` |
| `stop`、`max_tokens` | **接受但忽略**——上游网页会话不提供停止/长度控制 |

非流式回复带 `choices[0].message`，其中是 `content` 或 `tool_calls`，外加 `usage` 块。流式回复带
`choices[0].delta`；终止块的 `finish_reason` 为 `stop`、`tool_calls` 或 `length`。

```bash
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $DEEPSEEKER_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"hi"}],"stream":false}'
```

### `POST /v1/responses`（别名 `POST /responses`）

OpenAI Responses API 形状。输入可以是字符串或 item 数组（由 `_responses_input_to_messages` 归一）。
`reasoning.effort` 经 `_responses_effort` 映射。回复是一个 Responses 对象（`_build_response_object`），
`stream: true` 时是一串 `response.*` 事件。

## Anthropic 兼容面

### `POST /v1/messages`（别名 `POST /messages`）

Anthropic Messages API。Claude Code 与 Claude Desktop 说的就是这套。

| 字段 | 说明 |
|---|---|
| `model` | 经 `resolve_model` 解析；`anthropic/claude-v4.1flash` 别名供客户端自动发现 |
| `messages` | Anthropic 消息数组；`convert_anthropic_messages` 向内归一 |
| `system` | 字符串或 block 数组；折进上游提示 |
| `tools` | Anthropic 工具 schema；回复里的文本方言调用变成 `tool_use` 块 |
| `thinking` | 由 `is_thinking_enabled` 判定；think 标签内容跨分片重新拼装 |
| `stream` | `true` 走 Anthropic SSE：`message_start` -> `content_block_start/delta/stop` -> `message_delta` -> `message_stop` |

`stop_reason` 为 `end_turn`、`tool_use` 或 `max_tokens`（后者表示上游流断在工具参数里，
该调用因此被丢弃而不是以半成品发出）。

```bash
curl -s http://127.0.0.1:4000/v1/messages \
  -H "x-api-key: $DEEPSEEKER_API_KEY" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","max_tokens":1024,"messages":[{"role":"user","content":"hi"}]}'
```

## 文件

上传会绑定到执行它的那个令牌（`record_file`），因为上游文件是按账号隔离的。用调度器挑的令牌去取，
只要那个令牌不是属主就会 404，所以读取优先用登记的属主，只有历史遗留或未登记的 id 才回落到调度器。

### `POST /v1/files` 与 `POST /v1/files/upload`

`multipart/form-data`，带一个 `file` 字段。最大 25 MB（超过返回 `413`）。两条路径接受同样的请求体、
返回同样的 id；两种响应形状按面区分：

```json
// /v1/files  （OpenAI 形状）
{"id": "file-...", "object": "file", "bytes": 1234, "created_at": 1770000000,
 "filename": "a.png", "purpose": "answers"}

// /v1/files/upload  （Anthropic 形状）
{"id": "file-...", "type": "file", "filename": "a.png", "size": 1234,
 "created_at": "2026-10-25T00:00:00Z"}
```

错误：`400` 未提供文件、`413` 过大、`500` 上游上传失败、`503` 无可用令牌。

### `GET /v1/files/{file_id}` 与 `GET /v1/files/{file_id}/content`

以流的形式返回文件字节，内容类型取上游的。上游没有该文件返回 `404`，取回握手失败返回 `502`。

```bash
curl -s -o out.png http://127.0.0.1:4000/v1/files/file-abc/content \
  -H "Authorization: Bearer $DEEPSEEKER_API_KEY"
```

## 模型

### `GET /v1/models`（别名 `GET /models`）

返回一个模型条目外加 Anthropic 风格的别名。`context_window` 与 `max_output_tokens` 反映**观测到的**
上游行为（单条首消息约 1M，记住的会话内上下文在滚动前约 393K），不是上游的保证值。`capabilities`
块声明图片/PDF 输入、结构化输出、thinking 与 effort 等级，供客户端自动发现能力。

`DEEPSEEKER_MODE=official` 时改为返回 `official_api.OFFICIAL_MODELS`。

```bash
curl -s http://127.0.0.1:4000/v1/models -H "Authorization: Bearer $DEEPSEEKER_API_KEY"
```

## 健康检查

### `GET /health`

浅检查公开且廉价：至少有一个令牌是 `ACTIVE` 时 `200`，一个都没有时 `503`。令牌存在与否决定就绪状态，
WAF cookie 文件只作为备份可见性上报。

| 查询 | 鉴权 | 行为 |
|---|---|---|
| *（无）* | 无 | 只返回 `{"status": "ok" \| "degraded"}` |
| *（无）* | API key | 追加 `active_tokens`、`cookies_valid`、`version` |
| `?deep=1` | **必需** | 一次真实端到端上游往返（PoW 挑战 -> 求解 -> 建会话）；缺 key 时在产生任何上游流量**之前**返回 `401` |

```bash
curl http://127.0.0.1:4000/health
curl -H "Authorization: Bearer $DEEPSEEKER_API_KEY" 'http://127.0.0.1:4000/health?deep=1'
```

通过鉴权的深度探测会追加 `deep.upstream_reachable`；当它不是 `true` 时，`status` 变为 `degraded`，
HTTP 状态码为 `503`。

## 审计

只读。两个端点都接受**任一**：API key 或活跃的管理会话。

### `GET /v1/audit/perf?window=<秒>`

默认窗口 3600 秒。返回该窗口内的请求量、错误率、延迟分位数与恢复计数，外加 `enabled` 表示审计存储是否开启。

```bash
curl -s 'http://127.0.0.1:4000/v1/audit/perf?window=900' \
  -H "Authorization: Bearer $DEEPSEEKER_API_KEY"
```

### `GET /v1/audit/rate-limits?since=<epoch>`

仅限管理员（令牌 id 与逐令牌限流次数属于运营细节）。返回 `{"tokens": [...], "enabled": bool}` ——
窗口内每个受限令牌一行，含原因分解（`http_429` / `sse_rate_limit` / `http_401` / `http_403` / `http_other`）
与自动恢复次数。

```bash
curl -s 'http://127.0.0.1:4000/v1/audit/rate-limits' -b cookies.txt
```

任一端点存储失败都是 `503 {"error": "audit store unavailable"}`，绝不是 `500`。

## 管理界面

HTML 路由，靠 `session_id` cookie 鉴权。变更类路由还会校验 Origin 头与 Host 是否一致。

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/login` | 登录表单 |
| `POST` | `/login` | 写入 `session_id`；5 次失败 -> 锁定 5 分钟 |
| `GET` | `/logout` | 清除会话 |
| `GET` | `/lang/{code}` | 切换界面语言，存到 `ds_lang` cookie |
| `GET` | `/audit` | 审计与性能页 |
| `GET` | `/dashboard` | 令牌池、按天 × key 的用量、文件统计 |
| `GET` | `/` | 重定向到仪表盘 |
| `POST` | `/tokens/add` | 表单字段 `auth_token`、`alias` |
| `POST` | `/tokens/{token_id}/delete` | 删除令牌 |
| `POST` | `/tokens/{token_id}/restore` | 离开 `QUARANTINED` 的**唯一**途径 |

所有管理面 POST 都以 meta-refresh 重定向回 `/dashboard`（未鉴权时回 `/login`），因此不用 JavaScript
也能从普通 HTML 表单使用。

## 错误格式

错误只在一处构造（`errors.py`），且总是带关联 id。

**OpenAI 面**

```json
{"error": {"message": "...", "type": "api_error", "code": 502, "request_id": "6f1c..."}}
```

状态码为 429 时 `type` 是 `rate_limit_error`，否则是 `api_error`。

**Anthropic 面**

```json
{"type": "error", "error": {"type": "api_error", "message": "... [trace: 6f1c...]"}}
```

trace id 放在消息文本里，因为 Anthropic 的错误对象没有额外字段契约。两面都设置 `X-Request-ID` 头。

状态映射：有类型化的上游状态就用它，连接级失败用 `502`，无法生成 WAF cookie 时用 `503`。每条消息都过一遍
`_redact_secret_material`，所以长 hex/base64 串与 `Bearer` 方案在到达客户端之前会被替换为 `[redacted]`——
即使这段文本来自上游 SSE 错误事件。
