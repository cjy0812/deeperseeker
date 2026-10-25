# 架构说明

DeeperSeeker 是一个反向代理：把 **OpenAI 与 Anthropic 形状的 API** 架在 **DeepSeek 网页版对话会话**之前。
客户端（Claude Code、Cline、OpenAI SDK、Claude Desktop）说的是自己那套标准协议，代理去驱动一个等价于浏览器的
上游会话，再把回复翻译回来。

本文说明一个请求在进程里怎么走、每个模块管什么，以及让令牌池保持健康的那些不变量。逐端点的报文级参考见
[API.md](API.md)；配置项见 [README.zh-CN.md](../../README.zh-CN.md) 的配置表。

## 目录

- [进程形态](#进程形态)
- [请求生命周期](#请求生命周期)
- [模块地图](#模块地图)
- [令牌池与状态机](#令牌池与状态机)
- [上下文：会话与滚动](#上下文会话与滚动)
- [流式、工具调用与截断](#流式工具调用与截断)
- [错误、关联 id 与脱敏](#错误关联-id-与脱敏)
- [审计与可观测性](#审计与可观测性)

## 进程形态

一个由 uvicorn 托管的 ASGI 应用（`app.py`）。没有 worker 池，也没有外部队列：在途计数器、会话 LRU、冷却计时器
都是**进程内**状态，所以多 worker 部署时每个 worker 只会报自己的数字。

持久状态放在数据目录下的单个 SQLite 库（`DB_PATH`，默认 `data/deeperseeker.db`），外加按天切分的 JSONL 审计文件
（`data/audit/audit-YYYY-MM-DD.jsonl`）。SQLite 调用是同步的，因此统一经 `app._db`（一层很薄的 `asyncio.to_thread`
包装）挪出事件循环——凡是碰存储的路由都 `await _db(...)`，而不是直接调 `functions.*`。

导入期状态被刻意压到最少：路径一律从 `BASE_DIR` 解析，不再全局 `os.chdir`；启动横幅、数据库建表、API key 文件都放在
`lifespan` 钩子里完成，因此 import 这个模块（测试、嵌入）不产生副作用。

## 请求生命周期

```
客户端
  |
  v
RecovererMiddleware      兜底异常屏障 -> 带 X-Request-ID 的 JSON 500
  v
RequestIDMiddleware      每请求关联 id（request_id_var）+ 访问日志行
  v
RealIPMiddleware         可信代理后的真实客户端 IP；否则 fail-closed
  v
路由处理器               check_key() -> 令牌槽 -> 会话查找 -> 上游 SSE
  v
上游（DeepSeek 网页版）  签名请求（需要时含 PoW），返回 text/event-stream
  v
DSML / XML / JSON 解析   dsml.StreamToolParser 或 dsml.parse_tools()
  v
协议还原                 formats.py -> OpenAI chunk / Anthropic event / Responses 对象
  v
客户端
```

中间件按 RealIP -> RequestID -> Recoverer 的顺序注册，Starlette 会逆序执行：**Recoverer 最先运行（最外层）**，
所以它下面抛出的任何异常都会被转成仍带关联头的规范 500。

每个承载模型流量的处理器都从同样三步开始：

1. `check_key(request)` —— 校验的是桥自己的 `DEEPSEEKER_API_KEY`，不是上游凭据。
2. 取一个**令牌槽**（`functions.acquire_token_slot`），让该令牌的在途计数反映真实并发；槽在 `finally` 里释放。
3. 按对话签名解析或新建上游**会话**（`functions.find_session` / `save_session`）。

## 模块地图

| 模块 | 行数 | 负责什么 |
|---|---|---|
| `app.py` | ~3200 | FastAPI 应用、全部对外路由、四个大型流式处理器（`handle_chat`、`stream_response`、`stream_anthropic_response`、`stream_responses_response`）、管理页、`main()` |
| `functions.py` | ~1580 | 令牌增删改查与调度器、会话存储、文件归属映射、上游 HTTP + PoW 签名、SSE 读取、用量记录、摘要 |
| `dsml.py` | ~840 | 上游工具调用方言：整包体的 `parse_tools()`、增量用的 `StreamToolParser`、截断判定、工具名修复 |
| `formats.py` | ~680 | 协议转换：Anthropic 消息进出、OpenAI Responses 对象、模型解析、thinking/effort 判定、token 估算辅助 |
| `audit.py` | ~620 | 只追加的 JSONL 事件存储，以及读时聚合（`perf_summary`、`token_rate_limit_summary`） |
| `plugin_helper.py` | ~700 | 注入上游提示的工具使用说明块，让模型以可解析的方言产出工具调用 |
| `middleware.py` | ~240 | 上面那三个纯 ASGI 中间件 |
| `official_api.py` | ~210 | `DEEPSEEKER_MODE=official` 时直通 `api.deepseek.com` |
| `webui.py` | ~140 | 管理页渲染：语言解析、Jinja 上下文、会话门禁 |
| `errors.py` | ~106 | 客户端可见的错误信封，以及它们必经的凭据脱敏 |
| `i18n/` | — | 每种语言一份扁平 key 的 JSON 词典，选择结果存 cookie |
| `routes/audit_api.py` | ~60 | 两个只读审计端点，以依赖注入的 router 形式构建 |

两条依赖规则防止这里长成一团乱麻：

* **`audit.py`、`i18n/`、`webui.py`、`errors.py`、`formats.py`、`dsml.py` 都不从 `app.py` 导入任何东西。**
  它们是叶子——不起代理也能单测。
* **`routes/` 永不导入 `app`。** `build_router(...)` 由宿主模块传入 `admin_or_key`、`get_current_admin`、
  `run_db`、`logger`，因为 `app.py` 导入了这个 router，反向依赖会成环。

`app.py` 仍回导 `errors.py` 与 `formats.py` 的公开名字。这是有意的：既有测试与下游导入方把 `app` 当门面用
（`from app import SINGLE_MODEL`），而很多测试是**在 `app` 的全局命名空间上**打补丁的，只有名字在那里被查找才生效。
把一个函数搬出 `app.py` 之前，先查有没有测试用 `patch.object(app_module, "...")` 打它——标准规则是
*在哪里查找就在哪里打补丁*（patch where it is looked up）。

## 令牌池与状态机

一个「令牌」就是一份 DeepSeek 网页版账号凭据。池子只在你添加的令牌之间轮换，绝不自行申请。

| 状态 | 含义 | 如何离开 |
|---|---|---|
| `ACTIVE` | 可用，在轮换中 | 上游 401/403/429 将其标记为受限 |
| `RATE_LIMITED` | 冷却 `DEEPSEEKER_RATE_LIMIT_COOLDOWN` 秒 | 冷却到期自动恢复 |
| `QUARANTINED` | 凭据被拒——移出轮换 | 只能靠仪表盘上的 **Restore** 手动恢复 |

上游状态到池状态的映射（`functions.mark_limited`）：

| 上游信号 | 效果 |
|---|---|
| `429`（HTTP 状态，或 200 SSE 体里的 `rate_limit_reached` 事件） | 冷却；审计 `reason` 区分 `http_429` 与 `sse_rate_limit` |
| `403` 未达升级阈值 | 冷却，并给 403 连续计数加一 |
| `403` 累计到 `DEEPSEEKER_QUARANTINE_AFTER_403`，其间没有成功轮次 | `QUARANTINED` |
| `401` | 立即 `QUARANTINED`——被拒的凭据不是限流，每分钟重新武装它正是账号被封的方式 |

一次成功轮次会清零 403 连续计数。**陈旧的**在途成功**不能**解除隔离。

遇到限流状态时，坏掉的网页会话会被丢弃，该轮换到**另一个**令牌上带抖动退避重试——最多
`DEEPSEEKER_MAX_UPSTREAM_ATTEMPTS` 次——并做全量历史注入，同时先把账号级文件重新归属到该令牌上。
单令牌并发受 `TOKEN_CONCURRENCY` 限制；槽用 `acquire_token_slot` 获取，且总在 `finally` 里释放，
包括文件上传与下载路径。

池视图（`get_pool_view`）按**别名**暴露每个令牌的状态、在途数、最后使用时间和剩余冷却，**不含任何凭据材料**。

## 上下文：会话与滚动

上游上下文属于**对话会话**，不属于令牌。因此代理在 SQLite 里维护一张
签名 -> `(token_id, session_id, parent_message_id)` 的映射：

* `find_session(sig)` 返回该对话签名记住的会话，优先选令牌仍可用的那个。
* `save_session(...)` 在一轮成功后记录会话；`next_parent(...)` 维护上游期望的消息链。
* `prune_sessions()` 用 `MAX_SESSIONS` 限制表大小，按 LRU 淘汰；`PRUNE_EVERY` 把清扫摊薄，
  而不是每次写入都付这个代价。`MAX_SIG_LOCKS` 限制进程内锁表的规模，该锁用于串行化同一签名上的并发轮次。
* 若签名未知，或发生了账号故障转移，则把完整对话历史注入一个新会话。

**滚动（rollover）。** 记住的会话内上下文大约到 `DEEPSEEKER_MEMORY_LIMIT_TOKENS`（约 393K）时，
代理会让模型生成交接摘要，并在一个用该摘要做种子的新会话里继续，同时留出 `ROLLOVER_SAFETY_TOKENS` 的余量，
摘要长度受 `MAX_SUMMARY_TOKENS` 限制。估算会计入工具 schema 与提示开销（`DEEPSEEKER_PROMPT_OVERHEAD_TOKENS`），
让主动滚动在会话死掉**之前**触发。

对 agent harness 有两条逃生通道很关键：

* **上下文类失败**（上游的上下文措辞，或带上下文提示的空 SSE 体）会让重试**强制走摘要滚动**，
  而不是原样重发同一份超长上下文——否则一个每轮都重放全部历史的 harness 会在每次尝试上以同样方式失败。
* **最新用户消息**被硬性截断到 `DEEPSEEKER_MAX_USER_MESSAGE_TOKENS`（保留尾部），
  使超过约 1M 上游上限的首条消息也能通过，而不是直接失败。

首次交互永不滚动——首条消息路径可接受约 1M token。`/v1/models` 声明的 `context_window` 与
`max_output_tokens` 来自这些**观测到的**行为，而不是上游的保证值。

## 流式、工具调用与截断

上游返回 `text/event-stream`，其载荷是纯文本，而不是结构化的工具调用。工具调用以 XML 风格块出现
（`DSML` 标记、`parameter` 标签块，或裸 JSON），有时本该是 ASCII `||` 的地方会是全角 `｜｜`（U+FF5C）。

* `dsml.parse_tools(text)` 处理完整包体：剥掉游离标记与包裹标签，把正文与调用块分开，解析参数，
  用声明的 schema 修复模型编造的工具名，返回 `(clean_text, tool_calls)`。
* `dsml.StreamToolParser` 处理增量块：只缓冲到足以判断某个前缀是否可能成立（`_is_plausible_stream_entry_prefix`），
  因此跨网络分片被切开的 think 标签与调用块边界会被重新拼装，而不是以半成品形式发出。
* **截断判定是窄口径的。** 缺外层闭合标签是正常的模型行为，**不**当作截断。只有最后一个 `parameter` 标签未闭合，
  或 JSON 花括号在对象中间被切断，才说明流断在参数里（`tool_block_is_truncated`）。发生这种情况时，
  该调用**完全不发出**——发出半写好的调用比不发出更糟，因为客户端会真的去执行它。随后回复以
  `finish_reason: "length"`（OpenAI）/ `stop_reason: "max_tokens"`（Anthropic）收尾，让 harness 知道该重试。

协议还原是 `formats.py` 的职责：`convert_anthropic_messages` 与 `_responses_input_to_messages` 把请求向内归一，
`format_anthropic_response`、`format_responses_response` 以及逐块构造器把回复向外归一。Anthropic 流保持
`content_block_start/delta/stop` 与单调递增的 `index`；OpenAI 流保持 `choices[0].delta`，并以单个终止
`finish_reason` 收尾。

## 错误、关联 id 与脱敏

所有客户端可见的失败都在 `errors.py` 里构造；不存在第二条错误路径。

* `_upstream_http_code(exc)` 从 `UpstreamError` 的类型化 `.status` 取值（连接级失败与 cookie 生成返回 `None`，
  由调用方回落到 `502`）。旧做法——从 `str(e)` 里正则抠 `HTTP (\d{3}):` 前缀——已经删除，
  因为上游任何措辞变化都可能悄悄让令牌轮换与限流标记失效。
* `_api_error_response(e, is_anthropic)` 负责重试过程中的形状；`_terminal_error_response(e, ...)` 负责
  预算耗尽时的退出（池里每个令牌都失败了）。终止退出复用同一套类型化状态机制，并盖上关联 id：
  OpenAI 面放 `error.request_id`，Anthropic 面在消息尾部加 `[trace: …]`（他们的错误对象没有额外字段契约）。
  两面都带 `X-Request-ID`。
* `_redact_secret_material(text)` 在消息进入响应体之前，清掉长 hex/base64 串与 `Bearer` 方案——
  包括来自上游 SSE 错误事件的文本。`CookieGenerationError` 映射到 `503`，因为上游不可达不是客户端错误。

关联 id 来自 `RequestIDMiddleware`，经由 `request_id_var` 上下文变量传递，因此响应体、访问日志行与仪表盘池视图
讲的是同一个故事。

## 审计与可观测性

`audit.py` 是独立的（不从项目里导入任何东西），每行一个 JSON 对象追加到
`data/audit/audit-YYYY-MM-DD.jsonl`，每个 **UTC** 日一个文件（文件名日期必须是 UTC，因为 `ts` 是 epoch 秒；
用本地日期会把跨零点的行归错桶）。追加是一次带锁的 `write()`，不做 fsync——`record()` 本来就在事件循环外运行，
锁的作用是防止大的 detail 字段交错。

记录内容：每个客户端请求、每次命中限流（`reason` 词表为 `http_429` / `sse_rate_limit` / `http_401` / `http_403`）、
每次冷却自动恢复、每次故障转移，以及逐轮用量。保留策略由 `DEEPSEEKER_AUDIT_ENABLED`、
`DEEPSEEKER_AUDIT_RETENTION_DAYS`、`DEEPSEEKER_AUDIT_MAX_ROWS` 控制。

读取路径：

* `audit.perf_summary(window)` —— 窗口内的请求量、错误率、延迟分位数与恢复计数；由 `GET /v1/audit/perf` 提供。
* `audit.token_rate_limit_summary(since)` —— 每个受限令牌一行，含原因分解与自动恢复次数；由
  `GET /v1/audit/rate-limits` 提供。
* 用量聚合（`usage_by_day_key`）—— 按天 × 按掩码后的 key 别名统计请求数、输入/输出 token 与统一费率的成本估算，
  渲染在仪表盘上。一行对应一个**已完成**的轮次，所以失败尝试与流式重试不会重复计数。

两个审计端点都是尽力而为：存储失败返回 `503`（"audit store unavailable"），绝不是 `500`，因为这些数据只用于诊断。
