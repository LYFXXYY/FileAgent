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

**依赖代价（2026-11-16 已减负）**：PyMuPDF 2.1 把版面分析（`pymupdf-layout` → onnxruntime + numpy + protobuf + flatbuffers）和 Markdown 导出（`pymupdf4llm` → networkx + tabulate）做成硬依赖，装上后 venv 一度到 337M / 84 包。但 `services/pdf.py` 只用 `page.get_text("blocks")`，对这些**零引用**，于是用 `pyproject.toml` 的 `[tool.uv] override-dependencies`（永不成立的 environment marker）把整条链摘掉：**75 包 / 233M**，`import pymupdf`、块提取、142 项测试全部照过。阶段 5 真要用版面分析时删掉那几行就装回来——别把它当成已经付过的成本。要换 pypdf 省得更多，但届时还得再引一次 PyMuPDF，变成两套 PDF 库并存，不值。

> 顺带修正：`pyproject.toml` 的 `description` 还写着"Word 文档 AI 处理平台"，已改为"文档 AI 处理平台"；`pytest` 原本同时出现在 `project.dependencies` 和 dev 组里，已从生产依赖里摘掉（部署时不该装 pytest）。

> 顺带修正了 ORM 与真实库的一处漂移：`operation_logs.original_content / processed_content` 改为 `Text().with_variant(LONGTEXT, "mysql")`。MySQL 的 `TEXT` 是 64KB **字节**上限，中文按 3 字节算两万字就撞墙，而 PDF 输入让长文档变成常态；本机老库早就是 LONGTEXT，这样声明让新建库和它一致（SQLite 单测仍走 TEXT）。`structured_output` 保持 TEXT——它只装问题清单，不装全文。

---

### 5.11 评测台最小版（阶段 2 的第一块砖）

用户口径是"不用测那么全面，简单测试就可以下一步"，所以这一轮只做**能当 CI 门禁的确定性指标**，不做排行榜、不做成本价格表、不做人工标注平台。

| 文件 | 职责 |
|---|---|
| `eval/cases.py` | 5 份合成夹具（的/地/得、成语错字、标点缺失、结构逻辑、干净文档）。答案由构造决定，零隐私零标注成本 |
| `eval/score.py` | 确定性打分 + 聚合 + 两轮对拍。纯函数，10 条单测直接测它 |
| `eval/runner.py` | CLI。读一行 provider、跑批改、落 `eval/runs/<时间戳>-<id>.json` |

**四个指标**（`None` 表示该夹具不参与，聚合时不进分母，绝不当 0 平均）：

| 指标 | 判法 | 为什么必须是它 |
|---|---|---|
| `locate_rate` | 有 `start` 的意见占比 | 定位失效=前端卡片点不动，用户看不到批注 |
| `recall` | 期望错处被任意意见的 `original` 覆盖 | 漏检是最贵的失效 |
| `fix_rate` | 修订稿里**正确写法出现且错误写法消失**，两者同时成立 | 见下 |
| `type_coverage` / `false_positives` | 类型期望按**组**写，组内任一命中即覆盖；超出 `max_issues` 的条数 | 硬挑刺同样是缺陷，所以"干净文档"也是一条夹具 |

`fix_rate` 的双条件不能省：只查"正确写法在不在"会被原文里本来就有的同形词蒙过去；只查"错误写法没了"会被"整段删掉"蒙过去。这个指标就是为阶段 1.5 那次实测发现写的——真机一轮意见 6 条全对、`revised_text` 却与原文一字不差，只看 `degraded` 和条数完全发现不了。

**档位归因（补上 §9.4 留的缺口）**：`LLMResult.format_tier` 记录 `chat_json` 实际落在 `json_schema` / `json_object` / `plain` 哪一档，拒绝降档时打 INFO 日志。**没有落库**（那要加列，属改表结构，做前先问），但 run JSON 与后端日志都能读到了。

**裁判（`--judge`）是旁路**：判分失败只记 `error`，绝不带走已跑完的客观指标——和 `record_llm_call()` 同一个原则，观测手段不能变成故障源。

**跑法**（`server` 目录下，会真花钱）：

```bash
uv run python -m eval.runner --provider 5 --case clean     # 先单条验通链路
uv run python -m eval.runner --provider 5 --label baseline-1
uv run python -m eval.runner --provider 5 --prompt-file cand.txt --baseline eval/runs/xxx.json
```

`--prompt-file` 整体替换批改指令（必须含 `{{content}}`），run JSON 记 `prompt_digest`，这就是最简版提示词版本管理：A/B 靠两轮数字对拍，不靠"感觉变好了"。

---

### 5.12 长文档分块批改图（阶段 3：LangGraph 首练）

只有**批改类**任务、且整篇长度超过 `LONGDOC_CHUNK_CHARS`（默认 6000 字）时才进图：

```
START → plan → (Send 扇出) → grade_chunk × N → merge → END
```

| 节点 | 干什么 | 花不花钱 |
|---|---|---|
| `plan` | 调 `services/chunking.split_document` 算块边界 | 否 |
| `grade_chunk` | 先查按块缓存，没命中才 `chat_json` 批这一块，并把块内偏移平移成整篇偏移 | 每块一次 |
| `merge` | 按块序号排序、`reassemble` 拼回整篇、展平意见清单、汇总 degraded | 否 |

**为什么只有批改类分块**：摘要、翻译、润色的产出要对**整篇**负责，逐块做完再拼只会得到 N 份局部摘要，比一次性塞进去更差。真要处理超长摘要得走"先逐块摘要再整篇汇总"的两段式，那是另一个功能，不在这里顺手做。

**分块的三条不变式**（`services/chunking.py`，改它必跑 `tests/test_chunking.py`）：

1. `chunk.text == source[chunk.start:chunk.end]` —— 块就是原文的连续切片，不重排不加空格
2. 块不重叠，块之间只允许隔着空白（段落分隔与空行）
3. `reassemble(source, chunks, [c.text for c in chunks]) == source` —— 逐字拼得回去

切点优先段落边界，段落本身超预算退到句末标点，再超才硬切。代价如实说：跨切点的片段
会匹配失败留 `None`（显示"未定位"），**不会**指到别处去——这跟 §5.8"绝不要求模型输出
坐标"是同一条纪律。硬切时的贪心是"预算内尽量靠后取句点"，取第一个超预算的句点是错的
（切出来的块本身还会超预算，第一版就这么错过）。

**省钱的根据是按块 store，不是 checkpoint**。检查点按 super-step 落盘，扇出里有一块抛异常，
整步的写入一起作废——实测重跑时三块全都重打。所以：

| 机制 | 管什么 | 不管什么 |
|---|---|---|
| `AsyncSqliteSaver`（`checkpoints/grading.sqlite3`） | 进程在两步之间没了，下次从那步之后接着跑 | 同一步内的部分成功 |
| `SqliteStore`（`checkpoints/grading.chunks.sqlite3`） | 每块成功后立刻落盘；重跑时键 `(thread_id, index)` 命中就不外呼 | — |

缓存命中还要求**块文本、批改指令摘要、模型名**三者一致：换 prompt 或换提供商还在吃旧结果，
那种"看起来变好了"最难查。命中缓存的块**不写 `llm_calls`**——本轮没发生外呼，记账就是重复计成本。

**reducer 不能用 `operator.add`**：同一 thread 的历次运行会堆在一起，用户点第二次"处理"时 3 块变
6 段，`reassemble` 直接报"块数与段数不一致"。这里用的是按块序号覆盖的 upsert reducer。
追加语义是给聊天式线程用的，"重跑一条日志"要的是这次的结果就是最终结果。

**为什么另建一个 `graphs/` 包**：图节点是编排，`services/` 是能力。混在一起，节点里偷偷查库
就没人拦得住，而 §5.7 那条"传输层不碰 DB"的边界正是靠这种分层守住的——provider 由调用方
解析后用闭包传进节点，状态里只留可序列化内容（塞 ORM 对象的话 msgpack 检查点写不下去）。

