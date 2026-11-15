# FileAgent 项目开发指南

> Java/Spring Boot 背景，首次接触 Python + Vue 全栈开发

---

## 1. Java → Python 核心差异速览

| 概念 | Java | Python |
|---|---|---|
| 类型 | 静态编译 | 动态 + 可选类型注解（Pydantic 做运行时校验） |
| 并发 | 多线程 / 虚拟线程 | asyncio 协程（IO 密集性能好，CPU 密集需线程池） |
| 依赖注入 | `@Autowired` 按类型 | `Depends()` 按函数调用，更显式 |
| 事务 | `@Transactional` | 手动 `commit()` / `rollback()` 或 context manager |
| ORM | JPA / Hibernate | SQLAlchemy（概念相似：Entity→Model, Session→Session） |
| 接口 | `interface` | `ABC` 或鸭子类型（Python 更松散） |
| 构建 | Maven / Gradle | **uv**（替代 pip+venv，10x 速度） |
| 文档 | SpringDoc | FastAPI 零配置自带 Swagger |
| 代码风格 | SpotBugs / Checkstyle | **ruff**（linter+formatter，Rust 实现，秒级） |

### 异步模型重点

Python asyncio 是**单线程协作式调度**——所有 async 函数在同一个事件循环里轮流执行，IO 等待时主动让出控制权。CPU 密集型计算会阻塞整个事件循环，需用 `run_in_executor` 丢到线程池。

### Word 处理对比

| Java (Apache POI) | Python (python-docx) |
|---|---|
| `XWPFDocument` | `Document()` |
| `XWPFParagraph` | `doc.paragraphs` |
| `XWPFRun` | `para.runs` / `para.add_run()` |
| `run.setColor()` | `run.font.color.rgb = RGBColor(0xFF, 0, 0)` |

---

## 2. Agent 开发核心概念

### 什么是 Agent

Agent = **LLM + 工具调用 + 决策循环**。LLM 不再一次输出结果，而是在"思考→行动→观察→再思考"的循环中自主完成任务。

两种基础模式：

- **ReAct**：交替推理和行动，每一步 LLM 决定调用哪个工具或输出最终答案
- **Plan-and-Execute**：先规划步骤列表，再逐步执行（适合多步任务）

### 本项目方案

**早期方案（单轮处理阶段）**：不引入 LangChain 等重框架，自己写一个 30 行的 ReAct 控制循环——理解原理后再学框架才能知道它帮你省了什么。

**当前方案（2026-11-14 修订）**：项目要往长文档分块、RAG、PDF→PPT 这类**多阶段可恢复流水线**走，上面那条结论的前提变了，因此选定 **LangGraph** 做编排层。

变的是需求形态，不是当初的判断：

| 当初的前提 | 现在的现实 | 为什么自实现循环不够用 |
|---|---|---|
| 一次 LLM 调用出结果 | 分块 → map → reduce，N 个并发子任务 | 要 fan-out / fan-in 和局部失败重试 |
| 失败就整条重来 | 进程重启后能从中间某步接着跑 | 要 checkpoint 持久化，`BackgroundTasks` 不落地 |
| 流程写死在代码里 | 大纲要人确认后才继续生成 | 要 interrupt / resume 的人在环中 |
| 无反馈回路 | 生成 → 自检 → 修订循环 | 要状态图和条件边 |

### 框架选型参考

| 框架 | 适用场景 | 本项目 |
|---|---|---|
| LangGraph | 有状态多步 Agent 图，带 checkpoint / interrupt | **阶段 3 引入**，首练用在长文档分块批改 |
| LangChain | 全功能 Agent、海量集成 | 不用。只要 LangGraph + 直连 OpenAI 兼容端点，少一层抽象 |
| LlamaIndex | RAG / 数据索引 | 不用。阶段 4 用 pgvector 自己写检索，链路更可控 |
| pgvector | Postgres 内做向量检索 | **阶段 4 采用**，省掉独立向量库 |
| 结构化输出 / JSON 模式 | 让模型产出可校验的 JSON | 阶段 0 已铺好 `chat_json` 三级降级 |
| 自实现 ReAct | 理解原理 + 轻量 | 已完成使命，不再往单轮场景上加东西 |

> 阶段 0（LLM 传输层）刻意**不依赖任何 agent 框架**：超时、重试、并发闸、计量、能力路由都是纯基础设施，用 httpx 手写更清楚，也能保证单测不引入新依赖。LangGraph 到阶段 3 才进来。

---

## 3. 开发工具推荐

### 3.1 Python 生态工具链

| 工具 | 用途 | 对标 Java | 安装 |
|---|---|---|---|
| **uv** | Python 包管理器（替代 pip+venv） | Maven 的依赖+构建一体化 | `pip install uv` |
| **ruff** | linter + formatter | SpotBugs + Checkstyle | `uv tool install ruff` |
| **mypy** | 静态类型检查 | 编译期类型检查 | `uv tool install mypy` |
| **pytest** | 测试框架 | JUnit | `uv add pytest` |
| **pre-commit** | Git 提交前自动检查 | Git hooks | `uv tool install pre-commit` |

### 3.2 uv 使用说明

uv 比 pip 快 10-100 倍，且自动管理虚拟环境，不用手动 `venvScriptsactivate`。

```bash
# uv 常用命令（对照 pip/venv 理解）
pip install xxx         →  uv add xxx
pip freeze > req.txt    →  uv sync             # 自动生成 uv.lock
python -m venv venv     →  uv venv             # uv 创建虚拟环境
source venv/bin/activate →  (uv 自动检测/使用)
pip install -r req.txt  →  uv sync             # 按 pyproject.toml 安装

# 热知识：uv 所有项目配置写在 pyproject.toml 里（类比 Maven 的 pom.xml）
# uv 的项目文件就是 pyproject.toml，依赖声明方式和 requirements.txt 二选一
```

### 3.3 Git 规范

```bash
# .gitignore 必加内容
__pycache__/
venv/
*.pyc
.env
server/uploads/*
server/outputs/*
node_modules/
dist/

# 提交信息格式（对标 Angular Commit Convention）
type: 简短描述

# type 取值：feat / fix / docs / refactor / test / chore
# 例：feat: 添加 JWT 认证中间件
```

### 3.4 Pre-commit 配置（可选）

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.0
    hooks:
      - id: ruff
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.11.0
    hooks:
      - id: mypy
```

安装后 `pre-commit install`，每次 git commit 自动跑 ruff 和 mypy。

---

## 4. 项目搭建（分步骤）

### 4.1 环境准备

```bash
# 确认 Python 版本 >= 3.11
python --version

# 安装 uv（全局一次，后续所有项目共用）
pip install uv

# 创建项目目录
mkdir FileAgent && cd FileAgent
mkdir -p server/{models,schemas,api/v1,services,middleware,tests}
mkdir -p server/{uploads,outputs}
mkdir -p client

# Git 初始化
git init
echo -e 'venv/\n__pycache__/\n*.pyc\n.env\nserver/uploads/*\nserver/outputs/*\nnode_modules/\ndist/' > .gitignore
git add -A
git commit -m "chore: 初始化项目结构"
```

### 4.2 后端初始化（用 uv）

```bash
cd server

# uv 自动创建虚拟环境 + 初始化 pyproject.toml（类比 Maven 的 pom.xml）
uv init

# 添加核心依赖
uv add fastapi "uvicorn[standard]" sqlalchemy pymysql python-docx \
    "python-jose[cryptography]" "passlib[bcrypt]" python-multipart \
    httpx pydantic-settings pytest httpx

# 生产依赖 vs 开发依赖（对标 Maven 的 scope）
# uv add pytest --dev  # 开发依赖安装在 [dev-dependencies] 下
# uv add uvicorn       # 生产依赖安装在 [project.dependencies] 下
```

### 4.3 前端初始化

```bash
cd ../client
npm create vite@latest . -- --template vue-ts
npm install
npm install element-plus pinia vue-router axios
npm install -D @types/node
```

### 4.4 最小可运行验证

**server/main.py**：
```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="FileAgent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
async def health():
    return {"status": "ok"}
