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

不引入 LangChain 等重框架，自己写一个 30 行的 ReAct 控制循环——理解原理后再学框架才能知道它帮你省了什么。

### 框架选型参考

| 框架 | 适用场景 | 本项目 |
|---|---|---|
| LangChain | 全功能 Agent | 暂不采用（抽象层多，学习曲线陡） |
| LlamaIndex | RAG / 数据索引 | 不需要 |
| OpenAI Func Call | 模型原生支持工具调用 | 后续可替换自实现 |
| 自实现 ReAct | 理解原理 + 轻量 | 当前方案 |

---

## 3. 项目搭建（分步骤）

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
uvicorn main:app --reload --port 8000
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

> ⚠ 本节早期设计为"所有用户共享 provider"，因存在凭据越权风险已改为按用户隔离。


### 5.4 上传 + 处理 + 下载流程

这是核心业务链路，分三步：

1. **上传**：接收 .docx 文件 → 校验扩展名 → python-docx 提取文本 → 存 operation_log(status=pending) → **返回 extracted_text 供前端预览**
2. **处理**：`POST /process/{id}` **立即返回** `status=processing`，真正的 LLM 调用放进 FastAPI `BackgroundTasks` 异步执行——取 task.prompt_template → `{{content}}` 替换为文档文本 → 调 LLM → 后台把结果写回 `processed_content(status=done)` 或 `error_message(status=failed)`。前端通过轮询拿结果。
3. **下载**：读 operation_log → 如果是批改任务，diff 对比原文与结果；否则直接写入 → 生成 .docx 返回

**为什么处理要后台化（不在请求里 await）**：LLM 可能耗时十几秒到一两分钟。若在请求内 `await`，客户端一旦断开（刷新/关页/网络抖动），请求被取消，日志会**永远卡在 processing**。改用后台任务 + 独立 DB session（`SessionLocal()`，不能用请求级 session，它在响应发出后就关了），响应先发出去，处理在后台跑完，断开重连也不影响。

**僵尸状态清理**：`BackgroundTasks` 不持久化，进程重启会丢失正在跑的后台任务。`main.py` 启动时把所有遗留的 `processing` 日志重置为 `failed`（错误信息"服务重启导致处理中断，请重试"），避免前端无限轮询。⚠ 仅适用单 worker；多 worker 部署应改用带超时阈值的 reaper 或任务队列（如 Celery/RQ）。

**轮询机制**：因为处理是异步的，前端点击"开始处理"后**总是**走轮询——通过 `GET /process/{id}/status` 每 5 秒查询一次（最多 120 次 / 10 分钟），`done` 自动推进到下载步骤，`failed` 显示错误。组件失活（onDeactivated）时暂停轮询，重新激活（onActivated）时恢复。

**步骤导航**：上传步骤可返回选任务（保留选中状态）；下载步骤可"处理新文件"回到上传步骤。每个步骤之间都有上一步按钮。

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
  uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  processed_at DATETIME,
  status ENUM('pending','processing','done','failed') DEFAULT 'pending',
  error_message TEXT,
  FOREIGN KEY (user_id) REFERENCES users(id),
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);
```

> **外键 ON DELETE 说明**：`tasks.provider_id` 和 `operation_logs.task_id` 用 `SET NULL`——删除 provider/task 时自动置空引用而非报错（否则 MySQL 外键约束会让 DELETE 返回 500）。日志靠 `task_name`/`provider_name` 快照仍可读。应用层删除时也**主动**先把引用置 NULL，兼容尚未带 ON DELETE 的旧库。
>
> **已有库迁移**：`model_providers.user_id` 是后加的列，`create_all()` 不会 ALTER 已存在的表。运行 `python server/migrate_add_provider_user_id.py`（幂等）补列并把现有 provider 回填给指定用户。

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

---

## 8. 部署

### 开发环境

```bash
# 后端
cd server && uvicorn main:app --reload --port 8000
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

## 9. 实现顺序

1. ✅ 后端骨架：FastAPI + MySQL 连接 + 建表
2. ✅ 认证模块：注册/登录 + JWT 中间件
3. ✅ 提供商 CRUD + 通用 LLM 调用服务
4. ✅ 任务 CRUD
5. ✅ Word 读写 + diff 批注服务
6. ✅ 上传 → 处理 → 下载 核心流程
7. ✅ 操作日志 API（含搜索筛选 + 重试支持）
8. ✅ 前端初始化 + 登录页 + 布局
9. ✅ 主操作页 + 任务管理 + 提供商配置 + 日志页
10. ✅ 前端状态持久化（keep-alive + sessionStorage）
11. ✅ 工作台 UX 增强：提供商可用性预检查、处理轮询、步骤导航、文本预览
12. ✅ 日志页 UX 增强：搜索筛选、失败重试、空状态提示
13. 联调 + Docker 部署

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

**回归验证**：pytest 12 passed；后端端到端冒烟 14/14 PASS（隔离/越权 404/删除不 500/后台处理流转）；前端 `vue-tsc --noEmit` 0 错误。


---

## 11. 环境变量 (.env)

```
DATABASE_URL=mysql+pymysql://root:password@localhost:3306/file_agent
JWT_SECRET_KEY=your-secret-key-change-me
JWT_EXPIRE_HOURS=2
UPLOAD_DIR=uploads
OUTPUT_DIR=outputs
```

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