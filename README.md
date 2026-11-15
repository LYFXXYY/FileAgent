# FileAgent

文档 AI 处理平台。上传文档，按自定义 Prompt 调用大模型处理，下载结果文档。

批改类任务（`is_grading`）有两层产出：一是**红字 diff 标注**（新增标红、删除标红加删除线，文末另附一份可直接复制的纯净全文），二是**可定位的问题清单**（类型 / 原文 / 建议 / 原因 / 置信度），前端点卡片即可跳到原文高亮处。

> 平台定位已经放开到"文档"：**输入支持 `.docx` 与文字型 `.pdf`**，输出统一是 `.docx`。扫描版 PDF（没有文字层）暂不处理，会直接提示；PDF 里的表格不进正文。

## 功能

- **账号与数据隔离**：注册 / 登录（JWT），任务、模型提供商、操作日志全部按用户隔离
- **模型提供商**：任意 OpenAI 兼容端点（豆包 / 通义 / DeepSeek / GPT / Ollama），网页上配 `base_url + api_key + model_name` 即可换模型，API Key 只回显掩码；每个提供商标 `text / vision / embedding` 能力，用途不匹配时直接报错而不是偷偷换模型
- **调用计量**：每次外呼记 token / 耗时 / 重试次数 / 用途到 `llm_calls` 表，成本可追溯
- **任务模板**：自定义 Prompt，用 `{{content}}` 占位文档正文；可指定提供商、可标记为批改类
- **多格式输入**：`.docx` 与文字型 `.pdf` 走同一份"按行分隔的自然段"提取契约——PDF 侧会把被排版切成多行的段落拼回原样、剔掉页码与跨页重复的页眉装饰，所以 diff、原文定位、结果生成都不区分来源
- **三步处理**：上传 → 处理 → 下载。处理在后台执行、前端轮询状态，刷新 / 切页 / 断连都不丢进度
- **结构化批改**：问题清单按原文逐字匹配定位，匹配不到就标「未定位」，绝不把批注指到错位置
- **操作日志**：分页 + 状态筛选 + 文件名搜索 + 批注条数，失败可重试，完成可重新下载

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI · SQLAlchemy 2.0 · PyMySQL · python-docx · PyMuPDF（PDF 提取）· httpx · bcrypt + python-jose |
| 前端 | Vue 3（`<script setup>`）· TypeScript · Vite · Element Plus · Pinia |
| 数据库 | MySQL 8，5 张表：`users` / `model_providers` / `tasks` / `operation_logs` / `llm_calls` |
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

首次启动自动建表。之后：注册账号 → 「模型提供商」填一个可用的 Key → 「任务」建一个 Prompt 模板 → 回工作台上传 `.docx` 或 `.pdf` 并处理。

## 测试与检查

```bash
cd server
uv run pytest -q            # 110 个用例（diff / word / pdf / llm 传输层 / 结构化批改），不碰 DB 和网络
uv run ruff check .         # lint
uv run python test_e2e.py   # 端到端冒烟：需后端已在 8000 运行，会注册临时账号造数据

# 结构化批改 + PDF 输入的端到端（零真实 token，需另起一个假 LLM 端点）
uv run uvicorn test_llm_server:app --port 19000    # 另开终端
uv run python test_e2e_structural.py               # 40 项断言

# 批改质量评测台（合成夹具 + 确定性指标）：⚠ 会调真实模型、产生 token 费用
uv run python -m eval.runner --provider 5 --case clean   # 先单条验通链路
uv run python -m eval.runner --provider 5                # 再跑全部 5 条，落 eval/runs/

cd ../client
npm run build               # vue-tsc 类型检查 + 构建
```

## 文档

- [SPEC.md](SPEC.md) — 设计指南：Java→Python 对照、模块实现要点、DDL、API 表、缺陷修复台账
- [AGENTS.md](AGENTS.md) — 编码智能体须知：项目约定、安全底线、已知陷阱与当前基线

## 说明

`server/uploads/`、`server/outputs/`、`.env` 均不入库。当前阶段未做限流、API Key 字段级加密、Alembic 迁移、多 worker 部署、扫描版 PDF 的 OCR 与表格提取，详见 AGENTS.md §7。