**这一轮刻意没做**：图级 `RetryPolicy`（`chat()` 已按白名单重试，再套一层就是双倍烧钱）、
`interrupt` 人工确认分块方案（要先有 UI，别为了用上 API 制造一次交互）、非批改类的分段汇总。

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
> **（MySQL 老库时代的说明，已随阶段 4 退役）** 当时"对齐老库"与"补列"靠三个幂等脚本（`migrate_add_provider_user_id.py` / `_capabilities.py` / `migrate_add_log_structured_output.py`）。Postgres 新库 `create_all()` 一次建齐、不需要它们，三件套已于 2026-11-17 `git rm`（历史可从 git 找回）。**今后再要改列**：写 Postgres 方言的一次性幂等脚本（先查 `information_schema.columns` 再 `ALTER TABLE`），范例 `migrate_add_task_output_format.py` / `migrate_add_admin_support.py`。
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
15. ✅ **阶段 1.5：输入格式扩到 PDF + 项目去 Word 化改名**（见 9.5）
16. ✅ **阶段 2 最小版：评测台**（见 9.6；完整形态的真实夹具留到需要时再补）
17. ✅ **阶段 3 主体：长文档分块批改图（LangGraph）**（见 9.7）
18. ✅ **依赖与产物减肥**：pymupdf 死重摘除 + Element Plus 按需引入（见 9.8）
19. ⬜ 联调 + Docker 部署（推迟到阶段 4 与 Postgres 一起做）

### 9.2 Agent 化演进路线（阶段 0–6）

学习目标是 LangGraph、RAG、Agent 调优。路线按**学习性价比**排，不按成就感排：PDF→PPT 是最重的一站，放最后当组装活。**2026-11-16 用户重新拆分阶段 4/5/6**：原来一锅炖的 RAG 拆成"先建基建（Postgres + 文档入库）"和"后接向量"，中间夹进 PDF→PPT——避免被 embedding 选型卡住交付节奏，也让 LangGraph 图编排练手期紧接阶段 5 复用。

| 阶段 | 内容 | 前置 | 状态 |
|---|---|---|---|
| **0** | LLM 传输层地基：多轮 messages、结构化输出三级降级、token/耗时/attempts 计量、分层超时 + 重试白名单、并发闸、provider 能力路由、logging handler | 无 | ✅ **已完成** 2026-11-14 |
| **1** | 批改输出结构化：自由文本 → 问题清单 JSON（位置/类型/原文/建议/置信度），前端可点击定位标注 | 0 | ✅ **已完成** 2026-11-15 |
| **1.5** | 输入格式扩到 PDF（提取层与 docx 同构）+ 项目去 Word 化改名。不在原路线里，是用户 2026-11-15 提的需求：后续真有 PDF 需求 | 1 | ✅ **已完成** 2026-11-15 |
| **2** | 评测台：golden set + rubric + LLM-as-judge + prompt 版本管理 + 回归对比 | 1.5 | 🟡 **最小版已完成** 2026-11-15（合成夹具 + 确定性指标 + `--prompt-file` A/B + 同源自判）。缺：真实文档夹具、第二厂商当裁判、档位落库、重复轮次 |
| **3** | LangGraph 首练**长文档分块批改**：fan-out/fan-in + 检查点 + 按块幂等续跑。顺带突破单次上下文上限 | 2 | 🟡 **主体已完成** 2026-11-16。缺 `interrupt` 人工确认分块方案（要先做 UI） |
| **4** | **Postgres 基建 + 规范文档入库**：业务表全量迁 Postgres（pgvector 镜像，MySQL 退役）、LangGraph 检查点从 SQLite 迁到 Postgres、规范文档上传与分块存表。**不做检索**——把存储和图状态这一层做扎实。 | 3 | ✅ **2026-11-16 全部完成**：业务表 + LangGraph saver/store + spec_docs/spec_chunks + e2e 双跑（31/0 + 52/0） |
| **5** | PDF→PPT：把确定性 PDF 几何处理移植成 service，LLM 只接管大纲规划与视觉批评修订循环 | 4 + VLM | ✅ **2026-11-17 全部 8 切片交付**：真渲染走 WSL LibreOffice（双后端 stub/soffice），e2e_ppt 28/0，详见 §9.13 |
| **6** | **向量能力接入**：本地 embedding 模型（RTX 4060 跑 bge-m3 或 bge-large-zh）+ pgvector 检索 + 规范条款注入批改 prompt 并带引用溯源 | 5 | ⬜ |

**为什么 2.5（Postgres）从"并入 4"变成"就是 4 的第一步"**：原计划"等 RAG 真需要 pgvector 时再迁"听起来节省，但把 Postgres 和 RAG 捆一起交付意味着 RAG 任何选型问题（embedding 来源、检索精度）都会拖着 Postgres 落地。拆开后 Postgres 是纯基建活、风险可控；RAG 的向量部分独立成阶段 6，可以慢慢挑模型。

**为什么阶段 6 排在 PDF→PPT 后**：阶段 5 不依赖向量检索（PPT 大纲靠 LLM 自己规划、视觉批评靠 VLM），但**强依赖 LangGraph 图编排**——这正好是阶段 3 的主产出，趁热做。把向量这一层推到 5 之后还有个好处：可以拿阶段 5 真实产生的多文档需求来反推 embedding 选型，而不是空对空猜。

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
- `chat_json` 的 `json_schema` 严格档至今只在 mock 端点上验证过降级路径，**还没拿真实国产端点跑通过一次严格模式**。真实端点是否支持 `strict: True`、字段是否有丢，需要在下一步用你配的豆包/qwen 实测。（后续已补：阶段 1.5 与阶段 2 的真实端点实测都命中 `json_schema` 档，见 9.5 / 9.6。）
- 结构化清单目前只在批改任务生效（`is_grading`），普通润色/改写任务不产出清单。
- docx 下载仍是纯红字 diff，**没有**把问题清单汇总进交付文档——那是独立的排版工作，未混进本阶段。
- demo 副本仍落后阶段 0 + 1 两轮。

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

### 9.6 阶段 2 最小版交付情况（2026-11-15）：评测台

改动 4 个文件 + 1 处 `LLMResult` 字段扩展，**没有新增依赖、没有改表结构**。

| 文件 | 改了什么 |
|---|---|
| `eval/cases.py` | 新增：5 份合成中文夹具 + 期望（`expect_fixes / expect_types / min_issues / max_issues`） |
| `eval/score.py` | 新增：4 个确定性指标 + 聚合 + `diff_runs` 带符号对拍 |
| `eval/runner.py` | 新增：CLI（`--provider / --case / --prompt-file / --judge / --baseline / --label`） |
| `services/llm.py` | `LLMResult.format_tier` + `_tier_name()`；拒绝降档的日志从 DEBUG 升到 INFO |
| `tests/test_eval_score.py` | 新增 10 例，含一条**夹具自洽性检查**（期望的错处必须真的在文档里） |
| `.gitignore` | `server/eval/runs/` 不入库（本机数字，抄进文档即可） |

**基线两轮（provider 5 qwen3.7-flash，5 条夹具，批改全部 `tier=json_schema`）**：

| 指标 | baseline-1（旧单值口径，18,625 token） | baseline-2（现组口径，19,990 token） | 读数 |
|---|---|---|---|
| `recall` | 1.0000 | 0.9167 | 第二轮成语错字漏了 1/4，同提示同夹具的漂移 |
| `locate_rate` | 0.8600 | 0.8333 | 稳定约 15% 的意见点不到原文 |
| `fix_rate` | 0.6389 | 0.5833 | 修订稿只落地一半多，两轮都是 |
| `type_coverage` | 0.6250 | **1.0000** | 变化全部来自口径（单值 → 组内任一命中），模型行为一点没变 |
| `false_positives` | 0 | 0 | 干净文档两轮各挑 1 条，没硬找 |

**这几轮数字给出的四条结论**（都是量出来的，不是猜的）：

1. **标点夹具的 `fix_rate` 两轮都是 0.3333**，而其它夹具会漂——不漂的那一项是系统性缺陷：模型肯重排句子，但不肯真正补逗号/句号。这是 prompt 能正面攻的目标，也是"重复跑"真正有价值的地方：只有稳定复现的差值才值得改代码。
2. **单轮数字会漂**：`recall` 1.0 → 0.9167、`de-di-de` 的 recall 曾在 1.0 与 0.8333 之间跳。5 条夹具的单轮差值只够看方向，要下"A/B 变好了"的结论得先加重复轮次（优先级排在加真实夹具之前）。
3. **同源自判会放水**：裁判给那条 `fix_rate=0.6667` 的结果打了 **5/5** 并写"所有误用都已修正"。模型给自己家打分系统性偏高，这是已知偏差，不是这套指标的失败——所以客观指标必须先于主观分存在。
4. **`format_tier` 立刻有回报**：批改调用稳定落在 `json_schema`，裁判那次（schema 含 array）落到了 `json_object`。没有这个字段，"换了端点分数变差"永远只能靠猜。

