# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 提交规范

**每完成一项修改——bug 修复、新功能、重构、文档更新——都必须先提交到 git 再继续下一项。** 不要把不相关的改动塞进同一个提交，也不要在回合结束时留下未提交的完成工作。提交信息沿用现有风格（简短祈使句主题，例如 `Add ruff checking`、`Fix ci badge typo in readme`）。只有在被明确要求时才 push。

## 常用命令

```bash
uv sync                            # 安装运行时依赖
uv sync --extra dev --extra tests  # CI 实际安装的集合（ruff + pytest + httpx）
cp .env.example .env               # 首次配置
uv run deeperseeker                # 启动服务（或：python app.py）
```

Lint 与测试——CI 在 Python 3.13 上跑的正是这两条：

```bash
.venv/bin/ruff check             # 静态检查（配置在 pyproject.toml，line-length 88）
.venv/bin/ruff check --fix       # 自动修复
.venv/bin/pytest                 # 全量测试
.venv/bin/pytest tests/test_token_pool.py                       # 单个文件
.venv/bin/pytest tests/test_token_pool.py::test_cooldown_auto_recovery  # 单个用例
python tests/test_token_pool.py  # 每个测试文件也可直接运行
```

测试兼容 pytest，但被写成可独立运行（`python tests/<file>.py`），因此每个文件都自行把仓库根目录插入 `sys.path`。**没有 `conftest.py`**，不要假设它存在；`tmpdir` 一类的 fixture 往往是测试文件内部手写的。

依赖变更走 `requirements*.in`（pip-compile）加 `uv.lock`，不要手工编辑 `requirements*.txt`：
- `requirements.in` → 运行时；`requirements-dev.in` → ruff/pip-tools；`requirements-tests.in` → pytest/httpx；`requirements-waf-backup.in` → 已弃用的 Playwright 路径。
- `pyproject.toml` 通过 `hatch-requirements-txt` 读取这些文件，所以 `.in` 文件才是唯一事实来源。

## 架构

一个 FastAPI 反向代理，冒充 DeepSeek 网页版聊天客户端，再把它以 OpenAI / Anthropic 兼容 API 的形式暴露出去。客户端说的是 OpenAI/Anthropic 线格式；代理在两种方向上都做转换，并且底层走的是一条**持久化的网页聊天会话**，而不是无状态 API 调用。

整个系统是仓库根目录下的四个扁平模块（没有包目录），外加 `static/`、`templates/`、`wasm/`：

- **`app.py`**（约 2.5K 行）—— FastAPI 接口层。所有 HTTP 路由、两个流式写出器（OpenAI 用 `stream_response`，Anthropic 用 `stream_anthropic_response`）、请求/响应格式转换、dashboard 与管理端鉴权，以及核心编排函数 `handle_chat`。
- **`functions.py`**（约 2K 行）—— 上游客户端与 SQLite 存储。与 `chat.deepseek.com` 通信，管理 token 池、PoW 挑战求解、cookie 生成、会话持久化、文件上传与 SSE 解析。`StreamToolParser` 和多格式工具调用解析也在这里。
- **`plugin_helper.py`** —— 提示词构造与上下文策略。规范化/消息签名、历史 token 预算、摘要式滚动（summarize-and-rollover）触发条件、附件描述、文件抽取与上传。上下文窗口行为在这里，不在 `app.py`。
- **`middleware.py`** —— `RealIPMiddleware`（由 `TRUSTED_PROXIES` 控制的 X-Forwarded-For 解析）、`RequestIDMiddleware`、`RecovererMiddleware`（把未处理异常转成 JSON 错误而不是裸 500）。`RequestIDMiddleware` 还通过 `audit.bind_request()` 把 request_id / endpoint / scope / model 绑到 ContextVar 上。
- **`audit.py`** —— 审计与性能事件存储。**刻意不 import 项目里任何其他模块**（`functions.py` 要 import 它，反向 import 会成环），自行按 `DB_PATH` 解析出同一个 SQLite 库。写路径 best-effort（`record()` 吞掉所有异常，只留一行 stderr），读路径故意会抛（`/v1/audit/*` 据此返回 503，好让运维分得清「库坏了」和「窗口内无事件」）。

### 请求流程

`POST /v1/chat/completions`（或 `/v1/messages`）→ 把 body 规范化为统一的 `messages` 列表 → `app.py` 中的 `handle_chat`。

`handle_chat` 是全系统的心脏，也是最该先读的函数：

1. **会话查找。** `generate_signature` 对规范化后的消息历史（截至最后一个 assistant 轮次）、模型与 API key scope 做哈希。`find_session` 用该签名映射到已存在的上游网页会话，因此延续的对话能复用会话。创建过程按签名加锁，避免并发的相同请求产生重复会话。
2. **Token 选择。** `pick_token` 从 SQLite token 池中选择，带冷却自动恢复、最小在途数优先、最久空闲作为并列打破规则，以及软性的单 token 并发上限（`acquire_token_slot`）。被限流的 token 在冷却结束前会被排除。
3. **滚动检查。** `needs_rollover` 判断累积上下文是否接近实测约 393K token 的记忆上限。若是，则创建一个**临时**会话专门生成交接摘要，再用 `build_prompt` 把摘要注入一个全新的真实会话。首轮交互永不滚动——首消息路径可接受约 1M token。
4. **发送与流式返回。** `send_message` 驱动上游 SSE；结果被解析、抽取工具调用，再以调用方要求的格式重新发出。

