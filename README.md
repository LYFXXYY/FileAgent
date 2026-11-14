# FileAgent

Word 文档 AI 处理平台。上传 `.docx`，按自定义 Prompt 调用大模型处理，下载结果文档；批改类任务输出**红字 diff 标注**（新增标红、删除标红加删除线），文末另附一份可直接复制的纯净全文。

## 功能

- **账号与数据隔离**：注册 / 登录（JWT），任务、模型提供商、操作日志全部按用户隔离
- **模型提供商**：任意 OpenAI 兼容端点（豆包 / 通义 / DeepSeek / GPT / Ollama），网页上配 `base_url + api_key + model_name` 即可换模型，API Key 只回显掩码
- **任务模板**：自定义 Prompt，用 `{{content}}` 占位文档正文；可指定提供商、可标记为批改类
- **三步处理**：上传 → 处理 → 下载。处理在后台执行、前端轮询状态，刷新 / 切页 / 断连都不丢进度
- **操作日志**：分页 + 状态筛选 + 文件名搜索，失败可重试，完成可重新下载

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI · SQLAlchemy 2.0 · PyMySQL · python-docx · httpx · bcrypt + python-jose |
| 前端 | Vue 3（`<script setup>`）· TypeScript · Vite · Element Plus · Pinia |
| 数据库 | MySQL 8，4 张表：`users` / `model_providers` / `tasks` / `operation_logs` |
| 工具链 | uv（Python 3.12）· ruff · pytest · vue-tsc |

## 快速开始

前置：Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js、MySQL 8。

```bash
# 1. 建库
mysql -u root -p -e "CREATE DATABASE file_agent DEFAULT CHARSET utf8mb4"

# 2. 后端
cd server
cp .env.example .env                            # 填 DATABASE_URL 和 JWT_SECRET_KEY
uv sync
uv run uvicorn main:app --reload --port 8000    # Swagger: http://localhost:8000/docs

# 3. 前端（另开一个终端）
cd client
npm install
npm run dev                                     # http://localhost:5173
```

首次启动自动建表。之后：注册账号 → 「模型提供商」填一个可用的 Key → 「任务」建一个 Prompt 模板 → 回工作台上传 `.docx` 并处理。

## 测试与检查

```bash
cd server
uv run pytest -q            # 12 个纯函数用例（diff / word），不碰 DB 和网络
uv run ruff check .         # lint
uv run python test_e2e.py   # 端到端冒烟：需后端已在 8000 运行，会注册临时账号造数据

cd ../client
npm run build               # vue-tsc 类型检查 + 构建
```

## 文档

- [SPEC.md](SPEC.md) — 设计指南：Java→Python 对照、模块实现要点、DDL、API 表、缺陷修复台账
- [AGENTS.md](AGENTS.md) — 编码智能体须知：项目约定、安全底线、已知陷阱与当前基线

## 说明

`server/uploads/`、`server/outputs/`、`.env` 均不入库。当前阶段未做限流、API Key 字段级加密、Alembic 迁移与多 worker 部署，详见 AGENTS.md §7。