**还没做**（用户 2026-11-15 定：推到阶段 3 一起做）：真实文档夹具、第二厂商当裁判、**同夹具重复轮次**（两轮实测已给它排到加夹具之前）、`format_tier` 落进 `llm_calls`（改表结构，做前先问）、**浏览器端 UI 走查**（上传 PDF → 点卡片高亮 → 下载名，目前只有 API 级验证，用户同意放最后）。评分口径已定：类型期望按组写、组内任一命中即覆盖（的地得算 `typo`/`grammar`/`wording` 都算数）。

---

### 9.7 阶段 3 交付情况（2026-11-16）：长文档分块批改图

改动 3 个新文件 + 4 处接线，**第二次引入运行时依赖**（langgraph 全家桶，锁 56 → 84 包，venv 303M → 337M）。

| 文件 | 改了什么 |
|---|---|
| `services/chunking.py` | 新增：分块 + 拼回，三条不变式（切片正确 / 不重叠 / 逐字还原） |
| `graphs/grading_graph.py` | 新增：`plan → Send 扇出 → merge`，`AsyncSqliteSaver` 检查点 + `SqliteStore` 按块幂等 |
| `api/v1/process.py` | `_ModelOutput.calls` 改成一笔一笔的计量列表；批改超预算才进图；`thread_id=log-{id}` |
| `config.py` / `.env.example` | `LONGDOC_CHUNK_CHARS` / `LONGDOC_ENABLED` / `CHECKPOINT_PATH` 三项 |
| `tests/test_chunking.py` | 新增 11 例（含无标点长串硬切、空行留白、预算为 0 报错） |
| `tests/test_grading_graph.py` | 新增 7 例（全局偏移、拼回、单块降级、续跑只重打失败块、换 prompt 破缓存） |
| `test_e2e_structural.py` | 新增第 10 节 11 项：长文档走完整业务链路（扇出、逐字核对偏移、每次外呼各记一行） |
| `.gitignore` | `server/checkpoints/` 不入库 |

**门禁**：`pytest` 139 passed（+18）；`ruff check` / `format --check` 全绿（54 files）；`uv lock --check` 一致；`npm run build` 通过（前端零改动）；`test_e2e_structural.py` 40 → **51/51**，补上 `progress` 契约检查后 **52/52**（需后端带 `LONGDOC_CHUNK_CHARS=60` 启动）；`test_e2e.py` 33/33 无回归。

**写测试时抓出的四个真问题**（都不是测试写错，是实现错，逐个记下来免得改回去）：

1. **`operator.add` reducer 会让重跑累加**：同一条日志点第二次"处理"，3 块变 6 段，`reassemble` 报"块数与段数不一致"。改成按块序号 upsert。
2. **只平移了 `start` 没平移 `end`**：第 1 块恰好是恒等偏移所以看不出来，第 2 块起意见区间整体偏短。全局偏移必须成对平移。
3. **`SqliteStore(path)` 传字符串直接炸**在 `create_function` 上——构造函数要的是 `sqlite3.Connection`，路径得走 `from_conn_string()` 上下文管理器，且要显式 `setup()` 建表。
4. **假 client 按顺序吐响应在扇出下是错的**：并发分支谁先到端点不确定，A 块会拿到 B 块的答复，测试"看起来通过"其实在测运气。改成按请求内容路由（`_RouteClient`）。

**检查点到底给了什么**（这是本阶段最想记住的一条）：扇出里有一块抛异常，整步的 checkpoint 写入一起作废，重跑时**所有块都重打**——实测过。所以"第 99 块挂了别把前 98 块再烧一遍"靠的是按块 `SqliteStore`，不是靠 `AsyncSqliteSaver`。两个都留着，各管各的粒度。

**这一轮没做**：`interrupt` 人工确认分块方案（要先有 UI）、图级 `RetryPolicy`（`chat()` 已重试，再套一层双倍烧钱）、非批改类的分段汇总、`checkpoints/` 的体积治理与 TTL、多 worker 下的 saver。

### 9.8 依赖与产物减肥（2026-11-16）

用户口径：三项臃肿里做第 1 和第 3 项。第 2 项（langgraph 全家桶）**保留**——本工程的学习目标就是 LangGraph，手写 `gather` + 缓存能省包但省不掉学习价值，这笔账要算在目的上而不是体积上。Postgres 用户选择"先不碰"。

**第 1 项：pymupdf 的版面分析死重**

| 项 | 摘之前 | 摘之后 |
|---|---|---|
| `uv.lock` 包数 | 84 | **75** |
| `.venv` | 337M | **233M** |
| 被摘掉的 | onnxruntime、numpy、protobuf、flatbuffers、networkx、tabulate、psutil、pymupdf-layout、pymupdf4llm | — |

做法：`pyproject.toml` 的 `[tool.uv] override-dependencies` 给这些包挂永不成立的 marker（`python_version < '3.0'`）。依据是 `services/pdf.py` 只用 `page.get_text("blocks")`，全仓 grep 对这些包**零引用**，且除 pymupdf 外没有别的包依赖它们。**要装回来就删那几行 override**，阶段 5 真做版面分析时是即得能力，不是沉没成本。

⚠ 一个不对称：pip 表达不了 override。demo 副本的 `requirements.txt` 装 `pymupdf` 仍会拖回 onnxruntime（约 100M 差异），已记进 AGENTS §8 当交付决定。

**第 3 项：Element Plus 按需引入**

`app.use(ElementPlus)` + 全量 CSS 换成 `unplugin-vue-components` + `ElementPlusResolver`：

| 项 | 之前 | 之后 |
|---|---|---|
| 首屏 JS | 893.11 kB（gzip 283.81） | **435.67 kB（gzip 152.58）** |
| 产物形态 | 单个巨型 chunk + 体积告警 | 按路由分片，告警消失 |
| CSS | 全量 `dist/index.css` | 只打进用过的组件样式 |

插件扫不到三类东西，全部在 `main.ts` / `App.vue` 手工补上（漏一个就是"构建通过、界面裸样式"）：`ElMessage` / `ElMessageBox` 的样式、`v-loading` 指令注册、中文 locale 改用 `el-config-provider`。另外顺手清掉三处陈账：`@element-plus/icons-vue` 从没被 import 过（删）、`pytest` 同时挂在生产依赖和 dev 组里（从生产依赖摘掉）、`pyproject` 描述还写着"Word 文档 AI 处理平台"（改）。

**验证到什么程度**：`npm run build` 通过；把 `dist/assets/*.css` 拼起来逐个 grep 32 个用过的 `el-*` 类名，`.el-message*` / `.el-loading-*` / `.el-progress*` / `.el-tabs__*` / `.el-select-dropdown__item` 全部命中；`el-option` / `el-tab-pane` / `el-config-provider` 三个查不到是**类名对不上**（它们的 style 入口引的是父组件 CSS），不是缺样式。**真实浏览器交互仍未走查**（弹窗外观、loading 遮罩、分页中文文案），留给连接器或用户自查。

**门禁**：pytest 119 passed（原 142，23 项实现细节级断言已合并为行为级）；ruff check / format 全绿；`uv lock --check` 一致（75 包）；`npm run build` 通过。

### 9.9 核心业务动作 → 测试映射

九个核心动作各映射到哪些用例。删用例前先看这张表——覆盖不会消失，只会变成"上线后由用户发现"。

| # | 动作 | 单元 `tests/` | e2e |
|---|---|---|---|
| 1 | **文本提取**（docx/PDF → 段落字符串） | `test_text_extract.py`、`test_pdf.py`（26 项） | e2e_structural §4/§8（docx+PDF 同构断言） |
| 2 | **Prompt 拼装**（模板 + `{{content}}` 替换） | `test_process.py` | e2e §5（触发处理） |
| 3 | **LLM 调用**（请求/响应/重试/降级/并发闸） | `test_llm.py`（27 项） | e2e §7（预期失败优雅退出）、e2e_structural §5/§6 |
| 4 | **结构化解析**（JSON → 问题清单） | `test_grading.py`（28 项定位+解析） | e2e_structural §6（type/original/suggestion/confidence） |
| 5 | **全局定位**（`start`/`end` 偏移映射） | `test_chunking.py`（11 项三条不变式） | e2e_structural §10（全局偏移逐字对得上） |
| 6 | **落库**（状态流转 + 计量旁路） | `test_process.py`、`test_llm.py` 计量相关 | e2e §8（日志分页）、e2e_structural §9（llm_calls 行） |
| 7 | **Diff 生成**（红字标注 + 纯净全文） | `test_diff.py`（12 项） | e2e §10（下载 docx 可解析）、e2e_structural §7（含修订+含原文） |
| 8 | **下载**（RFC 5987 文件名、Content-Type） | `test_process.py` 下载相关 | e2e §10、e2e_structural §8（`批改_e2e.docx`） |
| 9 | **数据隔离**（user_id 过滤、越权 404） | `test_llm.py` resolve 隔离（4 项） | e2e §5（跨用户 404）、e2e_structural §11（SET NULL 快照保留） |

