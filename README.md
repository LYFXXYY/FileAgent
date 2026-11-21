# FileAgent

文档 AI 处理平台。上传文档，按你自己写的 Prompt 决定要做什么，下载结果。

同一条链路上现在能做的事：批改（红字 diff + 可定位的问题清单）、润色、摘要、翻译；PDF 忠实转换成**可编辑 PPT**（文字进文本框、表格进原生表、图片原位贴图）；Word / PDF 转 **Markdown**。扫描版 PDF 配好 MinerU token 后也能处理（云端 OCR 兜底）。批改类任务还可以先召回**你自己的规范库**里的相关条款再判，结果里带条款号可溯源。

> **输入**：`.docx` 与 `.pdf`（文字型直接提取；扫描件走 MinerU 云 OCR，需服务端配置 `MINERU_TOKEN`）。PDF 里的表格不进批改正文。
> **输出**：由任务声明——文字类 `.docx`（批改带红字 diff）、转换类 `.pptx` / `.md`。
> **手机端**：≤768px 自动切换底部 TabBar + 卡片列表布局。

## 功能

- **账号与数据隔离**：注册 / 登录（JWT），任务、模型提供商、操作日志、规范库全部按用户隔离；越权一律 404 而不是 403
- **注册即开箱可用**：新账号自动获得 4 个预置任务（作文批改 / 文本润色 / PDF 转 PPT / 文档转 Markdown）和一份通用校对规范（进你的规范库）；embedding 端点没配也不挡注册
- **模型提供商**：任意 OpenAI 兼容端点（豆包 / 通义 / DeepSeek / GPT / Ollama / LM Studio），网页上填 `base_url + api_key + model_name` 就能换模型；API Key 只回显掩码，每个提供商标 `text / vision / embedding` 能力，用途不匹配直接报错，不偷偷换模型
- **管理员与默认模型授权**：管理员可把某个提供商设为公共、给用户授予"可使用官方默认模型"，改完用户下次进入应用即生效，无需重登
- **任务模板**：自定义 Prompt，用 `{{content}}` 占位文档正文；可指定提供商、可标记为批改类、可声明输出 `docx` / `pptx` / `md`
- **多格式输入**：`.docx` 与文字型 `.pdf` 走同一份"按行分隔的自然段"提取契约——PDF 侧会把被排版切成多行的段落按几何邻接拼回去、剔掉页码与跨页重复的页眉装饰，所以 diff、原文定位、结果生成都不区分来源
- **扫描件 PDF OCR 兜底**：无文字层 / 薄文字层的 PDF 不再拒收——受理文档、处理阶段调 MinerU 云识别，正文写回后批改定位照旧；识别产物（markdown / 版面块 / 图片）留存备查
- **三步处理**：上传 → 处理 → 下载。处理在后台执行、前端轮询状态，刷新 / 切页 / 断连都不丢进度
- **结构化批改**：问题清单只让模型逐字摘原文片段，位置由后端匹配；匹配不到就标「未定位」，绝不把批注指到错位置
- **长文档分块批改**：批改类超过预算（默认 6000 字）时自动切块并行批改，再按原位置拼回一篇——偏移仍是整篇偏移，清单照旧可点。跑到一半失败重跑，**已成功的块不再重复计费**
- **PDF → PPT 忠实转换**：MinerU 版面块驱动渲染——按阅读顺序重排为主、整页忠实兜底；表格解析成原生可编辑 PPT 表；解析坏块在配置视觉模型后以区域原图为底自动修复（产物标注"LLM 修复"），版面还有问题则由视觉批评循环调整布局（只调版面不改文字）
- **文档 → Markdown**：零大模型——Word 本地免费转换；PDF 走 MinerU 版面识别，保留标题层级、表格与图片引用，带图时打包 zip 下载
- **规范库与条款召回**：上传校对规范 / 格式要求（`.docx` / `.pdf`）→ 按**条款边界**切块入库 → 批改时按相关度召回 top-k 条款注入提示词。本地 embedding 端点 + pgvector 精确余弦扫描 + 字二元组词法召回，两路 RRF 融合
- **调用计量**：每次外呼记 token / 耗时 / 重试次数 / 用途到 `llm_calls`，成本可追溯（embedding 与 MinerU 按页计费没有 token，不编数字入账，成本用字符数代理）
- **操作日志**：分页 + 状态筛选 + 文件名搜索 + 批注条数，失败可重试，完成可重新下载

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI · SQLAlchemy 2.0 · psycopg2 + psycopg3（LangGraph 异步 saver）· python-docx · python-pptx · PyMuPDF（PDF 提取与区域裁图）· pgvector（向量列）· LangGraph（长文档分块批改）· httpx · MinerU 官方云 API（OCR 兜底与版面块）· bcrypt + python-jose |
| 前端 | Vue 3（`<script setup>`）· TypeScript · Vite · Element Plus（按需引入）· Pinia · 手机端响应式布局（底部 TabBar + 卡片列表） |
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
                              # 要处理扫描件 PDF / PPT / md 转换再填 MINERU_TOKEN（mineru.net 申请）