### 故障处理——恢复循环

`handle_chat` 中有三层嵌套恢复，理解它们的顺序很重要：

- **限流恢复。** HTTP 401/403/429 会把 token 标记为 `RATE_LIMITED`，开通新 token，并连同文件一起迁移上下文，然后重试一次。
- **会话重建。** 上游会话失败或返回空响应时，丢弃该会话，用压缩并做过 token 裁剪的历史注入在新会话上重试一次。这里涉及 3 个以上的代码区域，锁序必须小心处理——`_take_lock` 与 chat 锁在切换并重发这一段是被刻意持有的，以防止同签名的并发请求与本轮会话替换发生竞争。如果你重构这段路径，务必保留原有锁序，否则会重新引入已知竞争。
- **上游基础地址故障转移。** `functions.py` 中的 `post_with_failover` 提供了另一条独立的故障转移轴：一个主上游 base 加一个可选的 `DEEPSEEKER_UPSTREAM_FALLBACK`，按顺序尝试。

空/失败的上游响应是**被分类**而不是原样抛出的：`_raise_empty_sse_response`、`_sse_context_limit_hint`、`_scan_recent_sse_errors` 会检查最近的 SSE 行，以区分上下文溢出（触发滚动/新会话路径）与普通上游错误（经 `_api_error_response` 变成 JSON API 错误）。

### 上游客户端细节

- **Cookie 生成。** DeepSeek 有 AWS WAF。当前机制使用 Android 客户端头（`DEEPSEEKER_CLIENT_VERSION`），不需要浏览器。Playwright/Chromium 路径已弃用但保留为 `requirements-waf-backup.in` + `xvfb-run`（见 Dockerfile 中被注释掉的 CMD）；未与维护者确认前不要删除。
- **PoW。** `solve_create_pow` 通过 wasmtime 运行 `wasm/deepseek_pow_solver.wasm` 来应答挑战。该 wasm 来自另一个仓库。
- **把阻塞操作移出事件循环。** SQLite 辅助函数按设计是同步的（CLI 路径与测试共用），统一通过 `_db(...)` = `asyncio.to_thread` 调用。计划中的 aiosqlite 迁移被推迟到「store split」阶段。请求路径中任何新的阻塞调用都应遵循这一模式。
- **模型归一化。** 上游实际只有一个模型 `v4.1flash`。`resolve_model` 接受历史别名（`instant`、`expert`、`vision`、`anthropic/claude-*`）并全部归一到它。`/v1/models` 里的 `anthropic/claude-v4.1flash` 纯粹是为了 Claude Desktop 自动发现。声明的 `context_window`/`max_output_tokens` 反映的是**实测**上游行为，而非官方文档限制。

### 配置与默认值

有两个默认值刻意反直觉且与安全相关：

- `DEEPSEEKER_API_KEY` 未设置时**不会**回退到公开默认值——`_resolve_api_key` 会生成一个 `dsk-…` key，打印一次，并持久化到 `data_dir()/api_key.txt`（权限 0600）。`_log_security_banner` 会在启动时对不安全的 dashboard 凭据发出警告。
- `HOST` 默认 `127.0.0.1`；`docker-compose.yml` 的绑定同样仅限回环。对外暴露代理是需要显式选择的。

Dashboard 鉴权使用会话 TTL、暴力破解锁定（5 次尝试 → 5 分钟）以及 CSRF origin 检查。`RealIPMiddleware` 依赖 `TRUSTED_PROXIES` 被正确设置——空值意味着忽略 `X-Forwarded-For`，这是安全的默认。

## 测试约定

测试按**审计阶段 / issue 编号**命名，而非按模块：`test_stage0_rails.py`（中间件、可信代理、单 chat 锁、双端点故障转移）、`test_token_pool.py`（v2 调度器，issue #32）、`test_context_rollover.py`（issue #22）等等。每个文件顶部的 docstring 说明了它守护的是哪个回归、以及之前的行为为何是错的——修改它覆盖的代码前先读，并保持它记录的不变量。

测试大量 mock 上游，从不访问网络。需要调整阈值时，用 `mock.patch.object` 打补丁模块常量，而不是 reload 模块（reload 会让其他测试持有的引用失同步）。所有面向上游的工作都必须保持可 mock——不得有 import 期的网络调用。

## 关于本项目领域的一点说明

该项目在违反目标服务使用条款的前提下对其进行反向代理。README 提醒使用一次性账号。写面向用户的文案时请牢记这一背景，并且不要添加以规避检测或超出已记录的 token 池/冷却行为之外的绕过限流为目的的功能。