**明确不建议删的**：`test_chunking.py` 11 项 + `test_grading_graph.py` 10 项。它们锁的三条不变式（块能逐字拼回、偏移成对平移、重跑不重复计费）错了**没有任何用户可见信号**，只会让批注悄悄指到别的句子。

**e2e 的运维摩擦**：两个脚本（85 项断言 / 606 行）需起两个进程 + 真 MySQL + 轮询等待，分钟级。现已加自清理（跑完删自己的账号/日志/计量/任务/提供商/checkpoints），落盘文件因 UUID 存储名无法精确定位，偶尔手工清。

### 9.10 阶段 4 计划：Postgres 基建迁移（2026-11-16 定）

**范围**：MySQL 退役、Postgres 顶上、LangGraph 状态从 SQLite 迁到 Postgres、规范文档入库与分块存表。**不做检索**——检索（向量、关键词、混合）推到阶段 6，等本地 embedding 模型上线一起落。

**为什么不在阶段 4 顺手做检索**：阶段 4 的最大风险是迁库（驱动换、方言换、LangGraph saver 换），检索逻辑（chunker 调参、入库 pipeline、引用溯源）独立成阶段 6 可以让每一步都可单独验证、单独回滚。

#### 9.10.1 决策与边界

| 项 | 决定 |
|---|---|
| 镜像 | `pgvector/pgvector:pg16`（含 vector 扩展，阶段 6 直接 `CREATE EXTENSION`） |
| 宿主机端口 | **5434**（避开 gas-postgres 用的 5432） |
| 数据持久化 | bind mount 到 `D:\docker\fileagent-postgres`，不放 C 盘 |
| 用户/库名 | `fileagent` / `file_agent`（与其他项目隔离） |
| 现有 MySQL 数据 | **不迁**——用户口径：测试数据无所谓。新库起来后用 `reset_user_password.py` 重建账号，老 provider 手工重配 |
| `migrate_*.py` 脚本 | 废弃。新库 `create_all()` 一次建齐，不再需要幂等迁移 |
| 检查点存储 | 阶段 3 的 `SqliteSaver` + `SqliteStore` 换 `AsyncPostgresSaver` + `PostgresStore`，**两个都迁** |
| 表结构对齐 | 老库 vs 新库的外键差异（AGENTS §5）一并消失，全部按 ORM 声明走 |

#### 9.10.2 代码改动清单（按文件）

| 文件 | 改动 | 状态 |
|---|---|---|
| `server/pyproject.toml` | 删 `pymysql`，加 `psycopg2-binary` + `langgraph-checkpoint-postgres` + `psycopg[binary,pool]` | ✅ 2026-11-16 |
| `server/.env.example` | `DATABASE_URL` 串换 `postgresql+psycopg2://...@localhost:5434/file_agent`；`CHECKPOINT_PATH` 字段已撤 | ✅ 2026-11-16 |
| `server/config.py` | 注释里 `mysql+pymysql://...` 改 Postgres；`checkpoint_path` 字段已撤 | ✅ 2026-11-16 |
| `server/models/operation_log.py` | 删 `LONGTEXT` 与 `with_variant`，`DocumentText = Text()` | ✅ 2026-11-16 |
| `server/migrate_*.py` 三个 | 老 MySQL 方言脚本，已从 §2 命令清单撤下；文件保留作历史记录 | ⚠ 留观察，阶段 4 收尾前再决定删否 |
| `server/services/llm.py:record_llm_call` | 不需要改（SQLAlchemy 抽象掉方言） | ✅ 无改动 |
| `server/main.py` 顶 | **加 Windows SelectorEventLoop 切换**——psycopg3 异步模式 + 默认 ProactorEventLoop 直接卡死 | ✅ 2026-11-16 |
| `server/graphs/grading_graph.py` | `SqliteSaver` → `AsyncPostgresSaver`，`SqliteStore` → `AsyncPostgresStore`；构造走自定义 `_open_pg_conn`（psycopg.AsyncConnection + connect_timeout=5），绕开 `from_conn_string` 不带 timeout 的坑；`_build_graph` 内 store 调用全改 `await store.aget / aput`；生产入口 `grade_long_document(thread_id=...)`，不再有 `checkpoint_path` 参数；测试入口拆分出 `_grade_with_backend(checkpointer, store)` 让单测注入 SQLite | ✅ 2026-11-16 |
| `server/checkpoints/` 老 SQLite 文件 | 阶段 3 留下的两个 .sqlite3 已无引用，目录可清空 | ✅ 2026-11-16（目录已空） |
| `server/test_e2e_structural.py` docstring | MySQL → Postgres | ✅ 2026-11-16 |
| `server/tests/test_grading_graph.py` | 改成注入 AsyncSqliteSaver + AsyncSqliteStore，走 `_grade_with_backend`；新增 `derive_pg_dsn` 单测一项 | ✅ 2026-11-16 |
| `server/api/v1/process.py:grade_long_document` 调用点 | 不再传 `checkpoint_path`（生产 Postgres 自动） | ✅ 2026-11-16 |
| `server/run_backend.py` **新增** | 后端启动入口。**Windows 硬约束**：uvicorn 在 win32 非子进程模式硬编码 ProactorEventLoop（`uvicorn/loops/asyncio.py:9`），完全无视 `asyncio.set_event_loop_policy`。本文件先切 policy 再 monkey-patch `uvicorn.loops.asyncio.asyncio_loop_factory`，确保起的是 Selector。`uv run uvicorn main:app` 在 Windows 上**不能直接用**——长文档分块第 10 节必炸 | ✅ 2026-11-16 |
| `server/test_e2e.py` | httpx.Client 加 `trust_env=False`（与 structural 一致，避开 NO_PROXY 含 `[::1]` 让 httpx 解析端口炸的坑） | ✅ 2026-11-16 |

**Postgres 库里 LangGraph 表实测**：`checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations` / `store` / `store_migrations`，由 saver/store `setup()` 自动建。

#### 9.10.3 新增表（阶段 4 唯一新增 schema，**已落 2026-11-16**）

```python
# server/models/spec_doc.py
class SpecDoc(Base):
    __tablename__ = "spec_docs"
    id: Mapped[int]                    # PK
    user_id: Mapped[int]               # FK users.id, ondelete CASCADE，数据隔离
    title: Mapped[str]                 # 用户视角的标签，留空时回退到文件名去扩展名
    source_format: Mapped[str]         # '.docx' | '.pdf'
    char_count: Mapped[int]            # 列表页只读这个标量，不拉 chunks
    chunk_count: Mapped[int]
    uploaded_at                        # 时间戳

class SpecChunk(Base):
    __tablename__ = "spec_chunks"
    id: Mapped[int]                    # PK
    doc_id: Mapped[int]                # FK spec_docs.id, ON DELETE CASCADE（实测 pg_constraint 已建对）
    seq: Mapped[int]                   # 文档内块序号，0 起
    text: Mapped[str]                  # Postgres TEXT
    start: Mapped[int]                 # 全局偏移（与 services/chunking.Chunk 一致）
    end: Mapped[int]
    # 阶段 6 再加 embedding: Mapped[list[float]] = mapped_column(Vector(1024))
    # 阶段 6 同时给 text 加 tsvector 生成列 + GIN 索引（中文需 pg_jieba 或 zhparser）
```

切块复用 `services/chunking.split_document`，预算由新加配置项 `spec_chunk_chars`（默认 2000）控制，与批改链路的 `longdoc_chunk_chars=6000` 解耦——批改块大省钱、检索块小精度好，是两种不同的最优。

#### 9.10.4 验证里程碑

阶段 4 完成的标准不是"容器跑起来"，是下面全过：

1. ✅ `docker exec fileagent-postgres psql ... -c "\dt"` 看到 13 张表（业务 5 + 4 checkpoint + 2 store + 2 spec）
2. ✅ 后端起得来、`test_e2e.py` **31/0**（原 33 - 2 项 API DELETE 断言，被自清理接管）、`test_e2e_structural.py` **52/0**（含第 10 节长文档分块走通，证明 Postgres saver 接好了）
3. ✅ `pytest` **127 passed**（原 119 + 7 项 spec_storage 单测 + 1 阶段 A 已含的 DSN 派生）；单测仍走内存 SQLite
4. ✅ `checkpoints/` 目录已空——状态全在 Postgres 里
5. ✅ AGENTS.md §1 表格 DB 列改 Postgres、§5 整段"没有 Alembic + create_all 不 ALTER"那套叙述重写