uv sync
uv run python run_backend.py  # Swagger: http://localhost:8000/docs

# 3. 前端（另开终端）
cd client
npm install
npm run dev                   # http://localhost:5173
```

⚠ **别用 `uv run uvicorn main:app` 起后端**。uvicorn 在 win32 非子进程模式下硬编码 ProactorEventLoop，与 psycopg3 异步模式冲突，长文档分块批改跑到中途必炸；`run_backend.py` 会先切 Selector 事件循环再打补丁。首次启动自动建表。

之后：注册账号（自动带 4 个预置任务与通用规范）→「模型提供商」填一个可用的 Key → 工作台上传文档并处理。`MINERU_TOKEN` 未配置时，扫描件 PDF / PPT / md 转换的对应上传会被明确拒绝而不是半途失败。

## 测试与检查

用例数以实际跑出的为准，文档不抄快照数字。

```bash
cd server
uv run pytest -q              # 全量单测：不碰 DB 也不出网（HTTP 用假 client、DB 用内存 SQLite）
uv run ruff check .           # lint；uv run ruff format --check . 查格式
uv run python test_e2e.py                  # 端到端冒烟：需后端在 8000 已起
uv run python test_e2e_structural.py       # 结构化批改 + PDF + 分块（零真实 token；分块节需后端 LONGDOC_CHUNK_CHARS=60）
uv run python test_e2e_rag.py              # 规范库→检索→注入（需本地 embedding 端点在跑）
uv run python test_e2e_mineru.py           # 扫描件 OCR + md 转换：脚本自起后端，需假 LLM 19000 + 假 MinerU 19002
uv run python test_e2e_ppt.py              # PDF→PPT 忠实转换：脚本自起后端，需假 MinerU 19002 + 假 VLM 19001

# e2e 用到的假端点（各开一个终端）：
uv run uvicorn test_llm_server:app --port 19000
uv run uvicorn test_vlm_server:app --port 19001
uv run uvicorn test_mineru_server:app --port 19002

# 评测台
uv run python -m eval.retrieval            # 规范召回 recall@k，纯本地、零 token
uv run python -m eval.runner --provider 5 --case clean   # 批改质量：⚠ 会调真实模型花钱，先单条

cd ../client
npm run build                 # vue-tsc 类型检查 + 构建（唯一的类型门禁）
npm test                      # 前端用例（vitest，只测纯逻辑）
```

## 文档

- [SPEC.md](SPEC.md) — 设计指南：Java→Python 对照、各阶段设计与交付、DDL、API 表、缺陷修复台账
- [AGENTS.md](AGENTS.md) — 编码智能体须知：项目约定、安全底线、已知陷阱与当前基线

## 还没做

限流、API Key 字段级加密、Alembic 迁移、多 worker 部署、docx/PDF 表格提取进批改正文、加密 PDF 解密、把问题清单写进交付 docx、长文档分块路径的人工确认分块方案、`checkpoints/` 体积治理与 TTL。详见 AGENTS.md §7。
