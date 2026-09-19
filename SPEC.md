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
│       ├── views/            # Login Home TaskManage Log Provider
│       ├── components/       # AppLayout FileUpload TaskSelector ResultPanel
│       ├── stores/           # Pinia: auth.ts
│       ├── api/index.ts      # Axios 封装
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

### 5.4 上传 + 处理 + 下载流程

这是核心业务链路，分五步：

1. **上传**：接收 .docx 文件 → 校验扩展名 → python-docx 提取文本 → 存 operation_log(status=pending)
2. **处理**：取 task.prompt_template → `{{content}}` 替换为文档文本 → 调 LLM → 存 processed_content(status=done)
3. **下载**：读 operation_log → 如果是批改任务，diff 对比原文与结果；否则直接写入 → 生成 .docx 返回

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
  name VARCHAR(50) NOT NULL,
  base_url VARCHAR(255) NOT NULL,
  api_key VARCHAR(255) NOT NULL,
  model_name VARCHAR(100) NOT NULL,
  max_tokens INT DEFAULT 4096,
  temperature FLOAT DEFAULT 0.7,
  is_active TINYINT DEFAULT 1,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
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
  FOREIGN KEY (provider_id) REFERENCES model_providers(id)
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
  FOREIGN KEY (task_id) REFERENCES tasks(id)
);
```

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
| POST | /api/v1/upload | 是 | 上传 Word |
| POST | /api/v1/process/:id | 是 | 调 LLM 处理 |
| GET | /api/v1/download/:id | 是 | 下载 Word 结果 |
| GET | /api/v1/logs | 是 | 操作日志（分页） |

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
7. ✅ 操作日志 API
8. ✅ 前端初始化 + 登录页 + 布局
9. ✅ 主操作页 + 任务管理 + 提供商配置 + 日志页
10. 联调 + Docker 部署

---

## 10. 环境变量 (.env)

```
DATABASE_URL=mysql+pymysql://root:password@localhost:3306/file_agent
JWT_SECRET_KEY=your-secret-key-change-me
JWT_EXPIRE_HOURS=2
UPLOAD_DIR=uploads
OUTPUT_DIR=outputs
```

---

> **上手方式**：按第 4 节先把空项目跑通（后端 `health` + 前端页面），再按第 9 节顺序逐一实现模块。知识点在做的过程中查，不要提前啃完文档。