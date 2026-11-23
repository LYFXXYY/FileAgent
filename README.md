# FileAgent

**文档 AI 处理平台**：上传文档 → 用你自己写的一段 Prompt 定义要做什么 → 下载结果。
批改、润色、摘要、翻译、转 Markdown、把 PDF 整成可编辑 PPT，走的都是同一条链路。

![CI](https://github.com/LYFXXYY/FileAgent/actions/workflows/ci.yml/badge.svg)

- 输入 `.docx` / `.pdf`（文字型直接提取，扫描版走云端 OCR）
- 产出 `.docx`（红字 diff + 可定位问题清单）/ `.md` / `.pptx`
- 每个用户一套独立的模型配置、任务模板、规范库与操作日志

---

## 它做什么

| 能力 | 输入 | 产出 | 是否调用模型 |
|---|---|---|---|
| 批改 / 润色 / 摘要 / 翻译 | `.docx` `.pdf` | `.docx`（改动处红字标注，问题清单可点击跳原文） | 文本 LLM，必需 |
| 长文档批改 | 超过 6000 字的文档 | 同上，位置仍是全文偏移 | 自动切块并行，**重试只补没成功的块** |
| 条款召回（规范库） | 你上传的校对规范 | 批改结果带条款号可溯源 | 本地 embedding + pgvector，混合召回 |
| 文档转 Markdown | `.docx` `.pdf` | `.md`（带图打包 zip） | Word 本地转换零调用；PDF 走版面识别 |
| PDF 转 PPT | `.pdf` | 可编辑 `.pptx`（文字进文本框、表格进原生表、图片原位贴图） | 版面块驱动渲染；坏块与版面问题才用视觉模型 |
| 扫描件 PDF | 无文字层的 `.pdf` | 与文字型同一条批改链路 | 云端 OCR 兜底 |

## 30 秒跑起来

```bash
# 1. 数据库（pgvector 镜像，端口 5434）
docker run -d --name fileagent-postgres -p 127.0.0.1:5434:5434 \
  -e POSTGRES_USER=fileagent -e POSTGRES_PASSWORD=*** -e POSTGRES_DB=file_agent \
  pgvector/pgvector:pg16

# 2. 后端
cd server
cp .env.example .env          # 必填 DATABASE_URL、JWT_SECRET_KEY、API_KEY_ENC_KEY
uv sync
uv run python run_backend.py  # http://localhost:8000/docs  Swagger

# 3. 前端（另开终端）
cd client
npm install
npm run dev                   # http://localhost:5241
```

⚠ 后端必须走 `run_backend.py`，不能直接 `uvicorn main:app`——Windows 上 uvicorn 硬编码
ProactorEventLoop，与 psycopg3 异步模式冲突，长文档分块路径会直接报错；这个入口负责切
Selector 循环并打补丁。首次启动自动建表。

## 架构

```
浏览器 (Vue 3)
   │  同源 /api
   ▼
FastAPI 单体  ── api/v1（路由）→ services（业务）→ models（ORM）
   │                                   │
   │ 同步 psycopg2（业务链路）           │ 异步 psycopg3（LangGraph 检查点 / 向量检索）
   ▼                                   ▼
PostgreSQL + pgvector          外部服务：OpenAI 兼容 LLM / VLM、MinerU 云 OCR
```

几个刻意的设计选择：

- **模型不碰坐标**。批改定位靠模型逐字摘出原文片段，位置由后端在原文上精确匹配算出来；匹配不到就标「未定位」，绝不把批注指到错位置——让模型报行号/字符偏移，错了没有任何信号。
- **降级 ≠ 重试**。结构化输出的格式降级只发生一次（严格 JSON → 宽松 JSON → 不带格式），解析失败直接报错而不是重打一遍；同一份提示再打一次大概率还是坏 JSON，白烧一倍 token。
- **计量是旁路**。记账失败绝不把已经成功的处理判成失败；按页计费的外部服务没有 token 数字，就不编一个数塞进表里。
- **失败必须可行动**。用户看到的是一句人话加一个错误编号（`E-LLM-TIMEOUT`、`E-DOC-EMPTY`…），上游报文、URL、框架内部结构只进日志和计量表，不出现在界面上。
- **零 LLM 是硬约束而不是优化**。转换类任务不解析 provider、不写计量行，有单测钉住；唯一的例外是"Word 正文全在图片里"时用视觉模型逐图转写——否则用户拿到的是一份空 Markdown。

## 功能特性

**账号与数据**
- 注册 / 登录 / 自助改密，JWT 鉴权；任务、模型提供商、操作日志、规范库全部按用户隔离，越权一律 404
- 注册即开箱可用：预置 4 个常用任务 + 一份通用校对规范
- 管理员可把某个模型设为公共并逐个授权用户；系统固定只有一个管理员，界面与接口层双重封死

**文档处理**
- 三步链路：上传 → 后台处理 → 下载；处理立即返回，前端轮询状态，切页/刷新不丢进度
- 结构化批改：问题类型开放输入 + 后端归一化，换模型不会整片报错
- 长文档自动分块并行批改，按块缓存结果，重跑不重复计费
- 结果文件名遵循 RFC 5987，中文文档名不乱码

**检索与规范库**
- 按条款边界切块入库（一块一条款，切完自查"拼回原文逐字相等"）
- 向量 + 字二元组词法双路召回，RRF 融合；任一路失灵都还能召回
- 召回的条款随结果落库（`refs` 带条款号），"没建库 / 查了不相关 / 查不了"三种状态在界面上分得开

**转换**
- Word → Markdown 本地完成，免费、毫秒级
- PDF → Markdown / PPT 走版面识别；PPT 渲染以"能编辑"为目标，超长段落按连续子串分页、超高表格按行拆原生表
- 版面检查循环：只调版面不改文字，被点名的整页幻灯片改走重排

**运维与可观测**
- 每次外呼记 token / 延迟 / 重试次数 / 用途，成本可追溯
- 上传体积、PDF 页数、每人每天处理次数、每日注册上限四道闸
- 磁盘产物与图状态按留存期自动回收（含孤儿文件与孤儿线程），可挂定时器
- 运行日志落盘轮转（业务日志 + 访问日志），反代侧另有访问记录

**安全**
- 模型 API Key 字段级加密存储（Fernet），页面只回显前 4 位
- 用户填的模型地址做 SSRF 防护：解析成 IP 后拒绝指向内网 / 环回 / 保留地址
- 口令强度校验、按 (IP, 用户名) 的登录失败锁定
- 反代层统一安全响应头（CSP / nosniff / X-Frame-Options / Referrer-Policy）

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI · SQLAlchemy 2.0 · psycopg2 + psycopg3 · LangGraph · python-docx · python-pptx · PyMuPDF · httpx · bcrypt + JWT |
| 前端 | Vue 3 `<script setup>` · TypeScript · Vite · Element Plus（按需引入）· Pinia |
| 数据 | PostgreSQL 16 + pgvector（业务 5 表 + 规范库 2 表 + LangGraph 6 表） |
| 工具链 | uv（Python 3.12）· ruff · pytest · vitest · vue-tsc · GitHub Actions |

## 项目结构

```
server/
  api/v1/        路由：auth / providers / tasks / process / logs / specs / admin
  services/      业务：llm 传输层、批改与定位、分块图、PDF/Word 提取、MinerU、检索、加密、回收
  models/        ORM（13 张表）
  schemas/       Pydantic 出入参与校验
  graphs/        LangGraph 长文分块批改图
  tests/         单元测试（内存 SQLite + 假 client，不连库不出网）
  test_e2e_*.py  五套端到端回归（假 LLM / VLM / MinerU 端点，零真实 token）
  migrate_*.py   无 Alembic 下的幂等改表脚本（一次性执行，保留作范例）
client/src/
  views/ components/ stores/ api/ utils/ composables/
deploy/          Caddy 配置与启停脚本
docker-compose.yml + .env.compose.example   单机容器编排（后端 + Postgres + Caddy）
```

## 测试与质量门禁

```bash
cd server
uv run pytest -q                 # 单元测试：500 余条，不连 Postgres、不出网
uv run ruff check . && uv run ruff format --check .
uv run python test_e2e.py                # 端到端冒烟（需后端已起）
uv run python test_e2e_structural.py     # 结构化批改 + PDF + 分块
uv run python test_e2e_rag.py            # 规范库入库 → 检索 → 注入
uv run python test_e2e_mineru.py         # 扫描件 OCR + md 转换（脚本自起后端）
uv run python test_e2e_ppt.py            # PDF→PPT 忠实转换（脚本自起后端）

cd client
npm run build                    # vue-tsc 类型检查 + 构建（唯一的类型门禁）
npm test                         # 前端单测（vitest）
```

CI 在每次 push / PR 跑后端 lint + 单测、前端类型检查 + 构建 + 单测。端到端需要后端进程、
假端点与真实 Postgres，因此留在本地执行。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/register` · `/login` · `/change-password` | 注册、登录、自助改密 |
| GET | `/api/v1/auth/me` | 身份快照（启动时刷新角色与授权） |
| CRUD | `/api/v1/providers` | 模型提供商（密钥只回显掩码） |
| CRUD | `/api/v1/tasks` | 任务模板（Prompt + 输出格式 + 是否批改） |
| POST | `/api/v1/upload` | 上传并提取正文 |
| POST | `/api/v1/process/{id}` | 触发处理（立即返回，后台执行） |
| GET | `/api/v1/process/{id}/status` | 轮询状态、问题清单、条款引用 |
| GET | `/api/v1/download/{id}` | 下载结果（`.docx` / `.md` / `.pptx` / 含图 `.zip`） |
| GET | `/api/v1/logs` | 操作日志（分页 + 状态筛选 + 文件名搜索） |
| — | `/api/v1/specs` | 规范库入库与删除 |
| — | `/api/v1/admin` | 角色与默认模型授权（仅管理员） |

## 部署

- **局域网试用**：`npm run dev` 或 `npm run build && npm run preview` 都已绑 `0.0.0.0`，
  后端保持只绑环回即可（访客的 `/api` 由前端代理在本机转发）。同 WiFi 的人访问
  `http://<本机IP>:5241`。
- **加密入口**：`caddy run --config deploy/Caddyfile --adapter caddyfile`，
  访客用 `https://<本机IP>:8443`；证书由 Caddy 内置 CA 签发。
- **单机容器**：`cp .env.compose.example .env` 填好密钥 →
  `docker compose --profile build run --rm build-frontend` 生成前端产物 →
  `docker compose up -d`。`docker-compose.yml` 顶部有首次上线的推荐顺序，照着逐步确认。
- **关键配置**（完整清单见 `server/.env.example` 与 `server/config.py`）：
  `API_KEY_ENC_KEY`（丢了库里所有模型密钥不可恢复，务必单独备份）、
  `MAX_PROCESSES_PER_DAY`、`MAX_REGISTRATIONS_PER_DAY`、`MINERU_TOKEN`、`EMBEDDING_*`、
  `RETENTION_DAYS`、`JWT_EXPIRE_HOURS`。
