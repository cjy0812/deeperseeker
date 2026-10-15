---
name: project-tech-stack
description: deeperseeker 项目的完整技术栈速查——语言与运行时、依赖管理、Web 框架、上游客户端、存储、测试与质量工具、部署方式。在需要判断「这个项目用什么做 X」「该加什么依赖」「该用什么方式跑/测/部署」「某个库属于哪一层」时使用；也用于回答关于项目技术选型、版本约束、构建链路的问题。
---

# deeperseeker 技术栈

一个 FastAPI 反向代理，冒充 DeepSeek 网页版聊天客户端，对外暴露 OpenAI / Anthropic 兼容 API。

## 语言与运行时

| 项 | 值 |
|---|---|
| 语言 | Python，`requires-python = ">= 3.13"` |
| 目标版本 | `py313`（ruff `target-version`） |
| 入口 | 控制台脚本 `deeperseeker = "app:main"`，也可 `python app.py` |
| 代码组织 | 仓库根目录四个**扁平模块**，无包目录：`app.py`（接口层）、`functions.py`（上游客户端 + SQLite）、`plugin_helper.py`（提示词/上下文策略）、`middleware.py`（ASGI 中间件）；另有 `audit.py`（审计存储） |

## 依赖管理

- **uv** 是推荐的安装与环境工具（`uv sync`、`uv run deeperseeker`），`uv.lock` 已提交。
- 构建后端：**hatchling** + **hatch-requirements-txt**；`pyproject.toml` 的依赖是动态的，从 `requirements*.in` 读取。
- **`requirements*.in` 是唯一事实来源**；`requirements*.txt` 由 **pip-compile**（pip-tools）生成，**不要手工编辑**。

| 文件 | 用途 |
|---|---|
| `requirements.in` | 运行时依赖 |
| `requirements-dev.in` | ruff、pip-tools |
| `requirements-tests.in` | pytest、pytest-asyncio、httpx |
| `requirements-waf-backup.in` | 已弃用的 Playwright/Chromium 路径 |

可选依赖组（hatch metadata hooks）：`dev`、`tests`、`waf-backup`。CI 安装的是 `uv sync --extra dev --extra tests`。

## 运行时依赖（按用途）

| 用途 | 库 |
|---|---|
| Web 框架 / ASGI 服务 | **FastAPI** + **uvicorn** |
| 上游 HTTP 客户端 | **aiohttp**（与 `chat.deepseek.com` 通信） |
| 模板渲染（dashboard） | **jinja2** |
| 表单/文件上传解析 | **python-multipart** |
| 环境变量加载 | **python-dotenv** |
| PoW 挑战求解 | **wasmtime**（运行 `wasm/deepseek_pow_solver.wasm`） |
| token 计数 | **deepseek-tokenizer** |

## 存储

- **SQLite**，走标准库 `sqlite3`，**无 ORM、无迁移框架**。
- 单库文件：`DB_PATH` 环境变量，默认 `<包目录>/deeperseeker.db`。`functions.py` 与 `audit.py` **各自独立解析同一路径**（audit 刻意不 import 项目内其他模块，避免循环依赖）——改动其一时必须同步另一个。
- 建表在 `functions.init_db()`：`CREATE TABLE IF NOT EXISTS` + `PRAGMA table_info` 守卫的 `ALTER TABLE` 做加列迁移。
- 写连接启用 `PRAGMA journal_mode=WAL`。
- **所有阻塞的 SQLite 调用都在 `asyncio.to_thread` 里跑**：`app._db(fn, ...)` 与 `audit.arecord(...)`。请求路径上新增的阻塞调用必须遵循同一模式。

## 测试与质量

| 工具 | 配置 |
|---|---|
| **ruff** | `pyproject.toml`，`line-length = 88`，`select = ["E", "F", "I", "UP", "B", "SIM"]`，`target-version = "py313"` |
| **pytest** | `pytest-asyncio` + `httpx`（Starlette `TestClient` 走 httpx） |

约定（详见 `CLAUDE.md`）：

- 测试文件**按审计阶段 / issue 编号**命名（`test_stage0_rails.py`、`test_token_pool.py`、`test_context_rollover.py`、`test_audit.py` …），不按模块。
- 每个测试文件**既兼容 pytest，也可直接 `python tests/<file>.py` 运行**，因此各自把仓库根插入 `sys.path`。
- **没有 `conftest.py`**，不要假设它存在；`tmpdir` 一类 fixture 在文件内部手写。
- 调整阈值用 `mock.patch.object` 打补丁模块常量，**不要 reload 模块**。
- 测试大量 mock 上游，**从不访问网络**；不得有 import 期的网络调用。

常用命令：

```bash
.venv/bin/ruff check
.venv/bin/ruff check --fix
.venv/bin/pytest
.venv/bin/pytest tests/test_audit.py
.venv/bin/pytest tests/test_token_pool.py::test_cooldown_auto_recovery
```

（Windows 上虚拟环境解释器路径为 `.venv\Scripts\python.exe`。）

## 部署

- **Dockerfile** + **docker-compose.yml**；也支持 **Podman**（rootless，推荐）。
- `docker-compose.yml` **只绑 `127.0.0.1:4000`**；`HOST` 默认 `127.0.0.1`——对外暴露需显式选择。
- 容器内 Playwright/Chromium 路径保留但**已弃用**（`Dockerfile` 中被注释的 `xvfb-run` CMD），未经维护者确认不要删除。

## 前端与静态资源

- `templates/` + `static/`：服务端渲染的 dashboard（`/dashboard`），由 FastAPI 直接托管，**无前端构建步骤、无 Node.js**。
- `wasm/`：PoW 求解器 wasm，来自外部仓库 `deepseek_pow_solver`。

## 值得记住的边界

- 项目在**违反上游服务使用条款**的前提下做反代；README 提示使用一次性账号。不要添加以规避检测或超出已记录 token 池/冷却行为之外的绕过限流功能。
- 上下文窗口的声明值（`context_window` / `max_output_tokens`）反映的是**实测**上游行为，不是官方文档限制。