**额外发现**：uvicorn 在 Windows 非子进程模式下硬编码 ProactorEventLoop（`uvicorn/loops/asyncio.py:9`），完全忽略 `asyncio.set_event_loop_policy`。所以**生产路径不能直接 `uv run uvicorn main:app`**——必须用 `server/run_backend.py` 入口（先切 policy 再 monkey-patch uvicorn 的 `asyncio_loop_factory`）。这条已写进 AGENTS §2。

#### 9.10.5 阶段 4 显式不做的事

- ❌ 任何检索（关键词、向量、混合都到阶段 6）
- ❌ prompt 注入规范条款（依赖检索）
- ❌ Alembic 引入（迁移仍按 §5 老约定：幂等脚本 + 先 SHOW COLUMNS 判存在）
- ❌ 多 worker（reaper 那条单 worker 假设不变）
- ❌ 老 MySQL 数据迁过来

### 9.11 管理员 + 默认模型授权（**2026-11-17 已实施，简单版**）

用户提出的场景：项目部署给别人注册时，新用户没配 provider → 看不到任何模型 → 处理功能直接禁用。**admin 控制谁能用"官方默认 provider"**，其余人只能用私有 provider。

**已按 §9.11.1 简单版落地**（三列：`users.role` / `users.can_use_default` / `model_providers.is_public`）。两条实施期确认：
- ❌ **不做 audit 端点**（用户 2026-11-17 拍板）——`GET /admin/users` 行内直接带任务数/处理次数/token 累计，够管人用；变更历史等真需要时再建表。
- **Admin 页范围 = 三件事**：用户列表 + 角色升降 + 默认模型授权开关。admin 的"配置默认模型"动作发生在 Providers 页（勾"官方默认"），**没有**跨用户 provider 管理视图。

【实际实现清单】

| 层 | 内容 |
|---|---|
| 模型 | `User.role`（'user'/'admin'）、`User.can_use_default`、`ModelProvider.is_public`，全默认值兼容老行 |
| 迁移 | `migrate_add_admin_support.py`：三列 `ADD COLUMN IF NOT EXISTS` + `UPDATE users SET role='admin' WHERE username=:u`（默认 LYF，argv 可换）；**只提升不降级**，重复跑安全 |
| 依赖 | `middleware/auth.py:require_admin` —— 非 admin 一律 403；§3.1 隔离铁律的**声明式例外**（admin 路由有意跨用户） |
| 授权判据 | `services/llm.py:provider_usable_by(provider, user)` —— 自己的永远可用；公共的看 `can_use_default`；别人私有永不可用。providers 列表、tasks 校验、resolve_provider 三处共用 |
| resolve | 指定 provider → 必须 usable 否则显式报错；自动挑选 → 自己的 active 优先，找不到且被授权才回落公共池 |
| 路由 | `GET /admin/users`、`PATCH /admin/users/{id}/role`、`PATCH /admin/users/{id}/default-access`；两道防自锁：**不能改自己角色**、**不能降掉最后一个 admin** |
| 提供商 | 列表 = 自己的 +（被授权时）公共的；`is_public` 只有 admin 能创建/变更（路由层 400） |
| 登录 | `TokenResponse` 带 `username/role/can_use_default`，前端据此控制管理入口与"官方"条目；**改角色/授权后需重新登录生效**（低频操作，可接受） |
| 前端 | `Admin.vue`（角色下拉 + 授权开关，自己那行角色禁用）；路由 `meta.adminOnly` + 守卫；侧栏"用户管理"仅 admin 可见；Providers 页"官方"徽标 + admin 才见"官方默认"复选框，别人名下公共条目只读 |
| 测试 | `tests/test_admin.py` 10 项：授权矩阵 / 公共回落 / 未授权不可见 / 显式指定需授权 / 自己的优先 / 两道防自锁 / 授权开关即时生效 |

【§9.11.3 四个决策点的最终答案】①bootstrap=迁移脚本（比计划多了 argv 可传用户名）；②防自锁=前后端双保险（下拉禁用 + 后端 400）；③处理中任务不打断，下一次发起在 `resolve_provider` 显式报错；④admin 视图=叠加（普通用户视角 + `/admin` 入口）。

【遗留（有意不做）】角色变更实时生效（现在要重登录）、admin 审计日志、跨用户 provider 管理、`is_public` 的字段级加密仍按 §7 未做清单。

#### 9.11.1 设计选择

**简单版（推荐起点）**：users 加 1 列 `role` + 1 列 `can_use_default`。所有公开 provider 视为一组，授权是"整组开关"。

```sql
ALTER TABLE users ADD COLUMN role VARCHAR(10) NOT NULL DEFAULT 'user';        -- 'user' | 'admin'
ALTER TABLE users ADD COLUMN can_use_default BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE model_providers ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT false;
```

**精细版（不做）**：单建一张 `default_grants(user_id, provider_id)`，admin 按条授权哪个公开 provider 谁能用。学习项目用户基数小，整组开关够用。

#### 9.11.2 隔离规则升级（AGENTS §3.1 第一次破例）

| 角色 / 状态 | 看到的 provider 列表 |
|---|---|
| 普通用户 + `can_use_default=false` | 仅 `user_id == me` 的私有 provider |
| 普通用户 + `can_use_default=true` | 私有 + 所有 `is_public=true AND is_active=true` |
| admin | 私有 + 全部 is_public + **别人的私有也能列**（带 user_id 标签，便于审计） |

`resolve_provider(user, provider_id, ...)` 在指定 id 是公开 provider 时多一层 `current.can_use_default` 检查，缺授权一律 404（§3.1 一致性）。

**AGENTS §3.1 那条"每个查询都带 user_id"必须显式加例外说明**——admin 路由是有意跨用户的，新加 admin 功能时不要"顺手"按隔离规则填一遍把 admin 接口废掉。

#### 9.11.3 必须敲定的 4 件事

| # | 决策点 | 推荐 |
|---|---|---|
| 1 | 第一个 admin 怎么诞生 | 一次性迁移脚本 `migrate_add_admin.py`，硬编码用户名 `LYF` 升 admin；与现有 `migrate_*.py` 风格一致 |
| 2 | admin 自己关自己 default 权限怎么办（误操作秒自锁） | 后端在 `PATCH /admin/users/{id}/default-access` 拒绝 `id == current.id`；前端按钮 disable |
| 3 | 用户正在用默认 provider 跑长任务，admin 中途撤销授权 | **跑完不阻断**（已开始的不能假装没花 token），下一次发起时报 401 + UI 提示"该任务的默认模型授权已撤销"。这条要写进 SPEC，不静悄悄 |
| 4 | admin 视图独立还是叠加 | 叠加。普通用户视角 + 一个 `/admin` 入口；少一套页面维护成本 |

#### 9.11.4 工作量拆解

| 模块 | 行数量级 |
|---|---|
| 迁移脚本 + ORM 列 + models/__init__ | ~80 |
| `require_admin` 依赖 + 路由权限改造 | ~40 |
| 4 个 admin API（list/role/access/audit） | ~120 |
| `resolve_provider` 加默认授权门 | ~30 |
| 单测：role + access + 撤销边界 | ~150 |
| 前端 `Admin.vue` 新页（用户表 + 开关 + 升降按钮） | ~250 |
| 前端 Home.vue / Providers.vue 微调 | ~50 |
| **合计** | **~720 行 / 1–2 天** |

#### 9.11.5 风险点

- **api_key 泄露**：admin 跨用户看 provider 时仍要 `ProviderResponse.from_orm_masked`，单测专门测一次
- **撤销授权后 task 残留**：task 指定了 `provider_id = 公开那条`，用户后续点处理 → `resolve_provider` 拒绝 → 走 `_mark_failed` 而不是 500。这条要在测试里覆盖
- **数据隔离原则松动**：admin 例外要写得清清楚楚，否则以后给路由加 user_id 过滤的人会"顺手补一刀"把 admin 接口废掉

### 9.12 阶段 5 计划：PDF→PPT（2026-11-16 决策已敲定）

**一句话**：把文字型 PDF 经 LLM 规划大纲 → python-pptx 拼装 → 假 VLM 视觉批评循环修订，端到端跑通；OCR、图片提取、真云端 VLM 都是显式留口子但本轮不实施。

#### 9.12.1 决策一览（用户 2026-11-16 拍板）

