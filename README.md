# FileAgent

文档 AI 处理平台。上传文档，按你自己写的 Prompt 决定要做什么，下载结果。

同一条链路上现在能做的事：批改（红字 diff + 可定位的问题清单）、润色、摘要、翻译，以及把文字型 PDF 整成一份可编辑的 PPT。批改类任务还可以先召回**你自己的规范库**里的相关条款再判，结果里带条款号可溯源。

> **输入边界**：`.docx` 与**文字型** `.pdf`。扫描版 PDF（没有文字层或文字层薄到像是图片 OCR 出来的）直接拒收，不做 OCR；PDF 里的表格不进正文。
> **输出**：文字类任务统一 `.docx`，PPT 任务 `.pptx`。

## 功能

- **账号与数据隔离**：注册 / 登录（JWT），任务、模型提供商、操作日志、规范库全部按用户隔离；越权一律 404 而不是 403
- **模型提供商**：任意 OpenAI 兼容端点（豆包 / 通义 / DeepSeek / GPT / Ollama / LM Studio），网页上填 `base_url + api_key + model_name` 就能换模型；API Key 只回显掩码，每个提供商标 `text / vision / embedding` 能力，用途不匹配直接报错，不偷偷换模型
- **管理员与默认模型授权**：管理员可把某个提供商设为公共、给用户授予"可使用官方默认模型"，改完用户下次进入应用即生效，无需重登
- **任务模板**：自定义 Prompt，用 `{{content}}` 占位文档正文；可指定提供商、可标记为批改类、可声明输出 `docx` 或 `pptx`
- **多格式输入**：`.docx` 与文字型 `.pdf` 走同一份"按行分隔的自然段"提取契约——PDF 侧会把被排版切成多行的段落按几何邻接拼回去、剔掉页码与跨页重复的页眉装饰，所以 diff、原文定位、结果生成都不区分来源
- **三步处理**：上传 → 处理 → 下载。处理在后台执行、前端轮询状态，刷新 / 切页 / 断连都不丢进度
- **结构化批改**：问题清单只让模型逐字摘原文片段，位置由后端匹配；匹配不到就标「未定位」，绝不把批注指到错位置
- **长文档分块批改**：批改类超过预算（默认 6000 字）时自动切块并行批改，再按原位置拼回一篇——偏移仍是整篇偏移，清单照旧可点。跑到一半失败重跑，**已成功的块不再重复计费**
- **规范库与条款召回**：上传校对规范 / 格式要求（`.docx` / `.pdf`）→ 按**条款边界**切块入库 → 批改时按相关度召回 top-k 条款注入提示词。本地 embedding 端点 + pgvector 精确余弦扫描 + 字二元组词法召回，两路 RRF 融合
- **调用计量**：每次外呼记 token / 耗时 / 重试次数 / 用途到 `llm_calls`，成本可追溯（embedding 端点不报 token，所以不记账，成本用字符数代理）
- **操作日志**：分页 + 状态筛选 + 文件名搜索 + 批注条数，失败可重试，完成可重新下载

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI · SQLAlchemy 2.0 · psycopg2 · python-docx · python-pptx · PyMuPDF（PDF 提取）· pgvector（向量列）· LangGraph（分块批改与 PPT 批评循环）· httpx · bcrypt + python-jose |
| 前端 | Vue 3（`<script setup>`）· TypeScript · Vite · Element Plus（按需引入）· Pinia |
| 数据库 | Postgres 16 + pgvector（Docker 镜像 `pgvector/pgvector:pg16`），13 张表：业务 5 张（`users` / `model_providers` / `tasks` / `operation_logs` / `llm_calls`）+ 规范库 2 张（`spec_docs` / `spec_chunks`）+ LangGraph 6 张（checkpoints / store 系列） |
| 工具链 | uv（Python 3.12）· ruff · pytest · vitest · vue-tsc |

## 快速开始

前置：Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js、Docker。

```bash
# 1. 起数据库（端口 5434，数据卷在本机 D:\docker\fileagent-postgres）
docker run -d --name fileagent-postgres -p 5434:5434 \
  -e POSTGRES_USER=fileagent -e POSTGRES_PASSWORD=*** -e POSTGRES_DB=file_agent \
  pgvector/pgvector:pg16

# 2. 后端
cd server
cp .env.example .env          # 必填 DATABASE_URL 与 JWT_SECRET_KEY
                              # 要用规范库/RAG 再填 EMBEDDING_BASE_URL 与 EMBEDDING_MODEL
uv sync
uv run python run_backend.py  # Swagger: http://localhost:8000/docs

# 3. 前端（另开终端）
cd client
npm install
npm run dev                   # http://localhost:5173
```

⚠ **别用 `uv run uvicorn main:app` 起后端**。uvicorn 在 win32 非子进程模式下硬编码 ProactorEventLoop，与 psycopg3 异步模式冲突，长文档分块批改跑到中途必炸；`run_backend.py` 会先切 Selector 事件循环再打补丁。首次启动自动建表。

之后：注册账号 → 「模型提供商」填一个可用的 Key → 「任务」建一个 Prompt 模板 → 工作台上传文档并处理。要用规范库就去「规范库」传一份校对规范。

## 测试与检查

```bash
cd server
uv run pytest -q              # 290 个用例，不碰 DB 也不出网（HTTP 用假 client、DB 用内存 SQLite）
uv run ruff check .           # lint；uv run ruff format --check . 查格式
uv run python test_e2e.py                  # 端到端冒烟 31 项：需后端在 8000 已起
uv run python test_e2e_structural.py       # 结构化批改 + PDF + 分块 60 项（零真实 token）
uv run python test_e2e_ppt.py              # PDF→PPT 流水线 28 项（含真渲染，需 WSL LibreOffice）
uv run python test_e2e_rag.py              # 规范库→检索→注入 43 项（需本地 embedding 端点在跑）

# 上面三套 e2e 需要先起假端点：
uv run uvicorn test_llm_server:app --port 19000
uv run uvicorn test_vlm_server:app --port 19001
# 分块那一节要后端带 LONGDOC_CHUNK_CHARS=60 启动才会真的切块

# 评测台
uv run python -m eval.retrieval            # 规范召回 recall@k，纯本地、零 token
uv run python -m eval.runner --provider 5 --case clean   # 批改质量：⚠ 会调真实模型花钱，先单条

cd ../client
npm run build                 # vue-tsc 类型检查 + 构建（唯一的类型门禁）
npm test                      # 33 个前端用例（vitest，只测纯逻辑）
```

## 文档

- [SPEC.md](SPEC.md) — 设计指南：Java→Python 对照、各阶段设计与交付、DDL、API 表、缺陷修复台账
- [AGENTS.md](AGENTS.md) — 编码智能体须知：项目约定、安全底线、已知陷阱与当前基线

## 还没做

限流、API Key 字段级加密、Alembic 迁移、多 worker 部署、扫描版 PDF 的 OCR、docx/PDF 表格提取、把问题清单写进交付 docx、长文档分块路径的人工确认分块方案、`checkpoints/` 体积治理与 TTL。详见 AGENTS.md §7。