```

```bash
# 启动后端
cd server
uv run uvicorn main:app --reload --port 8000
# 访问 http://localhost:8000/docs 看到 Swagger 即成功
```

```bash
# 启动前端
cd client
npm run dev
# 访问 http://localhost:5173 看到 Vue 页面即成功
```

### 4.5 文件结构总览

```
FileAgent/
├── server/
│   ├── main.py              # FastAPI 入口 + 中间件 + 异常处理
│   ├── config.py             # pydantic-settings 读取 .env
│   ├── database.py           # SQLAlchemy engine + get_db 依赖
│   ├── models/               # ORM: user.py task.py operation_log.py provider.py
│   ├── schemas/              # Pydantic: 请求/响应模型
│   ├── api/v1/               # 路由: auth tasks process logs providers
│   ├── services/             # 业务: auth llm word diff
│   ├── middleware/auth.py    # JWT Depends
│   ├── tests/                # pytest
│   ├── uploads/ outputs/     # 文件暂存
│   └── requirements.txt
├── client/
│   └── src/
│       ├── views/            # Login Home Tasks Logs Providers
│       ├── components/       # AppLayout 等公共组件
│       ├── stores/           # Pinia: auth.ts home.ts（工作台状态持久化）
│       ├── api/              # Axios 封装 + 类型定义
│       └── router/index.ts
├── docker-compose.yml
└── .env
```

---

## 5. 核心模块实现要点

### 5.1 认证系统（auth）

核心三步：密码哈希 (passlib+bcrypt) → JWT 签发 (python-jose) → Depends 注入校验

路由：`POST /register` + `POST /login`，登录成功返回 `{access_token, token_type: "bearer"}`。JWT payload 存 `sub`(user_id) + `exp`。

前端 Axios 拦截器：请求前自动从 localStorage 取 token 加到 Authorization 头；响应拦截器遇到 401 清除 token 跳登录页。

### 5.2 数据库层

SQLAlchemy 连接池配置：`pool_size=10, max_overflow=20, pool_pre_ping=True`。通过 `Depends(get_db)` 注入 Session，每个请求结束自动关闭。

**建表**：在 `database.py` 中 `Base.metadata.create_all(bind=engine)`，首次启动自动建表。后续迁移用 Alembic。

**事务**：用 context manager 封装 try/commit/rollback，不要手动到处写 try-except。

### 5.3 模型提供商（provider）

通用 OpenAI 兼容接口：所有模型统一调 POST `{base_url}/chat/completions`，传 `{model, messages, max_tokens, temperature}`。provider 表存 base_url/api_key/model_name，用户可随时增删改。换模型 = 改一行配置，不动代码。

**数据隔离（重要）**：provider 按 `user_id` 归属，和 task 一致——每个用户只能看到/修改/删除自己创建的 provider，杜绝越权读写他人 API Key。

- 响应统一对 `api_key` 打码（前 4 位 + `****`），不返回真实密钥
- 创建时 `user_id` 从登录态取，**绝不接受请求体传入**
- 访问他人 provider 一律返回 404（而非 403，不暴露"存在但不属于你"）
- 任务指定 provider 时校验其归属当前用户；处理时若 provider 已被删除则**显式报错**，不静默 fallback 到别的模型
- LLM 默认 provider 选择（任务未指定时）也限定在当前用户名下

**能力路由（阶段 0 新增）**：`model_providers.capabilities` 存逗号分隔串（`text` / `vision` / `embedding`），由 `resolve_provider()` 按用途筛。

- 任务显式指定的提供商能力不匹配 → **直接报错**，不静默换成别的模型
- 未指定时在当前用户 active 提供商里挑第一个支持该能力且 id 最小的
- 每个用户的提供商就是个位数，所以在 Python 侧过滤，不写字符串/JSON 的 SQL 匹配
- 入口用 `field_validator` 拒未知能力值并把顺序归一化，同一集合永远存成同一个串

> ⚠ 本节早期设计为"所有用户共享 provider"，因存在凭据越权风险已改为按用户隔离。


### 5.4 上传 + 处理 + 下载流程

这是核心业务链路，分三步：

1. **上传**：接收 .docx / .pdf → 校验扩展名 → 按扩展名派发到对应提取器（见 5.10）→ 存 operation_log(status=pending) → **返回 extracted_text 供前端预览**
2. **处理**：`POST /process/{id}` **立即返回** `status=processing`，真正的 LLM 调用放进 FastAPI `BackgroundTasks` 异步执行——取 task.prompt_template → `{{content}}` 替换为文档文本 → 调 LLM → 后台把结果写回 `processed_content(status=done)` 或 `error_message(status=failed)`。前端通过轮询拿结果。
3. **下载**：读 operation_log → 如果是批改任务，diff 对比原文与结果；否则直接写入 → 生成 .docx 返回（文件名取原名去扩展再补 `.docx`，PDF 输入不会拿到一个叫 `.pdf` 的 docx）

**为什么处理要后台化（不在请求里 await）**：LLM 可能耗时十几秒到一两分钟。若在请求内 `await`，客户端一旦断开（刷新/关页/网络抖动），请求被取消，日志会**永远卡在 processing**。改用后台任务 + 独立 DB session（`SessionLocal()`，不能用请求级 session，它在响应发出后就关了），响应先发出去，处理在后台跑完，断开重连也不影响。

**僵尸状态清理**：`BackgroundTasks` 不持久化，进程重启会丢失正在跑的后台任务。`main.py` 启动时把所有遗留的 `processing` 日志重置为 `failed`（错误信息"服务重启导致处理中断，请重试"），避免前端无限轮询。⚠ 仅适用单 worker；多 worker 部署应改用带超时阈值的 reaper 或任务队列（如 Celery/RQ）。

**轮询机制**：因为处理是异步的，前端点击"开始处理"后**总是**走轮询——通过 `GET /process/{id}/status` 每 5 秒查询一次（最多 120 次 / 10 分钟），`done` 自动推进到下载步骤，`failed` 显示错误。组件失活（onDeactivated）时暂停轮询，重新激活（onActivated）时恢复。

**步骤导航**：上传步骤可返回选任务（保留选中状态）；下载步骤可"处理新文件"回到上传步骤。每个步骤之间都有上一步按钮。

**传输层细节见 5.7**：这一段只讲业务流程，超时 / 重试 / 并发 / 计量 / 能力路由的实现口径在 5.7，改 `services/llm.py` 前必读。

### 5.5 批改红色标注（diff 核心）

流程：原文(黑色) + LLM修订文 → `difflib.SequenceMatcher` 逐字对比 → 差异类型分三类：

| 差异 | Word 处理 |
|---|---|
| equal（未变）| 黑色默认字体 |
| insert（新增/修改）| `run.font.color.rgb = RGBColor(0xFF, 0, 0)` 红色 |
| delete（删除）| 红色 + `run.font.strike = True` 删除线 |

用 `difflib` 标准库即可，不需要额外依赖。

### 5.6 操作日志

记录每次操作的完整链路：谁(user_id)、什么文件(original_filename)、用的什么任务(task_name)、什么模型(provider_name)、上传时间、处理时间、状态(pending→processing→done/failed)、失败原因(error_message)。分页查询，按时间倒序。

**搜索筛选**：支持按状态（pending/processing/done/failed）筛选和按文件名模糊搜索（SQLAlchemy `ilike` 不区分大小写）。两个筛选条件可组合使用。

**失败重试**：状态为 `failed` 的记录可在日志页一键重试，重新调用 `POST /process/{id}` 处理。重试前弹出确认对话框，处理完成后自动刷新列表。

**空状态**：无数据时显示 `el-empty` 组件提示。如果当前有筛选条件，提示"清除筛选"按钮；如果无任何记录，引导用户前往工作台。

### 5.7 LLM 传输层（阶段 0）

`services/llm.py` 只做传输与计量，不做业务编排。四块职责边界清楚：

| 函数 | 职责 | 碰 DB |
|---|---|---|
| `chat(messages, provider, ...)` | 发一轮请求，返回 `LLMResult`（内容 + token + 耗时 + attempts） | 否 |
| `chat_json(...)` | 要 JSON 输出，按 `json_schema → json_object → 不带` 三级降级；返回 `(LLMResult, 解析结果或 None)` | 否 |
| `resolve_provider(db, user_id, provider_id, capability)` | 按能力和归属选提供商 | 是（只读） |
| `record_llm_call(...)` | 写一行 `llm_calls` 计量 | 是（自建独立 session） |

**为什么 `chat()` 不接 DB**：原来的 `call_llm(db, prompt, ...)` 在 async 函数里做同步 Session 查询，本身就在阻塞事件循环，再加计量写入会更糟。拆开之后 `chat()` 能用假 client 直接单测，不需要真端点。

**超时分两层**：`llm_timeout_connect` 只管 TCP + TLS 建连，`llm_timeout_read` 等响应收完；单次调用可用 `read_timeout` 覆盖读取层。长文档和短文本走同一个 `chat()`，靠这个参数分别放宽。

**重试白名单**：只重试网络错误、超时、429、5xx（见 `_RETRYABLE_STATUS`）。其他 4xx 一次就失败——密钥无效重试也不会变对，纯烧 token。429 优先听 `Retry-After`（超过 60 秒不认），否则指数退避加随机抖动，避免并发请求同拍重试。

**降级不等于重试**：`chat_json` 只在"端点明确拒绝 `response_format`"时降一档。**输出解析失败不降档也不重打**——降级方向是越来越松，同一份提示再打一次大概率还是烂 JSON，白烧一倍 token。这条是写单测时才发现的，第一版实现会为一句话的解析失败连打三次。

阶段 1 又把「解析失败」的语义从**抛错**改成**返回 `(result, None)`**：模型不按格式出货是预期结果而不是异常，调用方（`services/grading.py`）拿到 `None` 就退回自由文本，用户照样能拿到批改结果。返回类型是 `tuple[LLMResult, Any | None]`。

**并发闸**：进程内 `asyncio.Semaphore(llm_max_concurrency)`。⚠ 和启动 reaper 一样是单 worker 假设，多 worker 时每进程各有一个，不是全局上限。

**共享 httpx client**：模块级单例，`lifespan` 启动时预热、关闭时 `aclose()`。省掉每次调用的 DNS + TLS 握手，同时把"代理环境变量非法导致客户端构造失败"这类错误提前到启动日志里暴露，而不是等用户点处理才炸。`LLM_TRUST_ENV_PROXY=false` 是绕开 `NO_PROXY` 含非法条目（如裸 `[::1]` 会报 `InvalidURL: Invalid port`）的开关。

**计量是旁路**：`llm_calls` 记 token / 耗时 / attempts / 状态，但写失败只 `logger.warning`，绝不把已经成功的处理判成 failed。`attempts` 在失败路径要从 `LLMError.attempts` 取——不取的话库里永远是 1，看不出"重试到耗尽"和"一次就挂"的区别（这条也是实测踩出来的）。

**provider 名存快照**：处理时直接记实际选中的 `provider.name`，不再事后回查默认提供商——回查可能查到另一个模型。

### 5.8 结构化批改（阶段 1）

批改任务（`task.is_grading == True`）不再只要一篇修订文，而是同时产出**可定位、可打分的问题清单**。

| 模块 | 职责 |
|---|---|
| `schemas/grading.py` | `GradingIssue` / `GradingResult` 契约、给端点看的 `GRADING_JSON_SCHEMA`、`normalize_type()` 类型归一化 |
| `services/grading.py` | 输出契约（system 消息）、解析、原文定位、序列化落库/出库 |
| `api/v1/process.py:_run_model` | 按 `is_grading` 分流：批改走 `chat_json`，普通任务走 `chat` |

**关键设计：坐标不由模型给。** LLM 不数字符，让它输出 `paragraph_index` 或 `char_offset` 会大面积错位，而且错了没有任何信号，批注指到错位置比不指更糟。所以模型只逐字摘原文片段，`start/end` 由后端在 `original_content` 上精确匹配：

- 同一 `original` 出现多次且提了多条 → 依次认领不同出现位置（`_find_unused_span` 扫描时跳过已占用区间）
- 匹配不到 → `start=None`，前端显示"未定位"，绝不猜
- 区间互不重叠，`claimed` 列表保证不重复认领

**类型归一化是开放集。** 批改提示词由用户自写，模型爱用中文类型名（"错别字""前后不一致"）。硬枚举会在换模型时整片报错，所以 `_TYPE_ALIASES` 中英别名都收到 8 个 canonical 值，认不出来归 `other`，绝不因类型名丢掉一条意见。

**失败降级而不是失败。** 端点没吐合法 JSON、或缺 `revised_text` → `degraded=True`，`processed_content` 存模型原始输出，行为完全等价于阶段 0 之前，diff 下载不受影响。用户的文档必须拿到一个结果，不能因为模型不守格式就判 failed。

**为什么不破坏现有下载：** 结构化输出里**保留完整 `revised_text`** 写进 `processed_content`，`create_diff_docx` 一行没改。问题清单单独存在 `operation_logs.structured_output`（JSON 串），NULL 表示非批改任务或历史数据。

**`grading_degraded` 必须单独暴露给前端。** 少了这个标记，"批了但没发现问题"和"没能结构化所以没清单"都显示 0 条 —— 那是撒谎。

**日志页只带条数。** `LogResponse.issue_count` 由 `_to_response()` 解析 JSON 得出，不把整份清单拖进列表响应。

### 5.9 本地假 LLM 端点（验证用）

`server/test_llm_server.py` + `server/test_e2e_structural.py` 构成结构化批改的端到端验证：

- mock 端点**刻意不支持 `response_format`**（收到 `json_schema`/`json_object` 返回 400），如实模拟国产兼容端点的常见行为，逼 `chat_json` 走降级路径
- 返回固定 JSON（含错别字意见 + 修订文）和固定 usage，便于断言解析、定位、计量
- 全程零真实 API Key、零 token 费用

跑法（后端默认端口被占时可用 `FA_API` 覆盖，见 §9.4）：

```bash
uv run uvicorn test_llm_server:app --port 19000   # 另开一个终端
uv run uvicorn main:app --port 8000               # 或另选端口
uv run python test_e2e_structural.py              # 40 项断言
```

### 5.10 文档提取层（阶段 1.5：输入格式扩到 PDF）

平台定位是"文档 AI 处理"，但提取器只有 docx 一个，这一轮补上 PDF。**输出的分发口径**：

| 扩展名 | 提取器 | 依赖 | 说明 |
|---|---|---|---|
| `.docx` | `services/word.py:extract_text_from_docx` | python-docx | 只读 `doc.paragraphs`，表格/文本框不进正文 |
| `.pdf` | `services/pdf.py:extract_text_from_pdf` | PyMuPDF 2.x | 两遍拼段落 + 噪声剔除，见下 |

白名单**不再是单独的常量**：`api/v1/process.py:TEXT_EXTRACTORS` 是"扩展名 → 提取器"的字典，`ALLOWED_EXTENSIONS = set(TEXT_EXTRACTORS)`。加格式只动这张表，白名单跟着走，不会出现"支持了但拒绝"或"白名单放行了但没提取器"。

**核心约束：PDF 的提取结果必须和 docx 同构**（`"\n"` 分隔的自然段）。整条下游（diff、`_find_unused_span` 定位、`create_diff_docx` 重组段落、前端 `buildParagraphs`）都建立在这个约定上；只要 PDF 交出"每视觉行一段"，diff 就会把半句话当段落、批注指到换行处。为此做两遍：

1. **block 内按行拼**：Word/LaTeX 导出的件一个自然段是一个 block，内部以 `\n` 分行。拼接口径分三类——英文断词（行尾 `-` + 下行小写）去连字符直接接、两侧都是 CJK 不加空格（加了反而让 `original` 匹配不上）、其余补一个空格。
2. **block 间按几何邻接拼**：PPT/幻灯导出的件是**每视觉行各占一个 block**（pdf-to-ppt 那份 skill 也是这么记录的），只在"垂直间隙 ≤5pt + 左边缘差 ≤3pt + 上一段没以句末标点收束"三条同时成立时才合并。左边缘条件顺带挡住了跨栏误合。

**噪声剔除两条规则**（都是判据而非硬编码）：

- 页码：内容必须是纯数字/罗马数字，**并且**落在页面上 8% 或下 20% 的留白带里。少了位置条件会把正文里的年份、条款号、表格序号一起删掉（`2024` 恰好是四位数）。
- 跨页高频装饰：短文本（≤20 字符）、在 ≥60% 的页面原样出现、且不带句末标点、且文档 ≥3 页。用频次是因为换模板装饰词就变了，但"每页都有一模一行"不变；带标点的重复行是内容（诗词副歌、免责声明），宁可留噪声也不能丢字。

**三条失败路径都是 400，不是 500**（`PdfError` 由路由层转）：打不开（坏文件）、有打开密码、没有文字层。最后一条最常见——扫描件必须明确告诉用户"当前不支持图片型 PDF"，回一段空文本会让人以为文档本来就是空的。

**已知边界**：多栏长段落跨栏仍会被切开（文本层只给坐标不给阅读顺序，几何合并刻意不跨栏）；不做 OCR；不读表格结构。这些和 docx 那边一致，属功能边界不是缺陷。

**依赖代价（实测）**：`uv add pymupdf` 让 `uv.lock` 从 46 包涨到 56、`.venv` 到 303M（pymupdf 99M + 它带来的 onnxruntime 49M + numpy 22M，因为 `pymupdf-layout` 拉了版面分析模型）。留下的理由：阶段 5 的 PDF→PPT 本来就要 PyMuPDF 的 block/图片几何能力，换成纯 Python 的 pypdf 省 170M 但到阶段 5 还得再引一次，反而变成两套 PDF 库。要瘦身就整体退到 pypdf 并接受只做单遍提取。

> 顺带修正了 ORM 与真实库的一处漂移：`operation_logs.original_content / processed_content` 改为 `Text().with_variant(LONGTEXT, "mysql")`。MySQL 的 `TEXT` 是 64KB **字节**上限，中文按 3 字节算两万字就撞墙，而 PDF 输入让长文档变成常态；本机老库早就是 LONGTEXT，这样声明让新建库和它一致（SQLite 单测仍走 TEXT）。`structured_output` 保持 TEXT——它只装问题清单，不装全文。

---

## 6. 数据库设计

```sql
CREATE TABLE users (
  id INT PRIMARY KEY AUTO_INCREMENT,
  username VARCHAR(50) UNIQUE NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE model_providers (
  id INT PRIMARY KEY AUTO_INCREMENT,
  user_id INT NOT NULL,
  name VARCHAR(50) NOT NULL,
  base_url VARCHAR(255) NOT NULL,
  api_key VARCHAR(255) NOT NULL,
  model_name VARCHAR(100) NOT NULL,
  capabilities VARCHAR(100) NOT NULL DEFAULT 'text',
  max_tokens INT DEFAULT 4096,
  temperature FLOAT DEFAULT 0.7,
  is_active TINYINT DEFAULT 1,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE tasks (
  id INT PRIMARY KEY AUTO_INCREMENT,
  user_id INT NOT NULL,
  provider_id INT,
  name VARCHAR(100) NOT NULL,
  description TEXT,
  prompt_template TEXT NOT NULL,
  is_grading TINYINT DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES users(id),
  FOREIGN KEY (provider_id) REFERENCES model_providers(id) ON DELETE SET NULL
);

CREATE TABLE operation_logs (
  id INT PRIMARY KEY AUTO_INCREMENT,
  user_id INT NOT NULL,
  task_id INT,
  task_name VARCHAR(100) NOT NULL,
  provider_name VARCHAR(50),
  original_filename VARCHAR(255) NOT NULL,
  original_content LONGTEXT,
  processed_content LONGTEXT,
  structured_output TEXT,
  uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  processed_at DATETIME,
  status ENUM('pending','processing','done','failed') DEFAULT 'pending',
  error_message TEXT,
  FOREIGN KEY (user_id) REFERENCES users(id),
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE TABLE llm_calls (
  id INT PRIMARY KEY AUTO_INCREMENT,
  user_id INT NOT NULL,
  provider_id INT,
  log_id INT,
  purpose VARCHAR(30) NOT NULL DEFAULT 'chat',
  provider_name VARCHAR(50),
  model_name VARCHAR(100),
  prompt_tokens INT NOT NULL DEFAULT 0,
  completion_tokens INT NOT NULL DEFAULT 0,
  total_tokens INT NOT NULL DEFAULT 0,
  latency_ms INT NOT NULL DEFAULT 0,
  attempts INT NOT NULL DEFAULT 1,
  status VARCHAR(20) NOT NULL DEFAULT 'success',
  error_message TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY (provider_id) REFERENCES model_providers(id) ON DELETE SET NULL,
  FOREIGN KEY (log_id) REFERENCES operation_logs(id) ON DELETE SET NULL
);
```

> **外键 ON DELETE 说明**：`tasks.provider_id` 和 `operation_logs.task_id` **意图**是 `SET NULL`——删除 provider/task 时自动置空引用而非报错。`llm_calls` 三个外键同理：删用户连带清计量行，删 provider/log 只置空引用、计量行本身留下。日志和计量靠 `task_name` / `provider_name` 快照在关联被删后仍可读。应用层删除时同样**主动**先置空引用。
>
> ⚠ **上面这段 DDL 与本机真实库并不完全一致**（2026-11-14 用 `SHOW COLUMNS` + `information_schema.REFERENTIAL_CONSTRAINTS` 实测）：
>
> | 项 | 文档 / ORM 声明 | 本机真实库 | 影响 |
> |---|---|---|---|
> | `tasks.provider_id` 删除规则 | `ON DELETE SET NULL` | **NO ACTION** | 靠应用层先置空才没 500；新加删除路径若忘了就炸 |
> | `operation_logs.task_id` 删除规则 | `ON DELETE SET NULL` | **NO ACTION** | 同上 |
> | `operation_logs.status` | ORM 写 `nullable=False` | **NULL YES** default `pending` | 实际不会写进 NULL，暂无影响 |
> | `original_content` / `processed_content` | ~~ORM 声明 `Text`（65KB）~~ 已改 `with_variant(LONGTEXT, "mysql")` | **LONGTEXT** | 漂移已消除（2026-11-15，见 5.10）：新建库和老库都是 LONGTEXT |
>
> 根因：`create_all()` **只建新表、绝不 ALTER 已有表**。这几张表早期是按本节 DDL 手工建的，后来 s2 修复只在 ORM 侧加了 `ondelete`，数据库没跟上。新库从零启动会拿到 ORM 的正确规则，老库不会。
>
> **对齐老库需要单独写迁移**（重建外键约束），属删改表结构操作，未执行——要做前先确认。
>
> **已有库迁移**：后加的列都要跑幂等脚本补，`create_all()` 不会 ALTER：
>
> ```bash
> python server/migrate_add_provider_user_id.py          # model_providers.user_id
> python server/migrate_add_provider_capabilities.py     # model_providers.capabilities
> python server/migrate_add_log_structured_output.py     # operation_logs.structured_output（阶段 1）
> ```
>
> `llm_calls` 是新表，启动时 `create_all()` 自动建，不需要脚本。新列一律允许 NULL 且无需回填：NULL 就表示"这条没有结构化清单"，前端据此不显示问题卡片。

---

## 7. API 接口表

| 方法 | 路径 | 认证 | 说明 |
|---|---|---|---|
| POST | /api/v1/auth/register | 否 | 注册 |
| POST | /api/v1/auth/login | 否 | 登录，返回 JWT |
| GET | /api/v1/providers | 是 | 提供商列表 |
| POST | /api/v1/providers | 是 | 添加提供商 |
| PUT | /api/v1/providers/:id | 是 | 更新 |
| DELETE | /api/v1/providers/:id | 是 | 删除 |
| GET | /api/v1/tasks | 是 | 任务列表 |
| POST | /api/v1/tasks | 是 | 创建 |
| PUT | /api/v1/tasks/:id | 是 | 更新 |
| DELETE | /api/v1/tasks/:id | 是 | 删除 |
| POST | /api/v1/upload | 是 | 上传 Word（返回 log_id + extracted_text） |
| POST | /api/v1/process/:id | 是 | 调 LLM 处理（pending/failed 可重试） |
| GET | /api/v1/process/:id/status | 是 | 查询处理状态（只读，返回 processed_content） |
| GET | /api/v1/download/:id | 是 | 下载 Word 结果 |
| GET | /api/v1/logs | 是 | 操作日志（分页 + 状态筛选 + 文件名搜索） |

> 阶段 0 **没有新增接口**。变化只有两处：
>
> - `providers` 的请求/响应体多了 `capabilities`（逗号分隔串）。创建时不传由服务端补 `text`，所以旧前端和 `test_e2e.py` 原样调用仍然可用。
> - `POST /process/{id}` 多一个 400 分支：任务指定的提供商能力不匹配时，在返回 processing **之前**就报错，不再等后台跑一半才 failed。
>
> 阶段 1 **同样没有新增接口**，只加了返回字段：
>
> - `GET /process/{id}/status` 多 `issues`（`GradingIssue[]`，`done` 时才给）和 `grading_degraded`。非批改任务 / 历史数据 → `issues` 为 `null`。
> - `GET /logs`、`GET /logs/{id}` 每条多 `issue_count`（列表只带条数，不带整份清单）。
> - 两个字段都是新增可选字段，旧前端不读它们也照常工作，`test_e2e.py` 未改断言仍然 33/33。

---

## 8. 部署

### 开发环境

```bash
# 后端
cd server && uv run uvicorn main:app --reload --port 8000
# 前端（Vite 代理到 8000）
cd client && npm run dev
```

### 生产环境

```yaml
# docker-compose.yml — MySQL + FastAPI
services:
  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: ${DB_PASSWORD}
      MYSQL_DATABASE: fileagent
  server:
    build: ./server
    ports: ["8000:8000"]
    depends_on: [mysql]
```

Nginx 反代：前端静态文件直接 serve，`/api/` 代理到 `127.0.0.1:8000`。uvicorn 生产用 `--workers 4` + systemd 守护。

---

## 9. 实现路线与当前状态

### 9.1 已完成基线

1. ✅ 后端骨架：FastAPI + MySQL 连接 + 建表
2. ✅ 认证模块：注册/登录 + JWT 中间件
3. ✅ 提供商 CRUD + LLM 调用服务
4. ✅ 任务 CRUD
5. ✅ Word 读写 + diff 批注服务
6. ✅ 上传 → 处理 → 下载 核心流程
7. ✅ 操作日志 API（含搜索筛选 + 重试支持）
8. ✅ 前端初始化 + 登录页 + 布局
9. ✅ 主操作页 + 任务管理 + 提供商配置 + 日志页
10. ✅ 前端状态持久化（keep-alive + sessionStorage）
11. ✅ 工作台 UX 增强：提供商可用性预检查、处理轮询、步骤导航、文本预览
12. ✅ 日志页 UX 增强：搜索筛选、失败重试、空状态提示
13. ✅ **阶段 0：LLM 传输层地基**（见 9.3）
14. ✅ **阶段 1：结构化批改输出**（见 9.4）
15. ✅ **阶段 1.5：输入格式扩到 PDF + 产品改名**（见 9.5）
16. ⬜ 联调 + Docker 部署（推迟到阶段 2.5 与 Postgres 一起做）

### 9.2 Agent 化演进路线（阶段 0–5）

学习目标是 LangGraph、RAG、Agent 调优。路线按**学习性价比**排，不按成就感排：PDF→PPT 是最重的一站，放最后当组装活。

| 阶段 | 内容 | 前置 | 状态 |
|---|---|---|---|
| **0** | LLM 传输层地基：多轮 messages、结构化输出三级降级、token/耗时/attempts 计量、分层超时 + 重试白名单、并发闸、provider 能力路由、logging handler | 无 | ✅ **已完成** 2026-11-14 |
| **1** | 批改输出结构化：自由文本 → 问题清单 JSON（位置/类型/原文/建议/置信度），前端可点击定位标注 | 0 | ✅ **已完成** 2026-11-15 |
| **1.5** | 输入格式扩到 PDF（提取层与 docx 同构）+ 项目去 Word 化改名。不在原路线里，是用户 2026-11-15 提的需求：后续真有 PDF 需求 | 1 | ✅ **已完成** 2026-11-15 |
| **2** | 评测台：golden set + rubric + LLM-as-judge + prompt 版本管理 + 回归对比 | 1 | ⬜ 这才是"Agent 调优"的本体 |
| **2.5** | Postgres 迁移：独立 `pgvector/pgvector:pg16` 容器，业务 + LangGraph 检查点 + 向量统一一库 | 2 | ⬜ 到需要时再定，阶段 0–2 用不到 |
| **3** | LangGraph 首练**长文档分块批改**：fan-out/fan-in + PostgresSaver 检查点 + 条件重试 + interrupt 人工确认。顺带突破单次上下文上限 | 2.5 | ⬜ |
| **4** | RAG 规范库：多文档上传、分块、pgvector 检索，把格式规范条款注入批改 prompt 并带引用溯源 | 3 | ⬜ |
| **5** | PDF→PPT：把确定性 PDF 几何处理移植成 service，LLM 只接管大纲规划与视觉批评修订循环 | 4 + VLM | ⬜ |

**为什么是这个顺序**：没有结构化输出（阶段 1）就没法自动打分，没有打分（阶段 2）改 prompt 就是瞎调，没有度量就无从判断 LangGraph 到底帮了什么忙。地基先行的代价是前两个阶段"看不出大变化"。

**本地视觉模型（独立支线，不阻塞主线）**：实测 RTX 4060 Laptop 空闲显存只有 5976 MiB（桌面已占 1981），所以选 **Qwen3-VL-4B**（Q4 约 3.3GB，留余量给视觉塔激活和 KV cache）；8B 约 6.2GB **放不下**。运行时 Ollama 对 `qwen3-vl` 支持最稳。部署上线后服务器没有 GPU，视觉批评必须能切云端——这就是 `capabilities` 字段存在的理由。

### 9.3 阶段 0 交付情况（2026-11-14）

改动 15 个文件，1 张新表，1 个新列，**没有新增任何依赖**。

| 文件 | 改了什么 |
|---|---|
| `services/llm.py` | 重写为传输层：`chat` / `chat_json` / `resolve_provider` / `record_llm_call` / `extract_json` / 共享 client / 并发闸 |
| `models/llm_call.py` | **新增** `llm_calls` 计量表 |
| `models/provider.py` | 加 `capabilities` 列 |
| `models/__init__.py` | 注册 `LlmCall`，否则 `create_all()` 发现不了 |
| `schemas/provider.py` | `KNOWN_CAPABILITIES` + 能力值校验与顺序归一化；三个 schema 加字段 |
| `api/v1/process.py` | 适配新签名；`_meter()` 走线程池写计量；删掉 `_default_provider_name()` |
| `api/v1/providers.py` | **未改动**——`**body.model_dump()` 让新字段自动流过 |
| `config.py` / `.env.example` | 加 6 个配置项 |
| `main.py` | 补 logging handler；lifespan 预热与关闭共享 client |
| `migrate_add_provider_capabilities.py` | **新增**幂等迁移脚本 |
| `tests/test_llm.py` | **新增** 50 个用例 |
| `client/src/api/types.ts`、`views/Providers.vue` | 能力多选框 + 表格列 + 类型 |

**实测发现并当场修掉的两个真问题**（都是写测试/跑 e2e 时才暴露的，看代码看不出来）：

1. `chat_json` 第一版把"输出解析失败"也当成降级信号，会为一句话连打三次完整请求 → 改成只在端点明确拒绝 `response_format` 时降档，解析失败就地返回。（阶段 1 进一步把"抛错"改为返回 `(result, None)`，理由见 §5.7）
2. 失败路径的 `attempts` 永远是 1，计量表看不出"重试到耗尽"和"一次就挂"的区别 → 给 `LLMError` 挂上 `attempts`，失败也带次数上报。e2e 复跑确认库里从 `1` 变成 `3`。

**验证结果**（全部实测，非推断）：

| 门禁 | 结果 |
|---|---|
| `uv run pytest -q` | **62 passed**（阶段 0 前 12） |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 35 files already formatted |
| `uv lock --check` | Resolved 46 packages（无新增依赖） |
| `npm run build` | 通过（vue-tsc + vite） |
| 迁移脚本 | 首次 `[done]`，重复运行 `[skip]`，幂等成立 |
| `uv run python test_e2e.py` | **33/33 通过 ×2 轮**，连真实 MySQL 8 |
| 后端日志 | 0 条 traceback / exception / warning |
| 计量落库 | 失败行 `status=failed`、`attempts=3`、`provider_id` 被 SET NULL 而 `provider_name` 快照保留 |
| `llm_calls` 建表 | 由 `create_all()` 自动建，15 列 |
| 测试数据 | 已还原：users 1 / providers 2 / tasks 5 / logs 13 / uploads 7 全部 MATCH，`llm_calls` 清空 |

**遗留**：`config.py` 用旧式 `class Config`，Pydantic 报了 `PydanticDeprecatedSince20` 警告（阶段 0 前就有，与本次无关，未顺手改）。

### 9.4 阶段 1 交付情况（2026-11-15）

改动 16 个文件，1 个新列，**仍然没有新增任何依赖，也没有新增任何配置项**。

| 文件 | 改了什么 |
|---|---|
| `schemas/grading.py` | **新增**：`GradingIssue` / `GradingResult` 契约、`GRADING_JSON_SCHEMA`（strict）、`normalize_type()` + 中英别名表 |
| `services/grading.py` | **新增**：system 输出契约、解析降级、原文定位、序列化/反序列化 |
| `services/llm.py` | `chat_json` 解析失败改为返回 `(result, None)` 而不是抛错 |
| `api/v1/process.py` | `_run_model()` 按 `is_grading` 分流；`_meter()` 加 `purpose` 参数（`chat` / `grading` / `grading_degraded`） |
| `models/operation_log.py` | 加 `structured_output` 列 |
| `migrate_add_log_structured_output.py` | **新增**幂等迁移脚本 |
| `schemas/process.py` / `schemas/operation_log.py` | `ProcessResponse` 加 `issues` + `grading_degraded`；`LogResponse` 加 `issue_count` |
| `api/v1/logs.py` | `_to_response()` 解析出条数（列表不带整份清单） |
| `tests/test_grading.py` | **新增** 34 个用例 |
| `tests/test_llm.py` | 同步 `chat_json` 新语义 |
| `test_llm_server.py` | **新增**假 OpenAI 端点（刻意不支持 `response_format`） |
| `test_e2e_structural.py` | **新增**结构化端到端脚本，34 项断言 |
| `client/src/api/types.ts` | `Capability`、`GradingIssue`、`ProcessResponse.issues`、`LogEntry.issue_count` |
| `client/src/stores/home.ts` | `gradingIssues` / `gradingDegraded` 持久化 |
| `client/src/views/Home.vue` | 问题清单卡片 + 原文段落高亮 + 双向定位（点卡片滚到高亮，点高亮亮卡片）；`displayTextParas` 纯函数负责分段与套 span |

**关键取舍**：坐标由后端算，不由模型给（见 §5.8）。解析失败一律降级为自由文本，不让用户的文档判 failed。

**验证结果**（全部实测）：

| 门禁 | 结果 |
|---|---|
| `uv run pytest -q` | **90 passed**（阶段 0 后 62，+28） |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 41 files already formatted |
| `uv lock --check` | Resolved 46 packages（无新增依赖） |
| `npm run build` | 通过（vue-tsc + vite，exit 0） |
| `migrate_add_log_structured_output.py` | 首次 `[done]`、重复 `[skip]`，幂等成立 |
| `test_e2e_structural.py` | **34/34 通过**，零真实 token |
| `test_e2e.py`（回归） | **33/33 通过**，阶段 0/1 未破坏原有链路 |
| LLM 拒绝路径 | mock 端点返回 2 次 400（`json_schema` / `json_object`），`chat_json` 成功降级到不带格式的一档拿到结果 |
| 后端/mock 日志 | 0 条 traceback / exception / warning / error |
| 测试数据还原 | users 1 / providers 2 / tasks 5 / logs 13 / llm_calls 0 / uploads 7 六项全部 MATCH，`RESTORED: True` |

**一个实测发现的环境事实**：本机 **8000 端口被 WSL 侧另一个项目的 `backend.app:app` 占着**，而且因为 WSL 是 mirrored 网络，Windows 的 `netstat` 看不到这个监听，只表现为 `uvicorn --port 8000` 报 `[Errno 10048]` 而 `curl localhost:8000` 能收到对方 FastAPI 的 404。排查这类"端口没人监听但连得上"的情况，要去 `wsl -e bash -lc "ss -ltnp | grep ':端口'"` 查，别在 Windows 侧白费劲。`test_e2e_structural.py` 因此支持 `FA_API` / `FA_MOCK_LLM` 换端口。

**遗留**：
- `chat_json` 的 `json_schema` 严格档至今只在 mock 端点上验证过降级路径，**还没拿真实国产端点跑通过一次严格模式**。真实端点是否支持 `strict: True`、字段是否有丢，需要在下一步用你配的豆包/qwen 实测。
- 结构化清单目前只在批改任务生效（`is_grading`），普通润色/改写任务不产出清单。
- docx 下载仍是纯红字 diff，**没有**把问题清单汇总进交付文档——那是独立的排版工作，未混进本阶段。
- demo 副本仍落后阶段 0 + 1 两轮。

---

## 10. 缺陷修复总结

### 已修复的高优先级问题

| ID | 问题 | 修复方案 | 验证 |
|---|---|---|---|
| h1 | 工作台选任务时未预检查模型提供商可用性 | Step 0 添加 `activeProviderCount` computed，为 0 时显示 `el-alert` 警告 | 刷新任务列表后显示警告 |
| h2 | 处理中浏览器刷新后无轮询机制 | `startPolling()` + `onActivated` 恢复状态查询，超时 10 分钟 | 刷新页面自动恢复处理进度 |

### 已修复的中优先级问题

| ID | 问题 | 修复方案 | 验证 |
|---|---|---|---|
| m1 | 工作台步骤导航不灵活（缺上一步按钮） | Step 1/2 添加「上一步」按钮，Step 3 添加「处理新文件」 | 导航流畅 |
| m2 | 日志页面无搜索和筛选功能 | 后端 `/logs` 接口添加 `status`/`filename` 参数，前端 `el-select` + `el-input` | 筛选生效 |
| m3 | 日志页面无法重试失败的处理 | `failed` 状态行显示「重试」按钮，调用 `POST /process/{id}` | 确认后执行 |
| m4 | 上传后无法预览提取的文本 | 后端 `UploadResponse` 返回 `extracted_text`，前端 Step 2 用 `el-collapse` 展示 | 点击展开 |
| m5 | 处理结果无法在线预览 | 后端 `ProcessResponse` 返回 `processed_content`，前端 Step 3 用 `el-collapse` 展示 | 点击展开 |

### 已修复的低优先级问题

| ID | 问题 | 修复方案 | 验证 |
|---|---|---|---|
| l1 | 操作日志页面样式问题 | 添加搜索栏样式、空状态样式 | 界面整洁 |
| l2 | 工作台没有空状态 | Step 0 当 `tasks.length === 0` 显示「还没有任务」提示 | 显示正确 |
| l3 | 任务/提供商删除无确认 | `remove()` 函数添加 `ElMessageBox.confirm` 确认框 | 二次确认 |
| l4 | 下载按钮文案/样式 | Step 3 下载按钮文案「下载结果 .docx」，加 SVG 图标 | 按钮正确 |
| l5 | 任务描述截断 | `el-table-column` 添加 `show-overflow-tooltip` | 悬停显示完整 |

### 测试验证

- 前端 `vue-tsc --noEmit` 类型检查：通过
- 前端 `npm run build` 构建：通过
- 后端模块导入检查：通过

### 第二轮修复（2026-09-22）

代码审查发现的安全/正确性问题，已全部修复并回归验证。

| ID | 优先级 | 问题 | 修复方案 |
|---|---|---|---|
| s1 | 高 | provider 全局共享，任何用户可读/改/删他人 API Key（越权） | 加 `user_id` 列，CRUD 全部按用户隔离；响应打码 api_key；越权返回 404 |
| s2 | 高 | 删除被引用的 provider/task 触发外键约束 → 500 | 模型 FK 加 `ON DELETE SET NULL`；应用层删除前主动置空引用 |
| s3 | 中 | 处理在请求内 await，客户端断连后日志永久卡 processing | 改 `BackgroundTasks` + 独立 session 异步处理；接口立即返回 processing；前端总是轮询 |
| s4 | 中 | 进程重启丢失后台任务，遗留 processing 无法恢复 | 启动时把僵尸 processing 重置为 failed（单 worker 适用） |
| s5 | 中 | task 指定的 provider 被删后静默 fallback 到别的模型 | 处理前校验 provider 存在且归属当前用户，否则显式报错 |
| s6 | 中 | `db.query(M).get(id)` 在 SQLAlchemy 2.0 已弃用 | 改为 `db.get(M, id)` |
| s7 | 低 | download 的 Content-Disposition 头重复赋值 | 合并为一次赋值 |
| s8 | 低 | 启动用 print 输出中文，Windows GBK 控制台乱码 | 改用 logging |
| s9 | 低 | 缺单元测试 | 新增 `server/tests/`（diff + word 纯函数 12 个用例）；pyproject 配 pytest pythonpath |
| s10 | 中 | `CORS_ORIGINS` 是死配置：`main.py` 用 `os.getenv` 读，但 pydantic-settings 只把 `.env` 喂给 Settings 对象、不写入 `os.environ`，所以 `.env` 里的值永远读不到 | `Settings` 增加 `cors_origins` 字段，`main.py` 改用 `get_settings().cors_origins`。实测：`.env` 里加的源能通过预检，未列出的源仍被拒 400 |

**回归验证**：pytest 12 passed；后端端到端冒烟 14/14 PASS（隔离/越权 404/删除不 500/后台处理流转）；前端 `vue-tsc --noEmit` 0 错误。

### 第三轮：阶段 0 LLM 传输层地基（2026-11-14）

| ID | 优先级 | 问题 | 修复方案 |
|---|---|---|---|
| d1 | 高 | 项目从未配置 logging handler，root 停在 WARNING，`logger.info` 全部静默丢弃——重试、并发、计量一概看不见，"调优"没有观测面 | `main.py` 加 `_setup_logging()`：`basicConfig(force=True)` + stdout handler，级别由 `LOG_LEVEL` 控制。`force=True` 是必须的，否则 `--reload` 反复重导入会让每行打印两遍。实测启动日志已正常输出 |
| d2 | 中 | `call_llm` 只能单轮纯文本：`messages` 硬编码一条 user，无 system、无多轮、无结构化输出、无 token 计量、无重试 | 拆成 `chat()`（纯传输，可用假 client 单测）+ `resolve_provider()` + `record_llm_call()`，另加 `chat_json()` 和 `extract_json()` |
| d3 | 中 | 每次调用都新建 `httpx.AsyncClient`，DNS 与 TLS 握手不复用；`timeout=120` 写死在代码里 | 模块级共享 client，lifespan 预热与关闭；超时拆成 connect / read 两层，单次调用可覆盖读取层 |
| d4 | 低 | 并发无上限，本地小模型或云端限流会被打爆 | `asyncio.Semaphore(llm_max_concurrency)`。⚠ 单 worker 假设，多 worker 时每进程各一个 |
| d5 | 低 | provider 只有一个 `model_name`，无法区分文本 / 视觉 / 向量用途，后面接 VLM 和 embedding 要改两次表 | 加 `capabilities` 列 + 入口校验 + `resolve_provider` 能力路由；用途不匹配显式报错，不静默换模型 |

**过程中实测发现并修掉的两处**（详见 9.3）：`chat_json` 误把输出解析失败当成降级信号，会为一句话连打三次完整请求；失败路径的 `attempts` 永远记成 1，看不出"重试到耗尽"和"一次就挂"。

**顺带查明的库结构漂移**（详见 §6 实测对照表）：`tasks.provider_id` 与 `operation_logs.task_id` 在本机真实库里是 `NO ACTION`，并非 ORM 声明的 `SET NULL`——`create_all()` 永不 ALTER 已有表所致。现在靠应用层删除前先置空引用兜着，**新加删除路径必须照抄这一步**（AGENTS.md §3.2）。

**回归验证**：pytest 62 passed；端到端 33/33 两轮（连真实 MySQL）；`npm run build` 通过；ruff check / format 全绿；`uv lock --check` 一致且无新增依赖；迁移脚本幂等；测试数据已按快照精确还原。

### 第四轮：阶段 1 结构化批改输出（2026-11-15）

| ID | 优先级 | 问题 | 修复方案 |
|---|---|---|---|
| g1 | 高 | 批改只产出一整篇修订文本，问题埋在哪、改了什么无从定位，前端只能显示红字 diff，也没法自动打分（阶段 2 评测台的前置缺口） | 批改任务改为要 `revised_text` + `issues` 清单（类型/原文/建议/原因/置信度），存进 `operation_logs.structured_output`；前端出问题卡片并与原文高亮双向定位 |
| g2 | 高 | 若让模型输出 `paragraph_index` / `char_offset`，LLM 不数字符会大面积错位，且错位无任何信号，批注指到错位置比不指更糟 | 模型只逐字摘原文片段，`start/end` 由后端 `_find_unused_span` 精确匹配；匹配不到留 `None` 显示"未定位"，绝不猜 |
| g3 | 中 | 模型不守 JSON 格式（自由文本、缺 `revised_text`、围栏包裹）时若判 failed，用户的文档就拿不到任何结果 | `parse_grading_output` 一律降级为自由文本 + `degraded` 标记，diff 下载行为与阶段 0 完全一致 |
| g4 | 中 | `chat_json` 解析失败抛 `LLMError`，调用方拿不到原始内容就无法降级 | 语义改为返回 `(LLMResult, Any \| None)`：解析失败是预期结果而不是异常，交调用方兜 |
| g5 | 低 | "批改没发现问题"和"没能结构化所以没清单"都显示 0 条，等于撒谎 | `ProcessResponse.grading_degraded` 单独暴露，前端据此出 `el-alert` 警告 |
| g6 | 低 | 类型名由模型自由发挥（中文/"拼写"/"前后不一致"），硬枚举会整片报错 | `normalize_type()` 中英别名归一到 8 个 canonical 值，认不出归 `other`，单条不合契约只丢那一条 |

**环境侧实测发现**：本机 8000 端口被 WSL 内另一项目的 `backend.app:app` 占用。因 WSL 走 mirrored 网络，Windows `netstat` 查不到该监听，现象是 `uvicorn --port 8000` 报 `[Errno 10048]` 但 `curl localhost:8000` 能收到对方 FastAPI 的 404。查法：`wsl -e bash -lc "ss -ltnp | grep ':8000'"`。据此给 `test_e2e_structural.py` 加了 `FA_API` / `FA_MOCK_LLM` 端口覆盖。

**回归验证**：pytest 90 passed；`test_e2e_structural.py` 34/34（假端点，零 token）；`test_e2e.py` 33/33 无回归；`npm run build` 通过；ruff check / format 全绿；`uv lock --check` 一致且无新增依赖；迁移脚本幂等；测试数据六项计数全部还原 MATCH。

**仍未验证**：`json_schema` 严格档只在 mock 端点验证过降级路径，没拿真实国产端点跑过一次严格模式。

### 9.5 阶段 1.5 交付情况（2026-11-15）：PDF 输入 + 去 Word 化

改动 12 个文件，新增 1 个 service，**首次引入运行时依赖**（pymupdf，锁 46 → 56 包）。

| 文件 | 改了什么 |
|---|---|
| `services/pdf.py` | 新增：两遍段落重建 + 页码/跨页噪声剔除 + 三条失败路径（`PdfError`） |
| `api/v1/process.py` | `TEXT_EXTRACTORS` 派发表（白名单由它派生）；下载文件名改 `stem + .docx`；文案去 Word 化 |
| `models/operation_log.py` | 正文两列 `Text().with_variant(LONGTEXT, "mysql")`，与本机老库对齐 |
| `tests/test_pdf.py` | 新增 20 例：拼接/几何合并/噪声/三条错误路径 + 3 例合成 PDF 端到端 |
| `test_e2e_structural.py` | 新增第 8 节 PDF 链路（6 项）：提取与 docx 同构、定位、结果名换成 `.docx` |
| `client/views/Home.vue`、`api/process.ts` | `accept=".docx,.pdf"` + 上传区文案 |
| `AGENTS.md` / `README.md` / `SPEC.md` | 定位与格式口径同步；README 顺手修掉两处陈旧计数（表数、用例数） |

**验证到什么程度**（都是实测）：

- `pytest` 110 passed（+20）；`test_e2e_structural.py` **40/40**（+6）；`test_e2e.py` 33/33 无回归；ruff check / format 全绿；`npm run build` 通过。
- **真机跑通**（用户授权"能调用到 agent 就可以跑测试"）：合成中文 PDF → provider 5（qwen3.7-flash）→ done、`degraded=False`、4 条 的/地/得 意见全部精确定位（`start=9 end=16` 手算核对），下载名 `批改_校园随笔.docx`。用合成文本而非本机文档，避免私人内容外发。跑完按 `task_name` 清掉日志与计量行。
- 提取质量核对：3 段各由 2 个视觉块拼回、页脚 `3` 被位置规则剔除、无跨栏误合。

**这一轮发现的真缺陷（不是新功能带来的麻烦）**：下载文件名以前是 `批改_ + original_filename`，PDF 输入会产出一个叫 `.pdf` 的 docx，下载方直接打不开。已改成去扩展再补 `.docx`，docx 输入的结果名不变。

**遗留与代价**：

1. 真机那次跑回来的 `revised_text` 与原文一模一样（4 条意见都对，但修订文没落地修改）——批改 prompt 的产出质量问题，正是阶段 2 评测台要量化的东西，这轮不顺手改 prompt。
2. `chat_json` 用到了阶梯的哪一档**没有任何记录**：降级路径是静默的，端点接受 `json_schema` 还是被降到的，事后无法区分。跨端点比较（阶段 2）必须有这个归因，届时给 `llm_calls` 加一列。
3. pymupdf 的依赖树偏重（见 5.10），demo 交付副本的 `requirements.txt` 要跟着补，属交付决定。
4. 扫描版 PDF（无文字层）现在只报错，OCR 属阶段 5。

---

## 11. 环境变量 (.env)

```
DATABASE_URL=mysql+pymysql://root:password@localhost:3306/file_agent
JWT_SECRET_KEY=your-secret-key-change-me
JWT_EXPIRE_HOURS=2
UPLOAD_DIR=uploads
OUTPUT_DIR=outputs
CORS_ORIGINS=http://localhost:5173

# LLM 传输层（阶段 0）
LLM_TIMEOUT_CONNECT=10
LLM_TIMEOUT_READ=120
LLM_MAX_RETRIES=2
LLM_MAX_CONCURRENCY=4
LLM_TRUST_ENV_PROXY=true

LOG_LEVEL=INFO
```

> `CORS_ORIGINS` 必须在 `config.py` 的 `Settings` 里声明成字段才能生效：pydantic-settings 只把 `.env` 的值注入 Settings 对象，**不会写入 `os.environ`**，因此 `os.getenv("CORS_ORIGINS")` 永远读不到 `.env` 里的配置（这是 s10 的成因）。多个源用逗号分隔。**新增任何配置项都要先在 `Settings` 里加字段，再同步进 `.env.example`**，两条都是硬约定。
>
> **LLM 相关取值口径**：`LLM_TIMEOUT_CONNECT` 只管建连，`LLM_TIMEOUT_READ` 等响应收完（长文档调大这一个）；`LLM_MAX_RETRIES` 只对网络错误 / 超时 / 429 / 5xx 生效；`LLM_MAX_CONCURRENCY` 是进程内上限；`LLM_TRUST_ENV_PROXY=false` 用来绕开 `NO_PROXY` 含非法条目（如裸 `[::1]`）时 httpx 构造即报 `InvalidURL: Invalid port` 的问题——需要走系统代理访问境外端点时**不要关它**。
>
> `JWT_EXPIRE_HOURS` 文档口径是 2（也是 `config.py` 的默认值），本机 `.env` 可能为了开发方便设成 24。`.env` 不入库，以本机为准。


---

## 12. 前端状态管理与 UX 设计

### 12.1 状态持久化（Pinia + sessionStorage）

**问题**：用户在工作台点击"开始处理"后切换到其他页面或刷新浏览器，前端 UI 状态丢失，但后端处理仍在继续。

**方案**：用 Pinia store + sessionStorage 双层持久化：

| 层级 | 职责 | 实现 |
|---|---|---|
| ref（组件内） | 响应式 UI 状态 | `currentStep`, `processing`, `uploadResult` 等 |
| Pinia store | 状态集中管理 + 自动保存 | 标准 `ref` + `watch` 模式，任一状态变化自动 `sessionStorage.setItem` |
| sessionStorage | 浏览器级持久化 | 刷新后 `restore()` 恢复，关闭标签页清除 |

**关键文件**：`client/src/stores/home.ts` — 每个状态字段都是独立 `ref`，通过 `watch(..., { deep: true })` 监听全部字段，变化即触发持久化；`restore()` 逐字段恢复（缺省回退初始值）。

> 注意：不要使用「对象字面量 getter/setter 代理闭包变量」的写法——Pinia 会把 setup store 返回值中的访问器转换为普通数据属性，导致 `restore()` 更新的闭包变量对组件不可见、setter 也不再触发保存。这是实测踩过的坑。

**keep-alive 配合**：`<router-view>` 用 `<keep-alive>` 包裹，组件切回时触发 `onActivated` 而非 `onMounted`。各页面在 `onActivated` 中刷新数据（任务列表、提供商列表、处理状态），保证数据不过期。

### 12.2 处理轮询机制

**场景**：LLM 处理可能需要十几秒，期间如果 axios 超时或浏览器刷新，前端会失去处理状态。

**设计**：

```
doProcess() 超时/异常
  → 保持 processing=true
  → startPolling(): setInterval 每 5s 调 GET /process/{id}/status
  → status=done → stopPolling + 推进到下载步骤
  → status=failed → stopPolling + 显示错误
  → status=processing → 继续轮询
  → 超过 120 次（10 分钟）→ stopPolling + 提示超时
```

组件 `onDeactivated` 时暂停轮询，`onActivated` 时通过 `refreshProcessingStatus()` 恢复。

### 12.3 提供商可用性预检查

选任务步骤加载提供商列表后，用 `computed` 计算 `activeProviderCount`。如果为 0，显示 `el-alert` 警告并引导用户去提供商配置页。

### 12.4 文本预览

| 阶段 | 预览内容 | 数据来源 |
|---|---|---|
| 上传后 | 提取的原文文本 | `UploadResponse.extracted_text` |
| 处理后 | LLM 处理结果 | `ProcessResponse.processed_content` |

均用 `el-collapse` 折叠面板实现，默认折叠，点击展开。`.text-preview` 样式限制最大高度 240px + 滚动条。

### 12.5 日志页搜索与重试

- **搜索栏**：状态下拉（el-select）+ 文件名输入（el-input）+ 搜索/重置按钮，回车搜索
- **重试**：`failed` 状态的行显示"重试"按钮，调用 `POST /process/{id}`，确认后执行
- **空状态**：`el-table #empty` 插槽 + `el-empty` 组件，区分"有筛选无结果"和"完全无记录"两种场景

---

> **上手方式**：按第 4 节先把空项目跑通（后端 `health` + 前端页面），再按第 9 节顺序逐一实现模块。知识点在做的过程中查，不要提前啃完文档。