| # | 决策点 | 选定方案 |
|---|---|---|
| 1 | VLM 来源 | **先离线 mock VLM**（仿 `test_llm_server.py` 套路），架构必须留口子中后期切云端 / 本地（见 9.12.2 扩展点 A） |
| 2 | PDF 输入范围 | **仅文字型**，扫描件 OCR 推到阶段 5 之后（扩展点 B） |
| 3 | PPTX 生成路线 | **纯文字幻灯片先做**，图片提取后补（扩展点 C） |
| 4 | 任务模型扩展 | `tasks.output_format` 列（`docx` / `pptx`，默认 `docx`）；不新增 endpoint，沿用 upload → process → download 三段 |
| 5 | 视觉批评循环预算 | 默认 2 轮，用户可调；硬上限 5 轮 |
| 6 | 已装 `pdf-to-ppt` Skill 处理 | **借鉴判型 / 表格识别思路，不抄代码**（它是脚本形态，与 FastAPI 服务架构不一致） |

#### 9.12.2 架构扩展点位（"留口子"具体留在哪）

下面三个口子**阶段 5 必须先把抽象层做好**，否则后面切换实现要重写整条链路。这是用户决策 1 的硬约束。

**A. VLM 提供商切换口子**

阶段 0 已经把这件事做了一半：
- `ModelProvider.capabilities` 字段支持 `vision`
- `services/llm.py:resolve_provider(user, provider_id, capability="vision")` 按能力路由
- 传输层 `chat(messages, provider)` 不区分图像/文本——content 里塞 `image_url` 还是纯字符串都是 OpenAI 兼容

阶段 5 要做的：
1. 新增 **`server/test_vlm_server.py`**：假 VLM 端点。它**不是**普通 mock 的复制——批评循环需要"看到 PPT 渲染图"才出意见，所以这个 mock 必须：
   - 接收 messages 里含 `image_url` 的请求；
   - 从 image_url 里的元数据（比如文件名、slide_index）反推"这是第几页的哪一稿"；
   - 返回结构化的批评意见 JSON（缺标点 / 标题压正文 / 字号偏小 等）；
   - 支持通过环境变量 `MOCK_VLM_STUB_ROUNDS` 控制"前 N 轮挑刺，第 N+1 轮说没问题了"，让 e2e 测试能逼出循环退出条件。
2. 图代码（`graphs/ppt_graph.py`）接受 `provider: ModelProvider`，**不区分 provider 是 mock 还是云端**——同 §3.6 传输层不查库 / §3.10 节点闭包传 provider 的边界。
3. 真要切到云端：用户去 Providers.vue 加一条 `capabilities=text,vision`、base_url 填 `https://dashscope.aliyuncs.com/compatible-mode/v1`（或智谱、OpenAI 兼容端点）即可，代码零改动。
4. 真要切到本地 Ollama：同上，base_url 填 `http://localhost:11434/v1`，model_name 填 `qwen3-vl:4b`。

**B. OCR 扫描件口子**

当前 `services/pdf.py` 行为：检测到 PDF 没有文字层 → `PdfError("扫描件不支持")` → 路由 400。这条**必须保留**——扫描件 OCR 是个完整子系统（预处理 + 版面分析 + 中文识别），不能被一个 fallback 偷偷承担。

阶段 5 要做：
1. `services/pdf.py:extract_text_from_pdf` 改成 `extract_text_from_pdf(pdf_bytes, *, fallback_to_ocr: bool = False)`。当前所有调用方传 `False`，行为不变。
2. 留一个 `services/ocr.py` 占位模块，导出 `extract_text_via_ocr(pdf_bytes) -> str`，**阶段 5 暂抛 NotImplementedError** + 写清楚"接 rapidocr-onnxruntime / paddleocr 时在这里实现"。
3. `extract_text_from_pdf` 检测到无文字层 + `fallback_to_ocr=True` 时调它。这样调用方决定"这次要不要 OCR"，而不是 PDF 提取器自己偷跑。

**C. 图片提取与 PPTX 拼装口子**

阶段 5 文字幻灯片实现的关键约束：**PPTX 构建器签名必须接受 image_refs，即使阶段 5 永远传空**。这样后面把图片提取接上时不用重写 builder 接口。

具体：
```python
# services/pptx_builder.py（阶段 5 新建）
def build_pptx(
    outline: PptOutline,
    *,
    image_refs: dict[str, bytes] | None = None,  # key = 占位符 ID；阶段 5 永远传 None
    template_path: str | None = None,
) -> bytes:
    ...
```

`outline` 数据结构里**预留 `image_placeholders: list[str]`** 字段，即使阶段 5 LLM 不填、builder 不渲染。阶段 6（或单独的"图片轮"）实施时只要：
1. `services/pdf.py` 加 `extract_images_from_pdf()`；
2. prompt 模板让 LLM 在大纲里指定 `image_placeholders=["fig_3_1", ...]`；
3. builder 实现 image_refs 分支。
**不改 LLM 链路、不改 task model、不改 endpoint**。

#### 9.12.3 实施切片（垂直切片，按可独立验收排序）

```
s5.1 ✅ task.output_format 列 + schema 透传 + 前端表单
      - models/task.py 加列，schemas/task.py 加字段（Literal["docx", "pptx"]，默认 docx 兜老调用方）
      - migrate_add_task_output_format.py 一次性 ALTER（Postgres `ADD COLUMN IF NOT EXISTS` 幂等）
      - 前端 types.ts 加 OutputFormat 类型；Tasks.vue 表单 radio + 列表新列
      - tests/test_task_schema.py 5 项：默认 / 合法 / 非法值 / 大小写 / 响应 roundtrip
      - 顺手修了一个 baseline drift：vue-tsc 严格模式下 element-plus DefaultRow 推不到 Task/LogEntry/Provider
        类型，三个 vue 文件的 `openEdit(row)` / `remove(row)` 等 call-site 加 `row as Xxx` 显式 cast。
        原本 AGENTS §7 声称"npm run build 通过"是过期描述，2026-11-16 实测 baseline 就 fail，已修

s5.2 ✅ PPT 大纲生成图（不带批评循环）
      - schemas/ppt_outline.py：JSON schema + PptOutline / PptSlide 模型 + parse_outline_output（阶梯降级）
        + serialize_outline / deserialize_outline（kind 字段区分批改类）
      - services/ppt_outline.py：build_outline_messages（契约 system + 用户要求在 user）
      - graphs/ppt_graph.py：plan_slides 单节点 LangGraph（s5.6 加多节点时同形扩展，thread_id 已预留）
      - api/v1/process.py：_run_model 顶部加 task.output_format == "pptx" 派发；
        _render_outline_as_text 把大纲当过渡纯文本塞 processed_content（让 s5.3 之前下载不空响应）
      - 单测 13 项：schema roundtrip / 阶梯 1/2/3 降级 / 围栏 / 空 slides 占位 / 端点拒 response_format 降级 /
        图节点 mock 行为 / kind 判别器
      - 实测：145 passed（+13）；ruff 全绿

s5.3 ✅ PPTX 拼装（python-pptx，文字-only）
      - services/pptx_builder.py：build_pptx(outline, image_refs=None, template_path=None) 三参签名
        （§9.12.2.C 硬约束落地）。封面页 + 标题内容页 + 占位页（空 outline 也保证 ≥1 页）。
      - 图片轮启用路径已通：image_refs 字典 + 同名 image_placeholders → add_picture；
        未知 placeholder ID 静默跳过，缺图不抛。单测覆盖这两条
      - api/v1/process.py：download 路由按 task.output_format 派发到 docx / pptx 两条分支，
        Content-Disposition 的 filename* 仍走 RFC 5987；content-type 切到 .pptx 媒体类型
      - 单测 11 项：PK 头校验 / python-pptx 重读 / 页数匹配 / 封面文本 / bullets 段落顺序 /
        空 outline 占位 / image_refs 插图 / 未知 ID 跳过 / 参数化页数
      - 实测：156 passed（+11）；前端 build 仍通过

s5.4 ✅ PPT 渲染为图（2026-11-17，用户在 WSL 装好 libreoffice-impress 后落地）
      - services/pptx_renderer.py 从 stub 扩成双后端：settings.ppt_render_backend 派发
        'stub'（默认，单测/e2e/无 WSL 机器）/ 'soffice'（真渲染）
      - soffice 路径：Windows 临时目录写 pptx → `wsl soffice --headless --convert-to pdf`
        → pymupdf 渲 PNG（dpi 可配）。**交换走 /mnt/<盘> NTFS 挂载，不走 UNC**——
        本机实测 Windows 原生进程（python/pwsh）看到的 `\\wsl.localhost` 与 `wsl.exe`
        会话是**两个不同实例**，UNC 写的文件对方看不见；`/mnt/d` 指向真实 NTFS 无此问题
      - 每次转换开独立 `-env:UserInstallation=file:///tmp/lo_prof_<uuid>` profile：
        LibreOffice 默认配置带单实例锁，并发/重叠请求会把第二个 soffice 变哑炮
      - graph 的 render 节点改 `asyncio.to_thread`：soffice 一次 3–8s，sync 会卡死事件循环
      - 新配置：PPT_RENDER_BACKEND（默认 stub；本机 .env 已设 soffice）/ PPT_RENDER_DPI=100 /
        PPT_RENDER_TIMEOUT=90
      - 失败语义：找不到 wsl / 超时 / rc≠0 / PDF 坏 → 一律 RenderError，**绝不静默回退 stub**
        （静默降级 = 骗用户"渲染好了"，同 §3.7 degraded 不许假装 pass 一个道理）
      - 单测 8 项（test_pptx_renderer.py，subprocess 全 mock）+ gated 真渲染 smoke 2 项
        （test_ppt_render_smoke.py，无 WSL+soffice 自动 skip）
      - 实测：真渲染 e2e_ppt **28/0**（后端 .env 用 soffice 后端跑完整批评循环）

s5.5 ✅ 假 VLM 端点
      - server/test_vlm_server.py：OpenAI 兼容 chat/completions；与 test_llm_server 同形
      - 接收含 image_url 多模态 messages，按 prompt 里的 [critic-thread:...] 标记计数
      - MOCK_VLM_STUB_ROUNDS（默认 1）控制何时改口说 pass；多 thread 状态互不串
      - response_format json_schema/json_object 一律 400，逼出 chat_json 降级（与 §3.6 一致）
      - /v1/_mock/reset 单测清状态端点
      - schemas/vlm_critique.py：Critique + CritiqueIssue 模型、JSON schema、parse_critique
        阶梯降级；缺 verdict / 非 {pass, needs_revision} 一律归 needs_revision（不假装通过）
      - 单测 14 项：响应形状 / OpenAI 兼容 / response_format 拒绝 / 退出条件 / thread 隔离 /
        reset / anonymous 桶 / critique roundtrip
      - 实测：170 passed（+14）；ruff 全绿

s5.6 ✅ 批评循环（render 走 s5.4 stub，max_rounds 钳 5）
      - graphs/ppt_graph.py 重写为 5 节点图：plan_slides → assemble → render → critique
        →（条件边）→ revise_outline → assemble（loop）/ END
      - run_ppt_pipeline(...) 生产入口；plan_ppt_outline(...) 作 s5.2 兼容壳保留
      - 状态 calls 用 append reducer；每步外呼打 phase 标签，调用方据此映射
        llm_calls.purpose：outline / outline_degraded / critic / critic_degraded
      - services/pptx_renderer.py：render_pptx_to_images 返回 1×1 透明 PNG ×N；
        硬约束签名 bytes→list[bytes]，s5.4 真接渲染时**只换函数体**
      - services/vlm_critic.py：build_critic_messages（多模态 list + marker）+
        build_revise_messages（"按反馈改大纲"提示词）
      - 默认 2 / 硬上限 5；超上限 logger.warning + 钳到 5，不静悄悄
      - 没配 vision provider：critique 节点直接 pass、verdict=skipped、不调用 VLM 端
      - api/v1/process.py 调用方按 task.output_format 解析 vision provider，
        批评外呼的 llm_calls 归 critic_provider、其他归文本/大纲 provider
      - 不实施 interrupt 用户确认（同 §3.10 阶段 3 那条，要先有 UI）
      - 单测 8 项（test_ppt_graph.py）：skipped / pass 第一轮 / 修订后 pass / max_rounds 命中 /
        硬钳 5 / purpose 标签 / outline_degraded 标注 / critic_degraded 标注
      - 实测：178 passed（+8）；ruff 全绿；前端 build 不受影响

s5.7 ✅ e2e 在 Postgres + mock VLM 上跑通（28 项）
      - 新增 test_e2e_ppt.py：注册 e2e_p_* 用户、文本 + vision 双提供商、pptx 任务、
        合成中文 PDF 上传、跑完整 plan → assemble → render → critique(needs_revision)
        → revise → assemble → critique(pass)、下载 .pptx、python-pptx 重读 4 页（封面 + 3 正文）
      - 关键归因校验：llm_calls 表里 outline 行 provider_id == text_provider，
        critic 行 provider_id == vision_provider（§9.12.4 验证里程碑第 5 条的硬证据）
      - structured_output 顶层 kind=ppt_outline 字段也校了
      - 三进程：后端 8000（run_backend.py）+ 假 LLM 19000 + 假 VLM 19001；
        脚本启动前 wait_url 探测；跑完 finally 自动清 DB（同 test_e2e_structural 模式）
      - test_llm_server.py 同次扩展：_is_outline_request 用 ("幻灯片大纲", "完整新大纲")
        两个 system prompt 短语判别 plan / revise 都走 outline 分支，
        其余仍走 grading 分支保 test_e2e_structural 52/0 不变
      - 实测：test_e2e_ppt 28/0；test_e2e 31/0；test_e2e_structural 52/0（无回归）

s5.8 ✅ 文档收口（s5.4 推后续）
      - SPEC §9.13 阶段 5 交付情况（初版 11-16 记 6 切片；11-17 s5.4 真渲染收尾，8 切片全交付）
      - AGENTS §3.11 阶段 5 不可违反的约定（build_pptx / render 签名硬约束、
        critic provider 走 capabilities=vision 路由、purpose 集合扩 4 值、
        critic-thread 标记规则、parse 阶梯不许抛 pass 等）
      - 路线表阶段 5 → 🟡 6 切片已交付，s5.4 单独排
      - AGENTS §2 命令补 test_vlm_server.py + test_e2e_ppt.py
      - 门禁基线刷新：178 pytest + 31/0 + 52/0 + 28/0
```

每个切片**单独 commit**、可单独回滚。s5.1 与 s5.3 之间有依赖（output_format 没接通就没法走 PPTX 分支），但 s5.4 和 s5.5 互相独立可以并行。

#### 9.12.4 验证里程碑

1. `task.output_format` 列在 Postgres 里建好，单测覆盖"指定合法值通过 / 非法值 422 / 老库迁移幂等"
2. 一份 5 页文字型 PDF 上传 → 处理 → 拿到 5 页 PPTX，python-pptx 重读能验每页标题非空
3. mock VLM 批评循环跑满 2 轮，`outputs/` 看到 3 份中间稿（初稿 + 第 1 轮 + 终稿）
4. `test_e2e_ppt.py` 端到端 ≥ 25 项断言全过、自清理不留数据
5. 把假 VLM 提供商换成 dashscope qwen-vl-max（同 base_url 形状 + 真 key）后**代码零改动**也能跑通批评循环——这条证明 9.12.2.A 的口子确实留住了

#### 9.12.5 阶段 5 显式不做的事

- ❌ 扫描件 OCR（接 rapidocr，但留到后面）
- ❌ PDF 图片提取与幻灯片插图（builder 签名留 image_refs 但永远传 None）
- ❌ 模板 PPTX（用户自定义母版）—— python-pptx 支持但学习项目里不值得做
- ❌ interrupt 用户确认大纲（同 §3.10 阶段 3 那条，要先有 UI 才好做）
- ❌ 真云端 VLM 集成测试（接口留好了，e2e 跑 mock 就够；接云端是用户运行时配置）
- ❌ PPTX 动画、过渡、SmartArt 等非文本元素
- ❌ 多页大文档的分块 PPT（>50 页 PDF 推给阶段 6 检索配合做）

### 9.13 阶段 5 交付情况（2026-11-16 起，2026-11-17 s5.4 收尾）

**8 切片全部交付**。s5.4 真渲染于 11-17 用户在 WSL 装好 libreoffice-impress 后落地。

【端到端能力实测】
**上传一份 4 行中文 PDF → pptx 任务 → 自动跑完"plan_slides → assemble → render(真 soffice) → critique(needs_revision) → revise_outline → assemble → render → critique(pass)" → 下载 4 页 .pptx**。LLM 外呼归 text provider，VLM 外呼归 vision provider，llm_calls 表分别记 purpose=outline / critic。真渲染模式 e2e_ppt **28/0**（2026-11-17 实测）。

【门禁基线（2026-11-17）】

| 项 | 数字 | 增量来源 |
|---|---|---|
| `pytest` | **189 passed** | 阶段 0–3 142 → 阶段 4 +8 → s5.1 +5 → s5.2 +13 → s5.3 +11 → s5.5 +14 → s5.6 +8 → s5.4 +10 |
| `test_e2e.py` | 31/0 | 未变 |
| `test_e2e_structural.py` | 52/0 | 未变（mock LLM 加了大纲路由不影响 grading 检测） |
| `test_e2e_ppt.py` | **28/0（真渲染跑通）** | s5.7 新增 |
| `npm run build` | 通过 | s5.1 顺手修了 baseline drift（vue-tsc + element-plus DefaultRow） |
| `ruff check / format` | 全绿 / 77 files formatted | — |
| `uv lock` | 83 包 | python-pptx 拖入 pillow + xlsxwriter |

【文件清单】

新增：
- `server/schemas/ppt_outline.py`（PptOutline / PptSlide / parse / serialize / kind 字段）
- `server/schemas/vlm_critique.py`（Critique / CritiqueIssue / parse_critique）
- `server/services/ppt_outline.py`（build_outline_messages + OUTLINE_OUTPUT_INSTRUCTION）
- `server/services/vlm_critic.py`（build_critic_messages + build_revise_messages）
- `server/services/pptx_builder.py`（build_pptx 三参签名硬约束）
- `server/services/pptx_renderer.py`（**双后端 stub/soffice**，WSL 交换路径约定见模块 docstring）
- `server/graphs/ppt_graph.py`（5 节点 LangGraph，run_ppt_pipeline + plan_ppt_outline 兼容壳）
- `server/test_vlm_server.py`（mock VLM，[critic-thread:...] 计数）
- `server/test_e2e_ppt.py`（28 项端到端）
- `server/tests/test_ppt_outline.py` / `test_pptx_builder.py` / `test_vlm_critique.py` / `test_ppt_graph.py` / `test_pptx_renderer.py` / `test_ppt_render_smoke.py`（gated）
- `server/migrate_add_task_output_format.py`（Postgres `ADD COLUMN IF NOT EXISTS` 幂等）

修改：
- `server/models/task.py` 加 `output_format`
- `server/schemas/task.py` 加 `Literal["docx","pptx"]` 校验
- `server/api/v1/process.py` `_run_model` 三路派发 + `_error_purpose_for` + `_render_outline_as_text` + 下载路由派发改 `.pptx`
- `server/test_llm_server.py` 按 system prompt 路由 outline / grading 响应
- `server/main.py` 加 Windows SelectorEventLoop 切换 + 启动期 event_loop 类型诊断
- `server/config.py` 加 `ppt_render_backend` / `ppt_render_dpi` / `ppt_render_timeout`
- `client/src/api/types.ts` 加 OutputFormat 类型 + Task/TaskCreatePayload 字段
- `client/src/views/Tasks.vue` 表单 radio + 列表新列
- `client/src/views/{Tasks,Logs,Providers}.vue` call-site 加 `row as Xxx` cast 修 vue-tsc

【s5.4 落地后的运行约定】
1. `ppt_render_backend` 代码默认 `stub`（可移植）；本机 `.env` 已设 `PPT_RENDER_BACKEND=soffice` 启用真渲染。
2. Windows ↔ WSL 文件交换**必须走 `/mnt/<盘>` NTFS 挂载点**，不能用 `\\wsl.localhost` UNC——本机实测 Windows 原生进程（python/pwsh）看到的 wsl.localhost 与 `wsl.exe` 会话是**两个不同实例**，互相看不见对方的文件（2026-11-17 排查记录）。
3. soffice 每次转换开独立 `-env:UserInstallation` profile（默认配置有单实例锁，并发会变哑炮），跑完清理。
4. 渲染节点在 graph 里走 `asyncio.to_thread`：soffice 一次 3–8s，sync 会卡死事件循环。
5. 换渲染实现（LibreOffice on Windows / pdf2image / 远程服务）只改 `_render_via_soffice` 函数体，`render_pptx_to_images` 签名与 graph 不动。

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

**仍未验证**：`json_schema` 严格档只在 mock 端点验证过降级路径，没拿真实国产端点跑过一次严格模式。（阶段 1.5 / 阶段 2 的真实端点实测已命中 `json_schema`，见 9.5 / 9.6。）

### 第五轮：阶段 1.5 ~ 3（PDF 输入、评测台、分块批改）

只列缺陷本体，机理见 9.5 / 9.6 / 9.7。

| ID | 优先级 | 问题 | 修复方案 |
|---|---|---|---|
| s1 | 高 | 下载名是 `批改_ + 原文件名`，PDF 输入会产出一个叫 `.pdf` 的 docx，下载方打不开 | 取 stem 再补 `.docx`；docx 输入的结果名不变 |
| s2 | 高 | 分块图用 `operator.add` 当 reducer，同日志重跑把 3 块累加成 6 段，`reassemble` 报错 | 换成按块序号 upsert 的 reducer |
| s3 | 高 | 块内偏移只平移了 `start` 没平移 `end`，意见区间整体偏短 | `start`/`end` 成对平移（第 1 块恒等偏移所以看不出来） |
| s4 | 中 | 以为"检查点能让失败的块重跑不重复计费"，实测扇出内一块失败会整步作废、全部重打 | 加 `SqliteStore` 按块幂等（键含块文本 + 指令摘要 + 模型名）；命中的块不写 `llm_calls` |
| s5 | 中 | ORM 声明 `Text` 与本机老库 `LONGTEXT` 漂移，长 PDF 会撞 64KB 字节上限 | `Text().with_variant(LONGTEXT, "mysql")`，新建库与老库一致 |
| s6 | 低 | 评测台类型期望写死单值，的地得被判成"用词"就整片红，指标在跟模型较劲 | 类型期望改成按组 OR 语义，组间仍 AND |
| s7 | 低 | `SqliteStore(path)` 传字符串在 `create_function` 处炸 | 走 `from_conn_string()` 上下文并显式 `setup()` |

**测试脚手架的教训**：假 client 按脚本顺序吐响应，在扇出并发下会把 A 块的答复发给 B 块——测试"通过"只是在测运气。改成按请求内容路由（`tests/test_grading_graph.py` 的 `_RouteClient`）。

**回归验证（本轮）**：pytest 139 passed；`test_e2e_structural.py` 52/52（后端需带 `LONGDOC_CHUNK_CHARS=60`）；`test_e2e.py` 33/33；ruff check / format 全绿；`uv lock --check` 一致（减肥后 75 包）；`npm run build` 通过。

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

# 长文档分块批改（阶段 3）
LONGDOC_CHUNK_CHARS=6000
LONGDOC_ENABLED=true
CHECKPOINT_PATH=checkpoints/grading.sqlite3

LOG_LEVEL=INFO
```

> `CORS_ORIGINS` 必须在 `config.py` 的 `Settings` 里声明成字段才能生效：pydantic-settings 只把 `.env` 的值注入 Settings 对象，**不会写入 `os.environ`**，因此 `os.getenv("CORS_ORIGINS")` 永远读不到 `.env` 里的配置（这是 s10 的成因）。多个源用逗号分隔。**新增任何配置项都要先在 `Settings` 里加字段，再同步进 `.env.example`**，两条都是硬约定。
>
> **LLM 相关取值口径**：`LLM_TIMEOUT_CONNECT` 只管建连，`LLM_TIMEOUT_READ` 等响应收完（长文档调大这一个）；`LLM_MAX_RETRIES` 只对网络错误 / 超时 / 429 / 5xx 生效；`LLM_MAX_CONCURRENCY` 是进程内上限；`LLM_TRUST_ENV_PROXY=false` 用来绕开 `NO_PROXY` 含非法条目（如裸 `[::1]`）时 httpx 构造即报 `InvalidURL: Invalid port` 的问题——需要走系统代理访问境外端点时**不要关它**。
>
> `JWT_EXPIRE_HOURS` 文档口径是 2（也是 `config.py` 的默认值），本机 `.env` 可能为了开发方便设成 24。`.env` 不入库，以本机为准。
>
> **分块批改口径**：`LONGDOC_CHUNK_CHARS` 是每块字符预算（中文约 1 字 ≈ 1 token），整篇不超过它就不进图；`LONGDOC_ENABLED=false` 把长文档也压回单发路径，用来跟分块结果做 A/B（评测台的 `--prompt-file` 只管提示词，切不切块得靠这个开关）。`CHECKPOINT_PATH` 是图状态文件，按块缓存自动用同目录的 `.chunks` 兄弟文件——两个连接写同一个 sqlite 会撞库锁。两者都在 `server/checkpoints/`，已 gitignore，**只增不自缩**，属定期清理项。


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