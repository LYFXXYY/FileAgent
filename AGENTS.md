# AGENTS.md — FileAgent

本文件只记录**本项目特有、读代码不容易立刻发现**的约定与陷阱。
设计背景、Java→Python 对照、完整 API 表、DDL、缺陷修复台账见 [SPEC.md](SPEC.md)（权威设计文档），此处不重复。

**引用代码位置的写法**：用符号名（`api/v1/process.py:_run_processing`、`schemas/grading.py:_TYPE_ALIASES`），**不写行号**。实测全文 16 处 `file.py:NN` 引用里抽查 8 处已有 4 处指偏——行号随任何一次改动腐烂，符号名不会。

## 1. 项目速览

文档 AI 处理平台：上传文档 → 按用户自定义 Prompt 调 LLM → 下载结果。**当前输入支持 `.docx` 与 `.pdf`**（派发表 `api/v1/process.py:TEXT_EXTRACTORS`，白名单由它派生）；文字型 PDF 走本地提取，**扫描件 PDF 在配了 `MINERU_TOKEN` 时于处理阶段走 MinerU 云 OCR 兜底**（见 §3.15），没配则照旧 400。**但这条只描述批改/编辑链路**：md 转换任务里所有 PDF（含文字型）都走 MinerU，见下方依赖表和 §3.16。批改类任务输出红字 diff 标注 + 可定位的问题清单。**结果格式由 `task.output_format` 决定：`docx`（默认，批改/润色/摘要等）/ `pptx`（② PDF→PPT 忠实转换，见 §3.17）/ `md`（① md 转换，零 LLM，见 §3.16）**——别再写"结果永远是 .docx"。
平台定位已不止 Word，但表格不进批改正文、OCR 不做本地推理——改文案前先认清这一点，别把 UI 写成"任意文档全能处理"。

**三个功能各自的依赖**（别按功能名猜，按这张表；判据在 `api/v1/process.py`）：

| 功能 | LLM | RAG（embedding + pgvector） | MinerU |
|---|---|---|---|
| 批改 / 编辑（`output_format=docx`） | 必需 | 仅 `task.is_grading` 时召回条款（转换类已由互斥排除）；长文**按块各查一次** | 仅扫描件 PDF：OCR 出的 markdown **剥成纯文本**当正文，交付物仍是 diff docx |
| PDF→PPT（`pptx`） | 不用文本 LLM；VLM 仅两处旁路——坏块修复（`ppt_repair`）与版面批评循环（`ppt_critic`），都要"有坏块/开启循环 且 配了 vision 提供商"（§3.18/§3.19） | 不用 | **同 md，所有 PDF（含文字型）都走**；产物喂块驱动忠实转换渲染器（§3.17） |
| md 转换（`md`） | **零 LLM 是硬不变式**（不解析 provider、不写 `llm_calls`） | 不用 | **所有 PDF（含文字型）都走**；`docx→md` 走本地 python-docx，零外呼 |

⚠ 扫描件 PDF 一律进 MinerU，但**只有 md / pptx 任务把 MinerU 版面产物当交付原料**；批改任务拿的是 `markdown_to_plain_text()` 之后的纯文本（守 §3.8 与 docx 同构的不变式），那份 markdown 只作为 structured 三件套落盘留存。

**批改类长文档**（> `LONGDOC_CHUNK_CHARS`，默认 6000 字）走 LangGraph 分块图扇出批改，见 §3.10；非批改类任务不分块。
前后端分离单体，当前阶段无消息队列、无 Redis；数据库走 Docker（`pgvector/pgvector:pg16` 容器）。

| 层 | 位置 | 技术 |
|---|---|---|
| 后端 | `server/` | FastAPI + SQLAlchemy 2.0；psycopg2（业务同步链路）+ psycopg3（LangGraph 异步 saver/store）；Python 3.12（`.python-version`）；uv 管依赖 |
| 前端 | `client/` | Vue 3 `<script setup>` + TS + Vite + Element Plus + Pinia |
| 数据库 | Postgres `localhost:5434/file_agent`（Docker `pgvector/pgvector:pg16`） | 业务 5 表 + 规范库 2 表（`spec_docs` / `spec_chunks`）+ LangGraph 6 表，共 13 张 |

⚠ **阶段 4 已完成（2026-11-16）**：业务表 + LangGraph saver/store + `spec_docs`/`spec_chunks` 都已就位，MySQL 退役，e2e 各套也已在 Postgres 上跑绿（清单见 §7.2）。Docker 容器名 `fileagent-postgres`，数据卷在 `D:\docker\fileagent-postgres`。LangGraph 表 6 张：`checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations` / `store` / `store_migrations`，由 `setup()` 自动建。**Windows 硬约束**：psycopg3 异步模式 + 默认 ProactorEventLoop 直接卡死，`main.py` 顶部已切 `WindowsSelectorEventLoopPolicy`；任何独立脚本若用 `AsyncPostgresSaver/Store` 必须自己加这段。

分层：`api/v1`（路由）→ `services`（业务）→ `models`（ORM）；`schemas` 是 Pydantic 出入参；`middleware/auth.py` 提供 `get_current_user` 依赖。
核心链路三段分离：`POST /upload` → `POST /process/{log_id}` → `GET /download/{log_id}`，状态存在 `operation_logs`（`pending → processing → done / failed`）。

## 2. 常用命令（均已实测）

后端一律先 `cd server`——pytest 的 `pythonpath=["."]` 和 `.env` 定位都以此为基准：

```bash
uv sync                                        # 装依赖（含 dev 组）
uv run python run_backend.py                   # 起后端（必须走这个入口，不能直接 uvicorn main:app，见下方 ⚠）
uv run pytest -q                               # 全绿；纯函数 + 假 client + 内存 SQLite + 合成 PDF，不连 Postgres 不出网（条数见 §7.1 的口径说明）
uv run ruff check .                            # 全绿
uv run ruff format --check .                   # 全部已格式化
uv lock --check                                # 依赖锁一致性
uv run python backfill_spec_embeddings.py --check   # 看规范库还欠多少向量（只读）
uv run python backfill_spec_embeddings.py      # 真编码回填：需 .env 的 EMBEDDING_* 指向在跑的端点
uv run python migrate_add_log_needs_ocr.py --check  # 看 needs_ocr / stored_filename 两列现状（只读）
uv run python migrate_add_fk_indexes.py --check    # 看两个 FK 索引在不在（只读，幂等）
uv run python check_doc_refs.py                 # 文档里的 `文件:符号` 引用有没有断链（只读，改完代码后跑）
LONGDOC_CHUNK_CHARS=60 LLM_TRUST_ENV_PROXY=false uv run python run_backend.py    # 跑 test_e2e_structural 用这套环境
uv run uvicorn test_llm_server:app --port 19000            # 假 OpenAI 文本端点（批改类 e2e 用）；文件里那套"幻灯片大纲"路由属已删除的阶段5链路，仅历史残留
uv run uvicorn test_vlm_server:app --port 19001            # 假 VLM 端点：② PPT 的坏块修复（[ppt-repair:]）与版面批评（[ppt-layout-thread:]）e2e 在用（test_e2e_ppt.py 第 9/10 节）
uv run uvicorn test_mineru_server:app --port 19002         # 假 MinerU 云端点（扫描件 OCR 兜底 e2e 用）
uv run python test_e2e.py                      # 端到端冒烟：需后端在 8000 已起 + 假 LLM 不用，见 §7.2
uv run python test_e2e_structural.py           # 结构化批改 + PDF + 分块端到端，零真实 token
uv run python test_e2e_rag.py                  # 规范库入库→检索→注入 端到端：后端 + 假 LLM + **本地 embedding 端点**三者在跑
uv run python -m eval.retrieval                # recall@k 三臂对拍（纯本地，零 token）；--modes hybrid,vector,lexical / --k 5
uv run python test_e2e_ppt.py                  # PDF→PPT 忠实转换端到端：**后端由脚本自起**（FA_PPT_E2E_PORT=8011，注入假 MinerU 环境），另需假 MinerU 19002 + 假 VLM 19001 在跑（第 9 节坏块修复）；不用假 LLM
uv run python test_e2e_mineru.py               # 扫描件 OCR 兜底 + md 转换端到端：**后端由脚本自起**（FA_PORT=8010，注入假 MinerU 环境），另需假 LLM 19000 + 假 MinerU 19002 在跑
uv run python -m eval.runner --provider 5 --case clean   # 评测台冒烟：**会真花钱**，先单条再全量
```

⚠ **Windows 上不能直接 `uv run uvicorn main:app` 起后端**：uvicorn 在 win32 非子进程模式下硬编码 ProactorEventLoop，与 psycopg3 异步模式（AsyncPostgresSaver/Store）冲突，长文档分块批改跑到第 10 节必炸 "Psycopg cannot use the 'ProactorEventLoop' to run in async mode"。**必须走 `uv run python run_backend.py`**——它会先切 Selector policy 再 monkey-patch uvicorn 的 `asyncio_loop_factory`。`--reload` 与本启动器互斥（reload 走子进程入口，不会重跑 run_backend.py 的策略补丁），需要 reload 时只能接受分块路径不可用。

⚠ **`test_e2e_structural.py` 第 10 节要后端带 `LONGDOC_CHUNK_CHARS=60` 启动**才会真的切块；用默认 6000 时那节 12 项断言会失败（不是回归，是预算没到分块门槛）。跑完 `server/checkpoints/` 会被脚本自动清理。

⚠ **8000 端口在本机可能"看不见地被占用"**：WSL 侧若有服务监听 8000，因 mirrored 网络，Windows `netstat` 查不到该监听，现象是 uvicorn 报 `[Errno 10048]` 而 `curl localhost:8000` 却能收到对方的 404。查证要跑 `wsl -e bash -lc "ss -ltnp | grep ':8000'"`。换端口时 `test_e2e_structural.py` 走 `FA_API` / `FA_MOCK_LLM` 环境变量，`test_e2e.py` 仍是硬编码 8000。

⚠ **`run_backend.py` 的 host 必须是 `["127.0.0.1", "::1"]` 双栈绑定**：只绑 `127.0.0.1` 时，浏览器和 Vite 代理解析 `localhost` 会先试 IPv6 `::1` 吃一次连接拒绝再回退，本机实测**每个请求凭空多 ~220ms**（health 端点 0.004s → 0.23s），这就是"前端界面加载慢"的真正根因——诊断法：同一端点分别用 `localhost` 和 `127.0.0.1` 计时，差值大就是这个坑。

前端 `cd client`：

```bash
npm install
npm run dev      # 5241（config 里钉死且 strictPort=true；5173 落在 Windows 保留段 5141–5240 内绑不上），/api 代理到 8000
npm run build    # = vue-tsc -b && vite build，唯一的类型门禁（无 eslint / prettier）
npm run preview  # 伺服 dist/，同样 5241；preview 不读 server.proxy，代理在 vite.config.ts 的 preview 块里另写了一遍
```

### 2.1 让别人也能连上（局域网试用，2026-11-22 实测）

```bash
# 后端额外绑到局域网网卡（不设 FA_HOSTS 时只绑环回，行为与单机自用逐字一致）
cd server && FA_HOSTS=127.0.0.1,::1,<本机局域网IP> uv run python run_backend.py
# 前端用构建产物伺服：vite.config.ts 的 preview 块已写 host: true，绑 0.0.0.0
cd client && npm run build && npm run preview
```
对方访问 `http://<本机局域网IP>:5241`。**两道实测出来的坎**：
1. **`MAX_PROCESSES_PER_DAY` 必须设**（默认 0 = 不限制）。每次处理都在花真钱（LLM 按 token、MinerU 按页），失败重试还会整条重跑，没这道闸一个人脚本化就能刷爆账单。
2. **Windows 防火墙按程序路径放行，不按端口**。本机实测：`D:\python\3.12\python.exe` 已有「公用」profile 入站规则（所以 8000 通），但 node 走的是 `D:\aiagent\app\nodejs\node.exe`，而规则只覆盖 `D:\aiagent\app\nvm\v24.15.0\node.exe` → **5241 被拦**。用端口规则最稳（不受 nvm 切版本影响），需管理员权限：
   `netsh advfirewall firewall add rule name="FileAgent preview" dir=in action=allow protocol=TCP localport=5241`
   查当前网络归类：`Get-NetConnectionProfile`（本例 WLAN 是 **Public**，所以只有「公用」profile 的规则生效）。

跑后端前需要 `server/.env`（不入库）：复制 `server/.env.example` 为 `.env`，填 `DATABASE_URL` 和 `JWT_SECRET_KEY`。
全部字段见 `server/config.py`；**新增字段必须同时补进 `.env.example`**。

## 3. 后端不可违反的约定

### 3.1 数据隔离（安全底线）

- **每个查询都带 `user_id == current.id`**，providers / tasks / logs / process 四个路由无一例外，新接口照抄。
- `user_id` 只取自 `Depends(get_current_user)`，**绝不接受请求体传入**（`api/v1/providers.py:create_provider`、`api/v1/tasks.py:create_task`）。
- 越权一律 **404 而非 403**，不暴露"资源存在但不属于你"（`providers.py:update_provider` / `delete_provider`、`tasks.py:update_task` / `delete_task` 里查不到就 404）。
- task 引用 provider 时校验的是**可用性**而不是只看归属：统一走 `services/llm.py:provider_usable_by`（自己的 provider，或被授权的公共 provider）。管理员功能落地后这里已不是 `provider.user_id == current.id` 的裸比较，改可见性规则看 SPEC §9.11。校验点：`tasks.py:create_task` / `update_task`、`process.py:process`。
- `api_key` **永不出现在响应里**：走 `ProviderResponse.from_orm_masked()`（前 4 位 + `****`）。LLM 错误响应体截断 500 字符，`error_message` 截断 2000。

### 3.2 删除顺序（否则外键约束 → 500）

必须**先置空引用再删**：

- 删 provider → 先 `Task.provider_id = None`（`api/v1/providers.py:delete_provider`）
- 删 task → 先 `OperationLog.task_id = None`（`api/v1/tasks.py:delete_task`）

模型侧**声明**了 `ON DELETE SET NULL`（`task.provider_id` / `log.task_id`）。阶段 4 起 Postgres 是新建库，外键规则按 ORM 一次建齐（实测 `pg_constraint.confdeltype` 8 条全部匹配，不再有"MySQL 老库 vs ORM 声明"这档差异）。应用层那次置空**保留**——单测仍走内存 SQLite（不会真的级联），删 provider 走应用层置空让单测和真库行为一致；同时如果未来引入 Alembic，这条路径也不依赖具体方言。
`llm_calls` 表的外键规则确实是 `SET NULL` / `CASCADE`。日志和计量靠 `task_name` / `provider_name` 快照在关联被删后仍可读。

### 3.3 处理必须后台化

`POST /process/{log_id}` **立即返回 processing**，真正干活在 `api/v1/process.py:_run_processing`：

- 必须用独立 `SessionLocal()`——请求级 `db` 在响应发出后就关闭了。
- **每一条失败路径都要 `_mark_failed()`**，绝不允许日志停在 processing（前端会无限轮询）。
- 只有 `pending` / `failed` 可触发；`GET /process/{id}/status` 纯只读，不会重复触发 LLM。
- `BackgroundTasks` 不持久化 → `server/main.py:lifespan` 启动时把僵尸 processing 重置为 failed。**该 reaper 只在单 worker 下正确**；上多 worker 前必须换成带超时阈值的 reaper 或任务队列。
- provider 一律走 `resolve_provider()`：task 指定 > 当前用户默认 active（按 id 升序 + 按 `capabilities` 筛）。指定的 provider 失效或能力不匹配要**显式报错**，不许静默 fallback 到别的模型。
- 请求里已对"task 指定了 provider"提前校验（`process.py:process` 调 `resolve_provider` 那几行），让这类错误在返回 processing **之前**就是 400，而不是后台跑一半才 failed。

### 3.4 配置读取

**新增配置项必须先在 `config.py` 的 `Settings` 里声明字段**，再用 `get_settings().xxx` 读。
pydantic-settings 只把 `.env` 注入 Settings 对象，**不写 `os.environ`** → `os.getenv("XXX")` 读 `.env` 永远拿到 None。CORS 曾因此成为死配置（SPEC §10 s10）。

### 3.5 其他后端事实

- 同步/异步约定：纯 DB 操作用 `def`（FastAPI 自动丢线程池），涉及外部 HTTP 才 `async def`。已知同步阻塞点用 `# noqa: ASYNC230` 并写明理由（`process.py` 里落盘与读回 PDF 那三处 `open(...)`）。
- 日志用 `logging.getLogger("fileagent...")`，**不要 print**（Windows GBK 控制台中文乱码）；独立脚本需 `sys.stdout.reconfigure(encoding="utf-8")`。root handler 已在 `main.py:_setup_logging()` 里配好，级别走 `LOG_LEVEL`。⚠ 那个 `basicConfig(force=True)` 不能省——`--reload` 会反复重导入模块，不清掉旧 handler 每行日志会打印两遍。
- `LogStatus` 是 `enum.StrEnum`，配 `Enum(..., values_callable=...)` 存 **value**（`"pending"`）而非 name；阶段 4 后 Postgres 用原生枚举类型 `logstatus`（`CREATE TYPE logstatus AS ENUM(...)`，由 `create_all()` 自动建）。改枚举要连带回归后端状态流转与前端字符串比较。
- 正文权威副本在 `operation_logs.original_content`，磁盘文件只是审计留存。两列声明已是 `Text()`——Postgres 的 TEXT 没有大小上限，原 MySQL 时代的 `with_variant(LONGTEXT, "mysql")` 已撤。`structured_output` 同样是 `Text`。
- 提取层两条硬边界：`extract_text_from_docx` **只读 `doc.paragraphs`**（不含表格/文本框），`extract_text_from_pdf` **不读表格、不做 OCR**——已知功能边界，不是 bug。细则见 §3.8。
- 上传白名单 = `TEXT_EXTRACTORS.keys()`（`.docx` / `.pdf`，`ALLOWED_EXTENSIONS` 由它派生），落盘名 `uuid4().hex + ext`。**有两份同名 `TEXT_EXTRACTORS`**：`api/v1/process.py` 那份是批改/处理链路（落盘 + 处理），`services/spec_storage.py` 那份是规范库入库链路（只切块存表，不落盘）。加格式时两处都要改，否则规范库会出现"上传得了，规范进不去"或反过来。
- 认证：直接用 bcrypt，**不要引 passlib**（1.7.4 与 bcrypt 4.1+ 不兼容），密码截 72 字节；JWT HS256，`sub` 是字符串化 user_id；`/auth/me` 只做一件事——启动时刷新 role/can_use_default 快照（AppLayout 挂载调用，失败静默）；无刷新 token。
- 下载中文文件名必须 RFC 5987：`filename*=UTF-8''{quote(...)}`（`api/v1/process.py:download`）。
- diff 用 `SequenceMatcher(autojunk=False)`——**关 autojunk 对中文是必须的**，否则"的/是/在"被当噪声导致结果不准（`services/diff.py:compute_diff`）。
- `create_diff_docx` 先 `strip_markdown` 再 diff，按 `\n` 重组段落，末尾分页追加一份无标注纯净全文。

### 3.6 LLM 传输层（`services/llm.py`）

职责边界先钉死：`chat()` / `chat_json()` **不碰 DB**（才能用假 client 单测，也不在 async 函数里阻塞事件循环）；需要 DB 的只有 `resolve_provider()`（只读）和 `record_llm_call()`（自建独立 session）。别把查询塞回 `chat()`。

- **重试白名单**：只重试网络错误 / 超时 / 429 / 5xx（见 `_RETRYABLE_STATUS`）。其他 4xx 一次就失败——密钥无效重试也不会变对。往集合里加状态码前先确认那个接口是幂等的。
- **降级 ≠ 重试**：`chat_json` 只在端点明确拒绝 `response_format` 时降一档（`json_schema → json_object → 不带`）。**输出解析失败直接报错，绝不再打第二次**——降级方向越来越松，同一份提示再打一次大概率还是烂 JSON，白烧一倍 token。这是写单测时才发现并改掉的，别"顺手"把它改回循环里 catch。
- **计量是旁路**：`record_llm_call()` 内部吞异常只 `logger.warning`。任何情况下都不能让记账失败把已经成功的处理判成 failed。
- **失败路径也要报 attempts**：`LLMError` 带 `.attempts`，`_meter()` 在 `except` 分支必须把它传进去。丢了的话计量里 `attempts` 永远是 1，看不出"重试到耗尽"和"一次就挂"的区别。
- **`record_llm_call()` 只接原始值，不接 ORM 对象**：它跑在 `run_in_threadpool` 里，跨线程共用同一个 Session 不安全，而且 commit 后属性会过期。调用方先把 `provider.id` / `provider.name` 取成普通元组再传。
- **并发闸是进程内的**：`asyncio.Semaphore(llm_max_concurrency)`，多 worker 下每进程各一个，不是全局上限——跟 §3.3 的 reaper 同一个单 worker 假设。
- **共享 httpx client 由 lifespan 管生命周期**：`main.py` 启动时预热 `get_client()`，让"代理环境变量非法导致构造失败"提前进启动日志，而不是等用户点处理才炸。`LLM_TRUST_ENV_PROXY=false` 是绕开 `NO_PROXY` 含裸 `[::1]` 时报 `InvalidURL: Invalid port` 的开关；**需要走系统代理访问境外端点时不要关它**。
- `chat()` 里 `except LLMError` 会重新包装以带上 attempts——新增 `LLMError` 抛出点时要确认 attempts 没被抹成默认 1。
- `capabilities` 是逗号分隔串，合法取值在 `schemas/provider.py` 的 `KNOWN_CAPABILITIES`。加新能力要同时改那里、前端 `Providers.vue` 的 `capabilityOptions`、以及新增的调用点。
- **思考开关只有一个入口**：`config.llm_enable_thinking`（`bool | None`）。**默认 None = 请求体里不带 `enable_thinking` 字段**——各家 OpenAI 兼容端点对未知参数的容忍度不一，默认发出去是风险；只有显式配置才注入。实测代价差极大：1117 字中文 PDF 走批改，qwen3.8-max 开着思考单轮 **240s / 10990 completion token**，关掉思考 **21s / 901 token**（约 7 倍便宜）。但同一份文档三次分别报出 13 / 6 / 11 条问题，**"关掉思考会不会漏检"目前无证据可判**，要结论得靠评测台同夹具跑重复轮次。
- **`provider.max_tokens` 对某些端点是死配置**：实测 qwen3.8-max 在 `max_tokens=4096` 下仍返回 10990 completion token（`finish_reason=stop`）。别把它当成本闸门用，成本只能从 `llm_calls.total_tokens` 事后看。
- **超时异常文案不要直接插值**：httpx 在 asyncio 门下抛出的 `ReadTimeout` 常常 `str()` 为空，`f"LLM 请求超时：{e}"` 会产出"LLM 请求超时：（共尝试 3 次）"这种冒号后空着的病句。统一走 `services/llm.py:_exc_text()`，空详情时退到异常类名。

### 3.7 结构化批改（阶段 1）

- **绝不要求模型输出坐标。** LLM 不数字符，让它给 `paragraph_index` / `char_offset` 会大面积错位且错位无信号，批注指到错位置比不指更糟。模型只逐字摘 `original` 片段，`start/end` 由 `services/grading.py:_find_unused_span` 在 `original_content` 上精确匹配；匹配不到留 `None`，前端显示"未定位"。改这条前先想清楚怎么验证错位——无法验证的东西不要交给模型。
- **同名片段依次认领不同出现位置**：`claimed` 区间列表保证多条相同 `original` 不会全指到第一处，且区间互不重叠。
- **解析失败必须降级而不是 fail**：端点吐自由文本、缺 `revised_text`、被围栏包裹都是常态。`parse_grading_output` 退回"整段输出当修订文 + `degraded=True`"，diff 下载行为与阶段 0 完全一致。用户的文档必须拿到一个结果。
- **`revised_text` 是结构化清单的前提**：缺它就降级，不去猜一份修订文。批改任务仍把修订文写进 `processed_content`，所以 `create_diff_docx` 一行都不用改。
- 问题类型是**开放输入 + 后端归一化**：`normalize_type()` 中英别名收进 8 个 canonical 值，认不出归 `other`。加枚举要同时改 `schemas/grading.py` 的 `_TYPE_ALIASES` 和前端 `TYPE_COLOR` / `TYPE_LABEL`。硬枚举会让换模型时整片报错。
- 单条意见不合契约只丢那一条，不连累整份清单。
- `structured_output` 为 NULL 表示非批改任务或历史数据；有值但 `degraded=true` 表示模型没守格式。**两者语义不同，`grading_degraded` 必须单独透给前端**，否则"没发现问题"和"没结构化"都显示 0 条，等于撒谎。**状态接口同样不能把 NULL 压成 `[]`**——`deserialize_issues(None)` 返回 `[]`，所以 `process.py` 的 status 响应必须写 `if done and log.structured_output`，否则非批改任务的工作台会冒出一句"批改完成，未发现问题"（2026-11-17 普通用户真机走查实拍）。
- 日志列表只带 `issue_count`，不带整份清单（`api/v1/logs.py:_to_response` 解析 JSON 取条数）。
- 计量 `purpose` 现在分 `chat` / `grading` / `grading_degraded`，评测台按它分组。新增调用点要传对的 purpose。
- 结构化只在 `task.is_grading == True` 生效，普通任务走 `chat` 不走 `chat_json`。

### 3.8 文档提取层（阶段 1.5）

- **PDF 的提取结果必须与 docx 同构**（`\n` 分隔的自然段）。diff、`_find_unused_span` 定位、`create_diff_docx`、前端 `buildParagraphs` 全押在"一行一段"这个约定上；PDF 交出"每视觉行一段"就会把半句话当段落、批注指到换行处。要改 `services/pdf.py` 先跑 `test_e2e_structural.py` 第 8 节，它直接断言两种来源提取出的字符串相等。
- **两遍拼接**：block 内按行拼（Word/LaTeX 导出件），block 间按几何邻接拼（幻灯导出件每行一个 block）。邻接合并三条同时成立才合：垂直间隙 ≤5pt、左边缘差 ≤3pt、上一段没以句末标点收束。**左边缘条件不能删**——它挡的是双栏排版里上下对齐的两栏，跨栏缝合比切段更糟。
- **拼接规则里 CJK 不加空格**是必须的：加了空格 `original` 片段与 `original_content` 就对不上，整条定位链路失效。
- **页码判据必须带位置条件**：纯数字/罗马数字 + 落在上 8% 或下 20% 留白带。只按内容判会把正文里的年份、条款号、表格序号当页码删掉（`2024` 恰好是四位数）。
- **跨页高频装饰用频次判，不硬编码字符串**，且三条护栏：文档 ≥3 页、文本 ≤20 字符、**带句末标点的一律放过**。丢内容的代价远大于留噪声。
- 失败一律 `PdfError` → 路由转 400（打不开 / 有打开密码 / 没有文字层 / 文字层过薄）。**扫描件两条判据抛子类 `ScannedPdfError`**——上传路由靠它分流 MinerU 兜底（§3.15），损坏/加密仍是父类，OCR 救不了它们，别混用。
  最后一种是扫描件判据的补集：整页是图片、只被 OCR 出一层图注水印的文档（实测一份 8 页护理指南只有 64 字符，却带着 16 张图），旧判据"有没有文字层"对它形同虚设，会被当正常文档送去付费模型。新判据三条同时成立才拦：页数 ≥2、每页平均字符 < `_MIN_CHARS_PER_PAGE`(80)、图片数 ≥ 页数——**带图这个条件不能省**，否则确实只有一两页短文的正常文档会被误杀。
- **扫描件绝不回空文本**——那会让用户以为文档本来就是空的。
- 依赖：`pymupdf` 2.x 把版面分析（`pymupdf-layout` → onnxruntime + numpy）和 Markdown 导出（`pymupdf4llm` → networkx + tabulate）做成了**硬依赖**。`services/pdf.py` 只用 `get_text("blocks")`，对这些零引用，所以 `pyproject.toml` 的 `[tool.uv] override-dependencies` 用"永不成立的 marker"把它们摘掉了（省下的体积主要是 onnxruntime + numpy）。**将来真要用版面分析时（阶段 5 已完成但用的是 WSL LibreOffice 渲染，没碰 `pymupdf-layout`），删掉那几行 override 就能装回来**，别把它当成已经付过的成本。包数以 `grep -c '^\[\[package\]\]' uv.lock` 现测为准。`import pymupdf`，**不要用 `fitz`**（2.x 里已弃用，会打 FutureWarning）。

### 3.9 评测台（阶段 2 最小版）

- **`None` 不是 0**。夹具没有某项期望（结构类夹具没有唯一正确写法）时指标返回 `None`，聚合时不进分母。写成 0 会把平均分整体拉低，看起来"变差了"其实是自己造的假信号。
- **类型期望按组写（`expect_type_groups`），组内任一命中即算覆盖**。别退回单值：的地得被判成"用词不当"不是漏检，硬要 `typo` 只会让评测台替模型裁决一个语言学上没有唯一答案的问题，结果是每换一家厂商红一次、还得重新解释为什么红。判对错靠 `recall` 和 `fix_rate`，类型只是给用户看的标签。
- **`fix_rate` 必须同时查"正确写法出现"和"错误写法消失"**。只查前者会被原文里本来就有的同形词蒙过去；只查后者会被"整段删了"蒙过去。这个指标专门用来抓"意见全对但修订稿原样退回"，是阶段 1.5 真机跑出来的缺陷。
- **裁判是旁路**：`--judge` 失败只在 run JSON 里记 `error`，不能把已经跑完的客观指标带走。同源自判系统性放水（实测：客观 `fix_rate=0.6667` 的一轮，裁判给 5/5 并说"全部已修正"）——所以确定性指标永远排在主观分前面。
- **单轮数字会漂**：同一份提示、同一份夹具，`recall` 两轮跑出 1.0 和 0.8333。5 条夹具只够看方向；要下"A/B 变好了"的结论，得加重复或加夹具，别拿一轮差值当结论。
- **加夹具必过 `test_every_case_is_self_consistent`**：期望的错处串必须真的在文档里、且与正确写法不同。写夹具时最容易把错处顺手改顺，于是 `recall` 永远是 0，评测台静悄悄变成测不出东西的机器。
- `eval/runs/` 已 ignore：那是本机数字，入库会让对比基线到处漂；要长期留的就抄进 SPEC §9.6。
- **`eval/runner.py` 是运维脚本，按 provider 主键直取、不做 `user_id` 隔离**。别把 §3.1 那条"每个查询都带 user_id"当成这里漏掉的 bug —— 那条约束管的是对外路由。
- 档位归因现在只在 `LLMResult.format_tier` + run JSON + INFO 日志里，**没有落库**（要加列就是改表，先问用户）。跨端点长期对比需要它进 `llm_calls`。

### 3.10 长文档分块批改（阶段 3 · LangGraph）

- **只有批改类分块**。摘要/翻译/润色超长时仍走单发：它们的产出要对整篇负责，逐块做完再拼只会得到 N 份局部结果。要真做长文摘要得"逐块摘要 + 整篇汇总"两段式，那是另一个功能，别在这里顺手加。
- **分块三条不变式**（`services/chunking.py`，改它必跑 `tests/test_chunking.py`）：块文本 == 原文连续切片、块不重叠且中间只隔空白、`reassemble(source, chunks, [c.text…]) == source`。任何一条破了，全局偏移和修订文都是错的，而且没有信号。
- **全局偏移必须成对平移**：`start` 和 `end` 一起加 `chunk.start`。只移 `start` 时第 1 块是恒等偏移，测试要到第 2 块才发现区间偏短——这类"第一块恰好对"的 bug 最会伪装。
- **reducer 不能是 `operator.add`**：同一条日志点第二次"处理"会把 3 块累加成 6 段，`reassemble` 直接报错。要按块序号 upsert（`_upsert_results`）。
- **省钱的根据是按块 `SqliteStore`，不是 checkpoint**。检查点按 super-step 落盘，扇出里一块抛异常会整步写入作废，实测重跑时所有块都重打。所以 `grade_chunk` 开头先查缓存、成功后立刻写缓存。
- **命中缓存的块不写 `llm_calls`**：本轮没发生外呼，记账就是重复计成本。`ChunkCall.cached` 就是为这个字段存在的，别在调用方丢掉。
- 缓存键是 `(thread_id, chunk_index)`，但**命中还要求块文本 + 指令摘要 + 模型名三者一致**。换 prompt 或换提供商还在吃旧结果，那种"看起来变好了"最难查。
- **节点不查库**：provider 由路由解析后闭包传进 `_build_graph`；状态里只放可 msgpack 序列化的值（塞 ORM 对象检查点直接写不下去）。这条是 §3.6"传输层不碰 DB"边界的延续。
- `SqliteStore` 构造函数要的是 `sqlite3.Connection`，**路径要走 `SqliteStore.from_conn_string()` 上下文并显式 `setup()`**；传字符串会炸在 `create_function` 上。图状态与按块缓存分两个 sqlite 文件（同文件两个连接会撞库锁）。
- **别加图级 `RetryPolicy`**：`chat()` 已按白名单重试，再套一层就是同一个提示打两次、双倍烧钱（同 §3.6 的"降级 ≠ 重试"）。
- 假 client 在扇出下**必须按请求内容路由**（见 `tests/test_grading_graph.py` 的 `_RouteClient`）：按脚本顺序吐响应会把 A 块的答复发给 B 块，测试通过只是在测运气。
- `checkpoints/` 已 gitignore、只增不自缩，属定期清理项；`interrupt` 人工确认分块方案还没做（要先有 UI）。

### 3.11 PPT 流水线（阶段 5 · 已整体删除，历史）

⚠ **2026-11-21 整条大纲流水线已按用户决定删除**：`graphs/ppt_graph.py` / `services/pptx_builder.py` / `schemas/ppt_outline.py` / `schemas/vlm_critique.py` 及其 5 个单测已 `git rm`（历史可从 git 找回）。现役 pptx 链路是 §3.17–§3.19 的忠实转换；其中仍有效的机制已迁到：
- **`render_pptx_to_images(pptx_bytes, slide_count)` 双后端派发点保留**（`services/pptx_renderer.py`，片3 版面循环在用）。`settings.ppt_render_backend`：`stub`（默认）/ `soffice`（WSL LibreOffice 真渲染）。**Windows↔WSL 文件交换必须走 `/mnt/<盘>` NTFS 挂载，禁用 `\\wsl.localhost` UNC**（见 SPEC §9.13 运行约定 2）。
- **轮次语义**（批评调用数、默认 2 硬上限 5、pass 早停）与 **"解析失败绝不当通过"** 的边界已原样继承到 §3.19。
- **VLM 走 `resolve_provider(user, None, "vision")`、不接受 provider_id 直传**（§3.3 capability 路由的延伸）——现役两处 VLM 都守这条。
- 阶段 5 的 `outline` / `critic` 等 purpose 取值不再产生新行；现役新增的是 `ppt_repair(_degraded)` 与 `ppt_critic(_degraded)`。

### 3.12 规范库向量层（阶段 6 · A1）

- **embedding 端点没有默认值**：`EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` 留空即报"未配置"。本机 LM Studio 端口随上次启动漂移，写死一个"看着能用"的默认值换来的是静默打到别的模型上（维度还刚好一样的话完全无信号）。维度三处必须一致：`models/spec_doc.py:EMBEDDING_DIM`（列宽的唯一权威）、`EMBEDDING_DIMENSIONS`（配置）、模型实际输出；`_check_config()` 在前两者不等时拦，第三处由响应维度门禁拦（实测 768 维回来直接报错，不让它进 1024 的列）。
- **查询前缀只在查询端加**：bge 检索是非对称的，GGUF 元数据里没有任何前缀信息，只能应用侧拼（`embedding_query_prefix`）。`embed_texts()` 永远不加——文档端也加的话入库向量全变成"查询表示"，相似度整体下降但不报错。
- **编码不写 `llm_calls`**：该端点 `usage.prompt_tokens` 恒 0，写进去的就是编造的数字。成本代理用字符数（`sum(len(chunk.text))`，库里本来就有）。
- **入库时编码失败 = 整份失败**：规范库只有 `ingest` / `delete`，没有 UPDATE 口子。"文本进了、向量 NULL"补不回来，而且检索会静默排除这些块、列表页却显示已入库。所以 `_embed_for_ingest()` 在写库**之前**跑，失败抛 `SpecStorageError` → 400。`SPEC_EMBED_ON_INGEST=false` 是给"先建库后接端点"留的口子，此时块为 NULL，靠 `backfill_spec_embeddings.py` 补。
- **编码函数是同步的**：入库路由本身是 `def`（FastAPI 丢线程池），阻塞最自然；异步侧要用它得 `run_in_threadpool` 包（同 §3.3 的 `record_llm_call()`）。`httpx.Client(trust_env=False)` 是刻意的——端点固定在 127.0.0.1，走系统代理只会绕远，且本机 `NO_PROXY` 含裸 `[::1]` 时 httpx 直接抛 InvalidURL（§3.6 那个 `LLM_TRUST_ENV_PROXY` 开关是给要翻代理的聊天端点用的，这里不需要配置项）。
- **不重试、不降级**：编码幂等，重跑比在库里留半成品便宜。别把 §3.6 的聊天重试白名单搬过来。
- **回填刻意不分页也不建异步任务**：实测 66 块一批 2.17s、全库 26s 含启动。批量按 32 条切段只为兜住"上千块"的情况，不是性能调优。
- **仍然不建 ANN 索引**：见 §7 阶段 6 前置约束那条，pgvector 近似索引带 `user_id` 过滤会静默少返回 top-k。回填后 66 块全部有向量，检索走精确余弦扫描。

### 3.13 规范检索与注入（阶段 6 · A2–A4）

- **隔离压在 SQL 里**：`services/spec_retrieval.py` 的 `_VECTOR_SQL` 用 `JOIN spec_docs` 带 `user_id`，不是"先查 doc_id 再 IN"。后者漏掉 JOIN 就等于跨用户读别人的规范，而且列表会变长。
- **`embedding IS NOT NULL` 必须写进 WHERE**：NULL 参与 `<=>` 排序得到 NULL score，位置不可解释——宁可少召回不要乱序。
- **滑窗查询是必须的**：bge 静默截断（见 §3.12 坑 2），整篇一次编码等于只拿开头去检索。超 `spec_query_max_windows` 时等步长抽样，**并且首段和末段都必须落进窗口**——`int(i * step)` 那种取法末段永远进不来（10 段抽 4 个只到第 7 段），文档尾巴的条款静默查不到。
- **条款号只认行首**：交叉引用（"参见 3.1.6"）、行距（"1.5 倍"）、年份都长成 x.y 形状，不锚定 `^` 就会把引用读成本块编号，而引用编号错了在结果里看不出任何异常。
- **查询前缀默认是空串**，这条**推翻了 §7 里那条旧记载**：bge-m3 训练时不带指令（带指令的是 bge-large-zh 那一批）。实测 9 条夹具 k=5：加前缀 recall@5=0.380 / hit=0.778，不加 recall@5=0.491 / hit=0.889。换模型时按该模型的训练方式配 `EMBEDDING_QUERY_PREFIX`，别再照"非对称就要加前缀"的通用说法默认回去。
- **注入进 user 不进 system**：条款是内容不是契约（§3.7 那条边界）。混进 system 模型会把输出格式一起改写。
- **注入必须带"不适用就不要硬套"**：否则模型给每条意见都挂一个不相干的条款号，引用看起来更权威、实际更假。
- **引用落在 `structured_output` 信封里**（`refs` / `spec_note`），不加新列、不改 `llm_calls`。**没用规范库时信封形状与阶段 6 之前逐字一致**（不写空 `refs` 键）——否则"没用规范"和"用了但没引用"在数据上分不开。
- **三种"没有"互不混淆**（同 §3.7 的 NULL vs `[]`）：没建库/开关关掉 → `refs=[]` 且 `note=None`；查了不相关 → 同上；**查不了** → `refs=[]` 且 `note` 有文案。前端 status 透 `spec_refs` / `spec_note` 两个字段。
- **检索是旁路**：`collect_spec_context` 内部吞掉一切异常返回"空上下文 + note"，批改照跑。编码端点挂了不该把已能工作的批改判成 failed。
- **分块路径按块查询，不是整篇查一次**（A6 已落地）：给每块注入同一份全局 top-k 等于只按开头几段检索（bge 还会静默截断长输入），而且长文档各段涉及的条款不同。查询能力以 `spec_lookup` 闭包传进 `_build_graph`——节点不查库（§3.10 那条边界在这里的延续），闭包里每次自开独立 session（并发跑块，跨线程共用 session 不安全，同 `record_llm_call`）。**命中按块缓存的块不再查询，但 `refs` 随结果一起存进缓存**，否则"重跑一次引用就消失了"。
- **`test_e2e_rag.py` 开机先探 embedding 端点**：缺了直接退出（返回码 4），不让断言在"没向量"的世界里一片红。假 LLM 端点新增 `GET /v1/_mock/last`，用它证明"条款真的进了 prompt"——只看结果里的 refs 只能证明检索到了，证明不了送进去了。
- **recall@k 的量法**：`python -m eval.retrieval --k 5 --modes hybrid,vector,lexical`，只打本地编码、零 token。具体数字见 §3.14 那张表（那一层落地后重测的）。这层只量"该给的条款有没有进 prompt"，**和模型批得好不好是两件事**，别拿它当 RAG 有效性结论（夹具与规范文档都是 AI 生成的，见 §7 效度边界）。

### 3.14 混合召回（阶段 6 · A5，纯向量不够看之后加的）

起因是一次真机跑：58 字的短文，纯向量 top-5 召回的条款**五条全不相干**（模型靠自己还是批对了，等于 RAG 白注入）。实测三臂对拍：

| 模式 | recall@5 | hit@5 | recall@10 | hit@10 |
|---|---|---|---|---|
| `vector` | 0.491 | 0.889 | 0.583 | 0.889 |
| `lexical` | 0.843 | 1.000 | 0.926 | 1.000 |
| `hybrid`（默认） | 0.815 | 1.000 | 0.898 | 1.000 |

（上表是修好 `clause_id` **之后**的数。修之前是 0.491 / 0.722 / 0.722——差的不是检索，
是测量：第 0 块的条款号被读成 `doc3-#0`，期望 1.1.1 永远对不上。**度量本身也会带 bug**，
数字跳变时先怀疑量法。）

- **词法这一路是字二元组 Dice，不是分词**：镜像里没有 pg_jieba / zhparser，应用侧也不引分词库。它补的是"文档里出现了规范举过的错例"这类表面重合——正好是纯向量捞不到的那种。
- **⚠ 这批夹具结构性地偏向词法**：阶段 6 夹具的错例本来就是从那份规范里抄的，所以字二元组天然重合。`0.722` 只说明"这一层信号在本库里可区分"，**不是"词法优于向量"的世界性结论**；用户自己的文档不会照抄规范错例，那时靠的是向量那一路。
- **默认 hybrid 的理由是抗故障，不是召回最高**：融合会略低于纯词法（k=5 时 0.815 < 0.843），换来的是任一路失灵都还能召回——编码端点没起来时词法照常出条款，这是纯向量做不到的；反过来用户自己的文档不会照抄规范错例时，词法失灵、向量兜住。
- **加了词法臂之后，前缀配错不再致命**：实测 hybrid 带不带前缀 recall@5 都是 0.815，而纯 vector 从 0.491 掉到 0.380。词法那一路把名次差拉回来了，所以前缀这个坑现在只影响 vector 单臂模式。
- **融合用 RRF 不用分数相加**：余弦常见 0.4–0.6、Dice 常见 0.05–0.2，相加等于让大标尺那一路永远说了算，而且换个模型比例就变。
- **每路各取 k 条，不取 2k**：实测取 2k 时 hybrid@10 从 0.778 掉到 0.741——向量臂名次靠后也要占坑，把词法的高置信条款挤出去了。
- **`SpecHit.via` 必须带着**：两个分数标尺混在同一个 `score` 字段里，不标来源就没法解释。前端因此**不显示数字、只显示来源**（语义/错例/双路）；给用户看 0.05 会让他们以为那条匹配得差，而它其实是词法的最强命中。
- **消融靠 `SPEC_RETRIEVAL_MODE`**（`hybrid` / `vector` / `lexical`），`eval.retrieval --modes` 用它对拍。别把它当装饰开关。
- **`clause_id` 扫块头最多 4 行，遇 `【` 就停**：真实规范的第 0 块是"文档标题 / 第1章 错别字类 / 1.1.1 同音别字"，只看前两行读不出编号，引用会显示成 `doc3-#0` 这种定位符——实测模型会照着这个假编号写进 reason，用户看到的溯源就是错的。行首锚定 + 遇正文标记即停，两件事一起才既不漏也不误读。

### 3.15 MinerU 扫描件 OCR 兜底（2026-11-20，A–E 全落地）

- **触发范围只有扫描件**（用户拍板）：文字型 PDF / docx 一行代码都不走 MinerU。判据是 `ScannedPdfError`（无文字层 / 薄文字层两条），损坏、加密不在兜底范围。**`MINERU_TOKEN` 留空 = 功能关闭**，扫描件照旧 400，行为与接入前逐字一致。
- **OCR 在处理阶段跑，不在上传阶段**：上传是同步提取（要回 extracted_text 预览），MinerU 是分钟级异步流程，塞进上传会把请求挂死。所以受理时 `needs_ocr=True` + `original_content=NULL`，`_run_processing` 在 **provider 解析之前**先 OCR 写回，再进 LLM。没有新增状态枚举，前端轮询照旧。
- **`stored_filename` 列是 OCR 的前提**：处理阶段要按落盘名读回 PDF 字节。这顺带补上了"磁盘副本不可回溯"的历史缺口（§7），e2e 清理现在能精确删本次落盘文件。历史行该列为 NULL，OCR 路径遇到 NULL 直接 `_mark_failed("扫描件落盘记录缺失，请重新上传")`，不猜路径。
- **双轨输出**（SPEC §9.12 约定）：`markdown_to_plain_text` 产 plain_text 守 §3.8 同构不变式给批改/diff/定位；full.md / content_list.json / images 落 `outputs/mineru/{log_id}/` 给未来 RAG/PPT 消费，第一步不接 UI。**表格不进批改正文**（用户拍板）：`|` 行与 HTML 表格行（MinerU 常把整张表压成一行 `<table><tr><td>…`，只按纯标签行跳会漏）都整行跳过。
- **归一化只去标记不改字**：批改定位靠逐字匹配，多改一个字 `_find_unused_span` 就悄悄失效。公式按原样保留，代码围栏内容不去标记。
- **传输层哲学同 §3.12 embedding**：不碰 DB、不重试、不降级（幂等重跑比留半成品便宜）、**不写 `llm_calls`**（按页计费没有 token，编数字就是造假）。同步阻塞（含 `time.sleep` 轮询），async 调用方必须 `run_in_threadpool` 包。
- **structured 落盘是旁路**：`save_structured` 吞 OSError 只 warning，不能把已拿到正文的处理判成 failed（同计量旁路边界）。
- **云 v4 batch 协议细节**（2026-11-20 核实）：PUT 预签名地址**不带 Content-Type**（签名不含它）；业务错误是 HTTP 200 + code 非 0，两层都要查；zip 下载不带 Authorization；解包只取 basename，`..` 路径条目跳过（zip-slip）。token 有效期 90 天，过期表现为业务码报错。
- **status 接口 done 时回填 `original_content`**：扫描件上传时没有预览文本，前端高亮定位的底本靠它补（`Home.vue:applyResult` 只在 extracted_text 为空时回填，普通上传不重复覆盖）。
- **重试会重新 OCR**：`needs_ocr` 写回前失败则保持 True，重跑整份再来——MinerU 侧没有半成品状态要维护。

### 3.16 md 转换任务（①，2026-11-20，零 LLM）

- **`output_format='md'` 与 `is_grading=True` 互斥**：md 没有修订文，做不了红字 diff。创建时 pydantic 校验抛 422（`schemas/task.py`），更新时按**合并后的值**判（旧 is_grading + 新 md，或旧 md + 新 is_grading 都要拦）抛 400。前端 `Tasks.vue` 用 `watch(output_format)` 选中 md 即强制 `is_grading=false / provider_id=null / prompt_template='{{content}}'`，并 disable 批改开关/模型下拉/Prompt 框，与后端同向，避免用户填完才被打回。
- **两条来源两种成本**：docx→md 走 `services/docx_md.py` 本地 python-docx 转换（免费、零外呼、不需要 token）；**PDF→md 一律走 MinerU，文字型 PDF 也走**——因为要的是版面识别出的标题层级/表格/图片引用，本地 `get_text("blocks")` 给不出 md 结构。所以 md 任务的 PDF 上传**不要求** `MINERU_TOKEN` 也能受理 docx，但 PDF 没配 token 时上传即 400（fail-fast，别等处理才炸）。
- **零 LLM 是硬不变式**：md 路径不解析 provider、不写 `llm_calls`、`provider_name=NULL`。`_run_md_conversion`（`process.py`）独立于 `_run_processing` 的 LLM 分支。e2e 第 15 节专门断言 md 日志的 LlmCall 计数为 0。
- **正文双轨**：`processed_content` 存 markdown 原文（带 `#`/`|`/`![]()`）；PDF 路径的 `original_content` 存 `markdown_to_plain_text(markdown)`（去标记，供前端预览/未来定位），**docx 路径的 `original_content` 保持上传时提取的纯文本不动**。structured 三件套（full.md / content_list.json / images）照 §3.15 落 `outputs/mineru/{log_id}/`。
- **下载形态由有没有图决定**（`_build_md_download`）：有图片 → zip（`{stem}.md` + `images/`），无图 → 纯 `.md`（`text/markdown`）。文件名走 RFC 5987 `filename*=UTF-8''`，stem 取自 `original_filename` 去扩展名（中文要 quote）。前端 `Home.vue:doDownload` 从 Content-Disposition 解析文件名，不再硬编码 `result_xxx`。
- **真实云 zip 命名与假端点不同**（2026-11-20 探测核实）：content_list 带 UUID 前缀（`{uuid}_content_list.json` / `{uuid}_content_list_v2.json`），md 是裸 `full.md`，图在 `images/{sha256}.jpg`。`_unzip_result` 改用**后缀匹配**而非精确 basename，**v2 优先**（带 bbox），v2 损坏回落 v1。`test_mineru_server.py` 的假 zip 已镜像这套命名，e2e 才真的测到后缀匹配逻辑。
- **`NO_PROXY` 裸 `[::1]` 会让 httpx 抛 InvalidURL**：`config.py:normalize_no_proxy_env()` 剥掉它，`main.py` 启动与 `mineru._get_client` 都调。⚠ `httpx.InvalidURL` **不是** `httpx.HTTPError` 子类，except 要两个都接。⚠ Windows `os.environ` 大小写不敏感（NO_PROXY 与 no_proxy 同键），`test_no_proxy.py` 按 `sys.platform` 分支，小写键测试在 win32 skip。
- **MinerU 对拉丁字符串可能插空格**（实测合成文字 PDF："P DF 转 M ar k d o w n"，中文段落干净）：这是云端版面识别对该类输入的产出特性，我方管道如实透传，**不是转换缺陷**，别在 docx_md / ocr 层"顺手修"。

### 3.17 PDF→PPT 忠实转换（② 片1，2026-11-21，替换阶段5大纲路径）

- **定位是"转换"不是"修改"**：忠实搬运 MinerU 版面块，不重组、不改写、不摘要；产出以**可编辑**为目标（文字进文本框、表格进原生 PPT 表、图按位贴图）。阶段5 的"大纲 LLM 重组"路线已从处理链路摘除（§3.11）。片1 零 LLM 是硬不变式（不解析 provider、不写 `llm_calls`、`provider_name=NULL`、`structured_output=NULL`——重试时顺带清掉旧 ppt_outline 信封残留）。
- **输入只收 PDF**：pptx 任务上传 docx 直接 400；PDF 没配 `MINERU_TOKEN` 上传即 400（fail-fast，同 md 的判据）。与批改互斥（`schemas/task.py` 创建 422 / `api/v1/tasks.py:update_task` 合并值 400），前端 `Tasks.vue` 的 `watch(output_format)` 对 md/pptx 同向强制归位。
- **渲染器在 `services/ppt_convert.py:render_blocks_to_pptx(pages, images, page_sizes, *, mode="auto")`**，纯函数不碰 DB 不出网（可假块单测）。输入是 content_list **v2 页嵌套**形状：外层页列表、内层块列表，bbox **归一化 0..1000**（不是 pt！），真实页面宽高比靠 `MinerUResult.page_sizes`（layout.json）还原。`test_mineru_server.py` 的假 zip 已镜像这套形状（页嵌套 + layout.json + 合法 PNG——假 jpeg 字节会把 add_picture 炸掉）。
- **布局双模：B 阅读顺序重排为主、A 整页忠实兜底**（用户拍板："转换目的是做成能用的 PPT"）。`mode="auto"` 逐页分流：横向页（幻灯导出件）或密集页（视觉块归一化面积 ≥0.45 或块数 >30）→ A；其余 → B。阈值是对两份真实护理文档实测调出来的（最大 0.21 / 26 块），调它先重测。
- **B 流式完整性不变式**：超长段落 `_split_text_pieces` 切成**连续子串**（拼接逐字等于原文，不丢字不插字）；超高表格无跨格时按行拆成多张原生表（单元格全保留），带跨格则回落整表渲染图。**A 模压盖不变式**：bbox 互不重叠 + 字装进框（`_fit_font_pt`）→ 不压盖；行距用绝对 Pt（倍数行距会被 PowerPoint 再乘默认行高）。
- **流式图片两条实拍教训**：整高贴图会把图文拆成碎片页（真实指南 56 页）→ 限高 0.6 内容高；低分辨率 logo 放大到糊 → 放大上限 1.5pt/px（2 倍）。
- **噪声按分流模式分**：A 模横向页保留页眉页脚（那本来就是页标题/品牌标）、页码永远跳；B 模流式一律跳页眉页脚页码。
- **交付链路**：`_run_ppt_conversion` 处理时把二进制落 `outputs/pptx/{log_id}.pptx`，下载路由 `_read_pptx_delivery` 直接读（缺失 400"请重新处理"），文件名 `转换_{stem}.pptx`。片3 版面修订后就地覆盖这个文件，接口不变。structured 三件套照旧落盘（片2 坏块修复要拿区域原图 grounding）。
- **`process.py` 的 provider 预校验对 md/pptx 跳过**（转换不用文本 provider）；老 LLM 路径的 `critic_provider` 解析、`_render_outline_as_text`、`_run_model` 的 pptx 分支已删，`_error_purpose_for` 不再返回 `outline`。

### 3.18 PPT 坏块 VLM 修复（② 片2，2026-11-21）

- **只修两类确定性坏块**（`services/ppt_repair.py:find_defects`）：title/paragraph 文本为空、table 的 html 缺失或解析不出网格。**判据必须可机械判定**，"看着质量差"是片3 版面循环的范围，不在这里主观裁决。
- **防幻觉铁律：没有区域原图就不修**。`crop_region` 用 pymupdf 按 bbox（归一化 0..1000 × `page_sizes` 的 pt）从**原 PDF** 裁区域渲染 144dpi PNG 喂 VLM，提示词只许逐字转录；裁不出（页码越界/退化框）直接跳过该块，绝不"凭上下文补"。
- **产出必须过结构校验才替换**：表格要求 `<table>` 且 `parse_html_table` 出网格；文字要求非空。过不了 → `degraded=True` 保留原块，**绝不抛**（§3.7"用户的文档必须拿到一个结果"的延伸）。
- **装配走不可变**：`_apply_repair` 整块复制后替换 content 并打 `repaired` 标记；原 `result.content_list` 不动（单测钉着这条）。
- **修复是旁路增强**：没配 vision 提供商（`resolve_provider(user, None, "vision")`，不接受 provider_id 直传，§3.3 延伸）、VLM 挂了、产出烂了——一律保留片1 兜底继续交付，转换照 done。所以 §3.17 的"零文本 LLM"不变式仍然成立，只是 llm_calls 可能多出 `ppt_repair` / `ppt_repair_degraded` 行（归 vision provider；`log.provider_name` 仅在真发生过修复时写 vision 名，"无需模型"对无坏块文档继续成立）。
- **成本闸 `MAX_REPAIR_BLOCKS=20`**：坏块成串通常意味着整份解析失败，逐块烧 VLM 不如整体兜底；超上限只修前 20 个并 logger.warning。
- **渲染器负责透明度**：带 `repaired` 的块在产物里标注"LLM 修复"（B 模在块后追加小字注记，A 模在块框下方贴标签）。这是用户拍板的要求，不是装饰——删标记等于交付污染。
- **假端点契约**：`test_vlm_server` 按 `[ppt-repair:log-N]` 标记分派修复分支，且**请求里没有 image_url 直接 400**（把 grounding 契约钉在 e2e 层）；`test_mineru_server` 的 `defective` 模式吐含空段落 + 无 html 表格的 v2 页。`/v1/_mock/repairs` 计数供 e2e 断言"真打过 VLM"。

### 3.19 PPT 版面检查循环（② 片3，2026-11-21，只修版面不改字）

- **用户拍板的边界**：循环**只查/修版面，绝不改文字**。所以修订只有两个确定性旋钮（`services/ppt_layout.py:_apply_revisions`）：被点名的 A 整页忠实幻灯片 → 其源页强制改走 B 流式重排（重排天然消压盖/竖排堆叠）；被点名的 B 流式幻灯片 → 全局 `font_scale *= 0.85`（下限 0.7）。文字在任何路径下都没有被改写的口子——这是不变式，不是实现细节。
- **修订依据靠渲染元数据**：`services/ppt_convert.py:render_blocks_with_meta` 返回与幻灯片顺序一致的 `[{pages, mode}]`；批评给的 `slide_index` 靠它映射回源页/旋钮。**改渲染器分页逻辑必须同步维护 meta**（meta 长度 == 幻灯片数是硬约定，占位页也算）。
- **轮次语义同 §3.11**：`max_rounds`（配置 `PPT_LAYOUT_MAX_ROUNDS`，默认 2）指**批评 VLM 调用次数**，硬上限 5（超了 warning 后钳制）；pass 早停时实际只跑 1 次。末轮仍是 needs_revision 就交付当前版本，不再白烧一轮重渲染。
- **"没听清"绝不当前提通过**：批评产出解析失败 → `degraded=True` 记账 + 收手交付现版（继承 §3.11 边界）；批评外呼失败、渲染后端异常同理——**整个循环是旁路**，任何异常都不允许把已能交付的转换判 failed（process.py 里还有一层 Exception 兜底防循环自身出 bug）。
- **送样抽样有上限**：单轮至多 12 张，均匀抽样 + 首尾必送（`_sample_indices`）；thread 标记 `[ppt-layout-thread:log-N]` 跨轮稳定（假端点按它计数），轮次写在标记外。
- **计量**：`llm_calls.purpose` 新增 `ppt_critic` / `ppt_critic_degraded`（归 vision provider）。`log.provider_name` 在"修复或批评真发生过"时才写 vision 名；开关 `PPT_LAYOUT_ENABLED=false` 可整体关掉循环（只剩修复）。
- **e2e 走 stub 渲染**：`test_e2e_ppt.py` 的 start_backend 强制 `PPT_RENDER_BACKEND=stub`——测的是循环机制与计量归因，真 soffice 渲染留人工目检，不在 CI 路径烧 WSL 时间。假 VLM 的版面分支第一轮点名幻灯片 1（stacked）、第二轮 pass。
- 已知遗留（人工观察，非缺陷判据）：A 模窄框拉丁词竖排堆叠正是靠这个循环改走 B 重排来治；B 模表格样式（边框/底纹）仍未还原源文档。

### 3.20 注册默认配置（2026-11-21，用户拍板"注册时自动初始化"）

- `services/defaults.py:init_user_defaults` 挂在 `api/v1/auth.py:register` 建号之后：**4 个默认任务**（作文批改 grading / 文本润色 / PDF 转 PPT / 文档转 Markdown）+ **通用参考文档**（`fixtures/specs/文字编辑校对规范（教学示例稿）.docx` 走 `ingest_spec_doc` 正常入库编码）。
- **两条硬约束**：① 全程旁路——embedding 端点没起来时参考文档跳过（logger.warning），注册绝不失败；② 幂等——按任务名 / 规范标题判重，老账号回填与新人注册同一条路（管理员账号已回填）。
- **任务与规范库按 user_id 隔离，没有公共模板机制**——"默认配置"只存在于每个用户自己的空间，改默认=改 `DEFAULT_TASKS`，同时同步前端 `Tasks.vue` 的 `PRESET_TASKS`（两处文案同源，无共享常量）。
- **对测试的连带义务（改这里必踩）**：五套 e2e 的 cleanup 现在都按 user_id 删默认任务/规范（否则 user 行 FK 删不掉）；`test_e2e_rag.py` 的"无规范库/删库归零"用例在注册后**先 GET+DELETE 清空默认规范**再测——新写涉及规范库断言的用例，主/他人两个账号都要清。


## 3.21 规模上限与 SSRF 防护（2026-11-22，开放给别人用之前加的）

- **三个上限都在 `config.py`，默认值本身就是策略**：`max_upload_mb=20`、`max_pdf_pages=300`、`allow_private_provider_urls=False`。前两个 **0 = 不限制**。为什么默认就开着而不是等上线：上传是"整份读进内存再提取"，不限大小一个请求就能打挂进程；而 PDF 会整份传给 MinerU 云**按页计费**——所以字节闸和页数闸是两件事，20 MB 里塞得下一份两千页的文件，字节没超、钱会超。默认 20 MB 的依据是本机最大的真实夹具 4.3 MB。
- **读取必须走 `services/uploads.py`**：`read_capped`（async）与 `read_capped_sync`（同步路由用）分块读、超限即断，**绝不先把整个文件吸进内存再判断**；`check_pdf_pages` 只卡 PDF。两条上传链路（`api/v1/process.py` 的工作文档、`api/v1/specs.py` 的规范库）都必须用它——写两遍判据迟早只改一边。超限返 **413**，页数超限返 **400**。
- **`check_pdf_pages` 对打不开的 PDF 一律放过**：损坏件、带密码的加密件在 `services/pdf.py` 各有专门文案，这里抢判据会把"文件已加密"误报成"页数超限"，比不拦更糟。
- **`base_url` 的 SSRF 校验在 `schemas/provider.py:_validate_base_url`，必须解析成 IP 再判**。base_url 不是给用户浏览器打开的链接，而是**服务端**发 POST 的目标，任何登录用户填 `http://169.254.169.254/` 就能让服务器替他去够云元数据。字符串黑名单挡不住 `127.1` / `0177.0.0.1` / `[::1]` / `127.0.0.1.nip.io`，所以先 `getaddrinfo` 再逐个判 `is_private / is_loopback / is_link_local / is_reserved / is_multicast / is_unspecified`。`ProviderUpdate` 也过一遍——否则"建号合规、编辑改成内网"就绕过去了。
  - **已知边界（未做）**：校验时解析到的 IP 与真正建连时可能不同（DNS rebinding），彻底做法要在 httpx 连接层再判一次。
  - **`ALLOW_PRIVATE_PROVIDER_URLS=true` 只给开发机**（连本地假端点 19000 / LM Studio 用），**部署时不要带这一行**。五套 e2e 用的 `http://127.0.0.1:*` 提供商全靠它。
- **单测里不要解析真实域名**：`_validate_base_url` 会走 `getaddrinfo`，测试只用**字面 IP**（数字地址不发 DNS 查询）和开关早退路径，否则离线环境一片红（§7.3 的"不出网"边界）。



## 3.22 认证加固（2026-11-22，开放注册给别人用时加的）

- **口令强度只作用于注册路径**：`schemas/auth.py:RegisterRequest` 是 `min_length=8` + `_reject_weak_password`（拒"整串只有一个字符类"：纯数字 / 纯字母）。**登录不校验长度**——`LoginRequest` 故意没有约束，否则存量短密码用户会被前端或后端拦在门外，等于自己把人锁住。前端 `Login.vue` 的 `rules` 因此是 `computed`，按 `tab` 切换：注册 tab 才带强度规则。改这条前先想"会不会把已有账号挡在外面"。
  不用"大小写+数字+符号"那套复杂度规则：它逼出 `Password123!`，既难记又是最常见的弱口令模板；长度才是主要防线。
- **登录失败锁定在 `api/v1/auth.py`**，键是 **(客户端 IP, 用户名小写)**：只按用户名会被拿来锁人（拿错密码把受害者锁在门外），只按 IP 则挡不住分布式脚本。窗口 `login_lockout_minutes` 同时是遗忘时长；**登录成功必须清账**。`login_max_failures=0` = 关闭。不存在的用户名同样计数并返 429，否则 401/429 的差异又能枚举用户名。
  ⚠ 两条已知边界：① 计数器是**进程内**字典，属 §3.3 reaper / §3.6 并发闸同一个**单 worker 假设**，重启清零；② `request.client.host` 在反向代理后面会全是代理 IP——**上 Nginx 之后必须改读 `X-Forwarded-For` 并校验代理链**，否则这道闸退化成"按 IP 全局锁定"。
- **注册永远只能建出 `user`**：`User(...)` 不读 body 的 role。唯一的 admin 由 `api/v1/admin.py:update_role` 的**护栏 0**固定——`body.role == "admin"` 直接 400，"能设管理员"在开放注册之后本身就是最大的越权面（第二个 admin 能看全站用户）。前端 `Admin.vue` 把"管理员"选项置 `disabled`（保留是为了让现有 admin 那行显示得出名字），但**真正的封死在接口层**。原有的护栏 1（不能改自己）与护栏 2（至少留一名 admin）没被取代。
- **`max_registrations_per_day` 是全局 DB 判据**（按 `users.created_at` 落在近 24 小时计数，走 `database.db_now()`），`0 = 不限制`。为什么光有 §3.21/§3.20 的按账号配额不够：配额按账号算，脚本注册 50 个号就有 50 份额度，**兜住总消费的其实是注册上限**。本机 `.env` 设 60 而不是 10，是因为五套 e2e 各自都要注册账号，得留够跑回归的余量。
- **`database.db_now(db)` 是所有"和 DB 时间列比时间"的单一入口**（配额窗口、注册上限都用它）。SQLite 回来的是字符串、Postgres 回来的是带 tz 的 datetime，这里统一成 naive UTC。新增这类判据时**不要再写第二个 `_db_now`**，也不要用应用侧 `datetime.now()`——§7.3 那条容器钟偏差的坑就是这么来的。



## 3.23 错误文案分层与错误编号（2026-11-22，开放给别人用之前做的）

**分层规矩：`message` 给人看，`detail` 给排障用，`code` 是两者的桥。**

- **`services/errors.py` 是编号的唯一来源**。`operation_logs.error_message` 直接显示在界面上，
  以前它混装两种东西：一处是写好人话（"扫描件落盘记录缺失，请重新上传"），另一处直接是
  `内部错误：{e}` 或 `body={resp.text[:500]}`——后者会把上游报文、URL、框架内部结构送给陌生用户。
- **`_mark_failed(db, log, message, *, code=CODE_INTERNAL)` 总是追加编号**，调用点只写人话。
  这样"一半有号一半没号"从构造上不可能出现；给不出具体 code 就落 `E-INTERNAL`，
  它本身是有用信号（这条失败还没被归类）。**不要在调用点自己拼 `（E-XXX）`**。
- **`LLMError` 带 `code` 与 `detail`**。上游响应体、原始异常文本一律进 `detail`，
  由 `logger.warning` 与 `llm_calls.error_message`（`_meter(error=e.detail)`）承接。
  计量表那一路必须传 `detail` 而不是 `str(e)`，否则原文就只存在于日志文件里。
- ⚠ **`chat_json` 的降级阶梯判据是 `e.detail`，不是 `str(e)`**。`_rejected_response_format`
  靠 `response_format` / `json_schema` 这些**只出现在上游原文里**的字样判断要不要降档。
  把文案换成人话之后若这里仍写 `str(e)`，降级会**静默失效**——实测就是这样：所有不支持
  严格档的端点都会直接抛"模型服务返回了错误"。改任何错误文案前先确认有没有代码在**解析**它。
- **前端 `extractErrorMessage` 是唯一出口**，它现在做三件事：后端字符串 detail 优先透出；
  422 的 `detail[]` 按 `FIELD_LABELS` 把字段名翻成中文、`translateMsg` 把 Pydantic 英文
  译成中文；**拿不到可读文案时按状态码兜底**（413→"文件太大"、429→额度、500→已记录），
  绝不把 `Internal Server Error` 这种英文原文弹给用户。
  加新 `FIELD_LABELS` 条目时同步后端模型字段名，别凭记忆写。
- **`describe_exception` 是兜底闸**：认得出类型给具体指引，认不出统一一句话 + `E-INTERNAL`，
  **绝不把 `str(exc)` 端出去**。`tests/test_error_codes.py` 里有专门一条断言"未知异常不得
  把 SQL 片段带进文案"，别删。
- **B6 留存告知在 `Home.vue` 上传步骤**（`upload-notice`）：说清"存在哪、什么情况下会离开本机
  （扫描件整页送 MinerU 云）、别上传他人隐私"。改上传链路的外发行为时必须同步这段话。

## 3.24 `api_key` 字段级加密（2026-11-22，Fernet）

- **库里存的不再是明文**：`model_providers.api_key` 是 Fernet 密文，`String(512)`（输入上限 255，密文约 400 字符）。加解密只在两个边界：写入时 `api/v1/providers.py:_encrypt_or_400`（**create 和 update 都要**，漏一个就是"创建时密文、改一次退回明文"），读取时 `services/llm.py` 拼 `Authorization` 前 `decrypt_secret`。业务代码不应再直接读 `provider.api_key`。
- **`services/secrets.py` 是严格模式：解不开就抛 `SecretError`，绝不"看着不像密文就当明文用"**。这不是洁癖——留一条明文后路，等于任何能写库的人都能让代码按明文处理，这道闸就形同虚设。因此**存量数据必须先跑 `migrate_encrypt_api_keys.py`**，否则那些 provider 一调用就报"模型密钥解密失败"。
- ⚠ **迁移脚本必须先区分"解不开"的两种原因**：值不像 Fernet token → 历史明文，加密它；值像 token 却解不开 → **密钥不对，立即中止且一行都不改**。把密文当明文再加密一层等于永久毁掉它。脚本有 `--check` / `--dry-run` 两档，判据是 `gAAAAA` 前缀 + 可 base64 解码 + 长度 ≥ 60（Fernet token 首字节恒为版本 0x80）。
- **`API_KEY_ENC_KEY` 一旦丢失，库里所有密钥不可恢复**。它只存在于 `.env`（已 ignore），必须单独备份。生成：`python -c "from services.secrets import generate_key; print(generate_key())"`。留空 = 保存/读取密钥都响亮失败（`E-CONFIG-SECRET`），不退回明文。
- **展示路径不许因为密钥问题 500**：`mask_secret` 解不开返回 `"****"` 而不是抛错——列表页要永远打得开，真正该报错的是外呼路径。
- **单测连带义务**：`tests/conftest.py` 给每个用例装一把固定测试密钥（`base64("0"*32)`，不用随机值以便复现）。凡构造 ORM `ModelProvider` 行的地方**必须写 `encrypt_secret(...)`**——写明文是在验证一条生产环境不存在的路径。
- 没有 Alembic，所以列宽 255→512 也在同一个幂等脚本里做（`information_schema` 查 `character_maximum_length` 再 ALTER）。



## 4. 前端不可违反的约定

- **前端测试是 `npm test`（vitest + happy-dom），配置在 `vitest.config.ts` 不在 `vite.config.ts`**：构建入口 `vue-tsc -b && vite build` 是唯一的类型门禁，不要为了跑测试去改它。测试文件放 `src/**/*.test.ts`，会被 `tsconfig.app.json` 的 include 一起类型检查，所以 `noUnusedLocals` 之类在测试里同样生效。当前只测 SFC 之外的纯逻辑（`extractErrorMessage`、`tokenStorage`、两个 Pinia store），**没装 @vue/test-utils**——要测组件渲染再说。加新配置项时记得：测试里用 `SimpleNamespace` 造假 Settings，字段缺一个就是一堆 AttributeError（本轮加 `llm_enable_thinking` 时 29 个测试连带失败就是这个原因）。
- **token 双写**：登录成功同时写 Pinia store 和 `localStorage`；axios 拦截器只读 `localStorage`（在 API 层拿不到 store）。key 常量集中在 `client/src/api/client.ts`（`fileagent_token` / `fileagent_username`），别处不要硬编码。
- 401 处理要排除 `/auth/login`（那是密码错误），用 `window.location.replace` 带 `redirect` 参数（拦截器层拿不到 router 实例）。
- 自动重试**只针对无响应的 GET**，1 次 / 800ms；写操作绝不重试。
- 处理结果**一律靠轮询 `status` 拿**，不要依赖 `POST /process` 的响应体（它只返回 processing）。`POLL_INTERVAL=5000`，等待上限是**墙钟**：`stores/home.ts:pollDeadlineAt`（`POLL_TIMEOUT_MS`=20 分钟，持久化，**切页与刷新都不重置**）。以前是 `MAX_POLL_COUNT=120` 计数，但 `startPolling()` 每次把计数清零而 `onActivated` 会重新调它——来回切页就永远到不了上限。超时**不是失败**：只 `stopPolling`，出口是"继续等待"（`resumeWaiting`，只重查状态）或去日志页；**绝不能在超时态把按钮挂回 `doProcess`**，那会重跑整条流水线再烧一次 MinerU 配额与 token。预算必须大于后端最坏路径（`mineru_max_wait=600s` + 修复与版面两段 VLM 外呼）。
- `AppLayout.vue` 的 `<keep-alive>` 包住 router-view，工作台切页不销毁 → 必须成对处理 `onActivated`（恢复状态 + 重启轮询）/ `onDeactivated`（停轮询）。
- **Pinia setup store 只用 `ref` + `watch`**，不要用对象字面量 getter/setter 代理闭包变量：Pinia 会把访问器转成普通数据属性，导致 `restore()` 后 UI 拿不到值、setter 不触发保存（`client/src/stores/home.ts` 文件头"持久化策略"注释已写明这条）。
- 持久化用 `sessionStorage`（key `fileagent_home_state`），**不存 File 对象**，恢复时 `pickedFile` 置 null。
- 路径别名 `@` → `src`，`vite.config.ts` 与 `tsconfig.app.json` 两处都要配。
- Element Plus 是**按需引入**（`vite.config.ts` 的 `unplugin-vue-components` + `ElementPlusResolver`），首屏 JS 从 893kB 降到 ~436kB，构建的体积告警随之消失。三件插件管不到的东西必须在 `main.ts` 手工接上，漏了就是"构建通过、界面上裸样式"：**ElMessage / ElMessageBox / v-loading**（函数式与指令，不是标签，扫不到）以及它们各自的样式、还有中文 locale（改成 App.vue 里的 `el-config-provider`，不要再 `app.use(ElementPlus)`）。`src/components.d.ts` 是构建生成的，已 ignore，删掉也能重新生成。
- **主题层是唯一定制入口**：`client/src/assets/theme.css`（`main.ts` 里在 `style.css` 之后 import）覆盖 Element Plus 的 CSS 变量——暖白底 / 墨黑前景 / 近黑主色 / 暖橙高亮笔点缀，另有漂移光斑 + 噪点背景。新增视觉定制优先改这个文件，**不要在页面里硬编码色值**（历史遗留的蓝紫渐变已在 2026-11-17 全部清掉，回归检查：`grep -rn "409eff|66b1ff|6366f1" src --include=*.vue` 应为空）。覆盖层用 `html:root` 选择器压过 base.css 的 `:root`/`html` 双声明；整体回滚只需删 `main.ts` 一行 import。动效/圆角/色彩锚点令牌都定义在 theme.css 顶部 `--fa-*`，页面里引用令牌不引用字面值。
- **给 `el-button--primary` 这类组件级规则加外观（渐变/圆角）时必须 `:not(.is-link):not(.is-text)`**——link 按钮也带 `el-button--primary` 类，否则"退出"这种文字链接会被刷成实心色块（2026-11-17 浏览器走查实拍）。同理 `el-step__head` 是容器，染背景要落到 `.el-step__icon.is-text` 上。
- **页面头部结构是全局的**：`.page` / `.page-header` / `.page-header-content` / `.page-icon` 都在 `style.css`，五个业务页共用（原先 Tasks/Providers/Logs 各抄一份 `.page-icon`，已去重）。新页面直接套这套类名 + `page-header-content > (page-icon + div)`，不要再在 scoped 里另写头部样式。
- **色彩分工**：`success/warning/danger/info` 只用于**状态**（日志状态、删除按钮）；属性标记（批改=是、`.pptx`、官方）一律 `type="primary"`。主色已是近黑，所以"处理中"用 `warning` 而不是 `primary`——否则与 `pending` 的灰阶糊在一起（UI-REFERENCE §4.5 预告的代价，实测踩到）。
- **状态值不给用户看英文枚举**：`Logs.vue` 的 `statusLabel` 从 `statusOptions` 派生，筛选下拉与表格列共用一份中文文案；新增枚举时改这一处。
- 改了样式装载方式，验证不能只看 `npm run build` 过了：把 `dist/assets/*.css` 拼起来 grep 每个用过的 `el-*` 类名，才能确认样式真的进了产物（`el-option` / `el-tab-pane` / `el-config-provider` 三个是名字对不上，不是缺样式——它们的 style 入口引的是父组件 CSS）。**弹窗样式、loading 遮罩、分页中文、状态中文标签、批注清单联动、手机端 TabBar/卡片列表/弹窗宽度已在 2026-11-21 真浏览器逐项走过并存档结论**，别再写"未验证"；仍未在浏览器里跑的是深色模式（明确不做）。
- **手机端专门布局（2026-11-21，375px 真机实测）**：判定唯一入口 `composables/useIsMobile.ts`（matchMedia 768px）——**768 阈值三处同源**（useIsMobile / style.css 断点 / theme.css 手机端层），改一处要想起另两处。形态：AppLayout 双渲染（桌面悬浮胶囊 / ≤768 细顶栏 + 底部 TabBar 5 项，规范库·用户管理·退出收进"更多"弹层）；五个列表页用 `MobileCardList`（泛型外壳 + 页面插槽）v-if 分支替换 el-table；弹窗/消息框宽度在 theme.css 手机端层收 92vw/86vw；Home 步骤条手机端换两字短标签（选择/上传/处理/下载，四字会竖折实拍）。新加列表页照 Logs.vue 的双分支写法，别只做桌面表格。
- provider 的 `capabilities` 后端存逗号分隔**字符串**、表单要的是**数组**，转换只认 `KNOWN_CAPABILITIES` 里的已知值（`client/src/views/Providers.vue` 的 `toCapabilityList()` / `buildPayload()`）——顺带做了顺序归一化和脏值丢弃，未知标记不会把表单卡死也不会吃 422。提交**必须走 `buildPayload()`**，别把带 `capabilityList` 临时字段的 form 整个发出去。
- 上传格式只在两处声明：`Home.vue` 的 `accept=".docx,.pdf"` 和上传区文案。**前端不做扩展名校验**，`accept` 只管文件选择对话框，拖拽照样能拖进 `.txt`——真校验在后端白名单，400 文案已是给用户看的。加格式时要同时改这两处 + 后端派发表。
- 结构化批改的原文高亮在 `client/src/utils/annotate.ts:buildParagraphs()`（纯函数，`annotate.test.ts` 钉着），视图在 `client/src/components/GradingResult.vue`。按 `\n` 分段，用全局偏移 `start/end` 折算成段内区间再套 span。**非高亮片段也必须给 `index: null`**（`Span.index` 是 `number | null` 必填），漏了会让相邻片段的 `seg.index` 是 `undefined`，点击时把 `activeIssueIndex` 写成 `undefined` 从而永不匹配。匹配不到位置的 issue 显示"未定位"，不要猜位置。卡片↔高亮的联动状态是 `GradingResult` 的局部状态，不要退回 Home 编排层。
- **`TYPE_COLOR` 八色是问题类型的数据编码，不是装饰**：不要为了跟暖白/墨黑主色系统一而压成单色阶梯，那样"错别字"和"结构问题"就分不出来了。它住在 `utils/annotate.ts`，加类型时要同时改后端 `schemas/grading.py` 的 `ISSUE_TYPES` 与这里的两份表。
- `gradingIssues` / `gradingDegraded` 和 `processedContent` 一样要走 `syncToStore()` + `home.ts` 持久化，并在 `reset()` / `goBackToUpload()` 里成对清空；`applyResult()` 是唯一铺结果到 UI 的入口（3 处轮询/上传回调都调它），别在调用点各自赋值。**阶段 6 的 `specRefs` / `specNote` 走同一条路**：加字段时 `home.ts` 的六处（state / 接口 / save / watch 数组 / restore / clear / return）和 Home 的两处清空都要成对改，漏一处就是刷新后显示上一次的引用。
- **规范库页（`views/Specs.vue`，阶段 6）**：上传走 `el-upload` 的 `:http-request` 自定义，不能用它默认的请求——那条路绕过 axios 拦截器，token 带不上。删除要写清后果（"之后批改不再召回它的 N 条条款"），因为规范库不可变，删了只能重传。条款引用显示在 `GradingResult.vue` 顶部，`refs` 为空时整行不出现（不是显示"参考了 0 条"）。
- **扫描件提示走 `ocr_pending`**（§3.15）：上传响应与 status 都带这个字段。`Home.vue` 在处理步骤把"提取到 N 字符"换成"扫描件将在处理时由 MinerU 识别"，耗时提示换成"几分钟"档；**不要显示空白预览假装提取失败**。OCR 完成后 status 回填 `original_content`，`applyResult` 只在 `extracted_text` 为空时补进 `uploadResult`（新对象，不可变），高亮底本就有了。`uploadResult` 整体持久化，`ocr_pending` 自动跟着走，不需要 home.ts 加字段。
- **转换类任务（md / pptx）的工作台文案随 `isConversionTask` 切换**（§3.16 / §3.17）：`Home.vue` 用 `selectedTask`（按 selectedTaskId find）派生 `isConversionTask` / `downloadLabel`（md→"下载 Markdown"、pptx→"下载 PPT (.pptx)"）/ `processHint`（md→"Word 本地转换很快…"；pptx→"PDF 走 MinerU 版面识别后本地拼装…1–3 分钟"）；处理步骤标题转换类时是"转换处理"不是"调用 LLM 处理"；**无可用模型的警告 `v-if` 要带 `&& !isConversionTask`**——转换零 LLM，没模型也能跑，别误报。`Tasks.vue` 的 `watch(output_format)` 对 md/pptx 同向强制归位、`isConversion` 驱动 disable、表格"无需模型"覆盖两种格式。`OutputFormat` 类型（`api/types.ts`）与后端 `schemas/task.py:OUTPUT_FORMATS` 同步，加值多处一起改。

## 5. 数据库变更

**没有 Alembic**。启动跑 `Base.metadata.create_all()`，它**只建新表、绝不 ALTER 已有表**。
改列/加约束要写一次性幂等迁移脚本（Postgres 方言：先查 `information_schema.columns` 判存在，再 `ALTER TABLE`）。
新增 ORM 类必须在 `server/models/__init__.py` 里 import，否则 `create_all()` 发现不了这张表。

⚠ **历史包袱（已了结）**：MySQL 时代本机老库是早期手工建表，与 ORM 声明长期漂移（外键规则 NO ACTION vs SET NULL、status 允许 NULL、TEXT vs LONGTEXT 不一致），§3.2 和 s2 修复都按这套假设写过。阶段 4（2026-11-16）一次性把业务表全量迁到 Postgres，**新库 `create_all()` 一次建齐，外键规则与 ORM 声明逐字匹配**，老库 vs 新库差异消失。`server/migrate_add_provider_*.py` 等三个 MySQL 方言脚本已废弃并于 2026-11-17 `git rm`（语法 `SHOW COLUMNS` 在 PG 上跑不了，新库也不需要——它们只是给老 MySQL 库做幂等 ALTER 用的；历史可从 git 找回）。
**未来再要改列**：写 Postgres 方言的一次性脚本（先查 `information_schema.columns` 再 ALTER），或者直接重 `create_all()`（如果可丢数据）。现成范例：`migrate_add_spec_embedding.py`（加 vector 列）、`migrate_add_log_needs_ocr.py`（一次加 `needs_ocr` / `stored_filename` 两列，2026-11-20 已执行）。

⚠ **`create_all()` 也不会给已存在的表补建索引**——所以模型里新加 `index=True` 同样要写一次性脚本落到现有库。范例：`migrate_add_fk_indexes.py`（`operation_logs.task_id` / `tasks.provider_id`，2026-11-22 已执行）。索引名沿用 SQLAlchemy 默认 `ix_<表>_<列>`，脚本与新库 `create_all()` 建出来的逐字一致，不留"两种库索引名不同"的差异。

## 6. 注释与代码风格

- 注释、docstring、错误消息、API 描述**全部中文**。
- 本仓库是学习项目：核心模块保留 `【Java 对比】` 教学块（Spring / JPA 类比），**有意为之，不要当冗余删掉**。
- 核心业务（隔离、后台处理、diff、状态机）注释可以详细；样板 CRUD 保持简洁。
- 不写"本次修复 / 为某需求添加"这类会随代码腐烂的注释。
- ruff：line-length 100、target py312。忽略项都有原因，别随手删——`B008`（FastAPI 把 `Depends()` 写在默认参数是框架约定）、`RET501`（204 显式 `return None` 更清楚）、`RUF001/002/003`（中文标点会被判歧义 Unicode，全量误报）。
- **`E501` 把东亚宽字符按宽度 2 计**，所以中文注释/字符串的实际字符数要砍到约 50 才稳；判超长别数字符，用 `unicodedata.east_asian_width` 算宽度。
- **`ASYNC109` 禁止 async 函数用名为 `timeout` 的参数**。要往下透传 `httpx.Timeout` 对象时把参数叫 `timeouts` 或 `read_timeout` 就没事，别为它加 `# noqa`——真有理由的阻塞点才用 `# noqa: ASYNC230` 并写明原因。

## 7. 当前基线与已知缺口

当前路线（阶段 0–6，加 MinerU / ① md / ② PPT 三段拓展，含为什么这么排）在 SPEC §9.2；阶段 0 交付与验证明细在 SPEC §9.3，阶段 1 在 §9.4，阶段 1.5（PDF 输入 + 去 Word 化）在 §9.5，阶段 2 最小版（评测台 + 基线数字）在 §9.6，阶段 3（长文档分块批改图）在 §9.7，缺陷台账第六轮在 §10，改 bug 前先看这几张表。

### 7.1 门禁与验证快照

最近一次全绿实测（2026-11-21，② PDF→PPT 忠实转换片1 之后）：**pytest / ruff check / ruff format --check / uv lock --check / npm run build / npm test / e2e 各套全过**（清单见 §7.2）。唯一 skip 的那条单测是 win32 下 `os.environ` 大小写不敏感、测不了小写 `no_proxy` 键，属平台限制不是回归。

⚠ **具体条数（pytest 的 passed 数、e2e 的 N/M）不再抄进文档**。这个位置历史上写过 204 / 277 / 322 / 363 四个版本，每抄一次就要跟着每次改动重改，而且已经出现过 §2 写"41 项"、本节写"46/0"这种同仓库互相打脸的数字。想知道条数就跑命令，别相信文档里的快照。

**真实云端冒烟（2026-11-20，用户自填 MINERU_TOKEN）**：合成 2 页扫描件全链路通过（59s，OCR 写回 + 批改定位 100% + structured 三件套落盘 + 计量只有 grading 行、无 MinerU 行）；md 转换浏览器走查 docx→md（本地，4s，标题/表格转 md）与 PDF→md（真实 MinerU，纯 .md 无图）均 done，扫描件上传提示正确。**只发合成件，用户真实文档绝不进云**。

`test_e2e_rag.py` 比其他几套多一个前置——**本地 embedding 端点必须在跑**，脚本开头探活，缺了直接退出（返回码 4）而不是让断言一片红。

### 7.2 e2e 脚本清单（都是现役脚本，别删）
- **`server/test_e2e.py` 是仓库自带的端到端冒烟脚本，不是遗留物，别删**：需后端已在 8000 运行、连真实 Postgres。它会注册 `tester_*` / `other_*` 两个临时账号，造 1 条日志和 1 个上传文件；**跑完自动清理 DB 记录**（llm_calls → logs → tasks → providers → users）。落盘文件自 2026-11-20 起有 `stored_filename` 列可精确回溯（`test_e2e_mineru.py` 已按它清理）；**历史行该列为 NULL**，老产物仍只能按命名规则清：`ls uploads | grep -E '^[0-9a-f]{32}\.(docx|pdf)$' | xargs -I{} rm uploads/{}`。⚠ 用户放进 `uploads/` 的**具名文件是真实文档夹具**，不属于清理范围（2026-11-19 按这条清了 35 个 e2e 产物，6 份具名文档一个没碰）。第 7 节现在真走重试路径（连不上假 provider → 重试 3 次 → failed），第 10 节直接改库把日志置为 done 来验证下载，都属预期行为。
- **`server/test_e2e_structural.py` + `test_llm_server.py` 是阶段 1 新增的结构化端到端**，同样别删：需后端 + 假 LLM 两个进程都在跑，全程零真实 token。mock 端点**刻意不支持 `response_format`**（回 400）来如实模拟国产兼容端点并逼出 `chat_json` 的降级路径，所以 LLM 日志里出现的 `400 Bad Request`（每次 `chat_json` 两条：`json_schema` 和 `json_object` 各一档）是预期行为不是故障。第 8 节是阶段 1.5 加的 **PDF 输入链路**：用 pymupdf 现场合成"每视觉行一个 block"的中文 PDF，断言提取结果与 docx **逐字相等**、定位成功、下载名换成 `.docx`——改提取器必跑它。它注册 `e2e_s_*` 账号，**跑完同样自动清理 DB + checkpoints/**。
- **`server/test_e2e_ppt.py` 是 ② PDF→PPT 忠实转换端到端**（2026-11-21 重写，替换阶段5 大纲流水线版；片2 加第 9 节、片3 加第 10 节）：**自己起后端**（`FA_PPT_E2E_PORT` 覆盖端口，默认 8011，注入假 MinerU 环境 + 强制 stub 渲染），另需假 MinerU 19002 + 假 VLM 19001 在跑；不用假 LLM。断言覆盖：互斥 422/400、docx 上传 400、文字型 PDF 受理→done、下载 .pptx 重读（文本框逐字、原生表单元格、图片、横向页眉保留、页码噪声跳过）、无坏块零 `llm_calls`、structured + `outputs/pptx/{log_id}.pptx` 落盘、MinerU failed→可行动文案→重试 done、越权下载 404、片2 坏块修复（`/v1/_mock/repairs` 计数 + 产物标记 + `ppt_repair` 归 vision）、片3 版面循环（批评 2 轮 + `ppt_critic` 计量 + 被点名横向页改走重排后页眉消失、文字一字未改）。它注册 `e2e_p_*` 账号，跑完自动清理 DB + provider + 本次落盘/产物文件。
- **`server/test_e2e_rag.py` 是阶段 6 补的规范库端到端**（此前 `/specs` 路由完全没被 e2e 碰过）：覆盖上传、向量落库、列表与越权、条款注入、引用落库、删库后归零。前置比其他几套多——**本地 embedding 端点必须在跑**，脚本开头探活，缺了直接退出（返回码 4）。
- **`server/test_e2e_mineru.py` 是扫描件 + md 转换端到端**（§3.15 / §3.16）：覆盖扫描件受理 → OCR 写回 → 批改定位 → structured 落盘 → 失败路径 → 计量边界，以及 md 互斥 / docx→md / PDF→md / 零 LLM 四节。它**自己起后端**（`FA_PORT` 覆盖端口，`run_backend.py` 已支持），目的是注入假 MinerU 环境而不改日常 `.env`；另需假 LLM 19000 + 假 MinerU 19002 在跑。

### 7.3 测试边界与其他基线事实

- 单测**不连 Postgres 也不出网**：HTTP 走假 client（替 `services.llm.get_client`），provider 路由走内存 SQLite。所以 `pytest` 绿不代表能连上数据库，端到端才是那一层的验证。加测试请保持这个边界，也别引入 `pytest-asyncio`——现有测试直接 `asyncio.run()` 就够。
- `uploads/`、`outputs/`、`.env`、各类缓存已 ignore；`.env.example` 靠 `.gitignore` 里的 `!.env.example` 例外入库。
- SPEC §11 示例写 `JWT_EXPIRE_HOURS=2`，本机 `.env` 实际是 24；`.env` 不入库，以本机为准。
- **时间列只用 DB 时钟（2026-11-17 修）**：全站 13 张表的 timestamp 列都由 `func.now()` 生成，唯独 `operation_logs.processed_at` 曾用应用侧 `datetime.now(UTC)` 写入。容器钟比宿主机快 3–4s，于是出现 `processed_at < uploaded_at` 的倒挂、"处理耗时"相减得负数。现在两处写入（成功路径 + `_mark_failed`）都走 `func.now()`，且 commit 后必须 `db.refresh(log)` 才能把 SQL 表达式读成真实 datetime；启动期 reaper 把僵尸 processing 重置为 failed 时同样要写 `processed_at`（模型约定"每个状态变更都记录 processed_at"）。**新增时间列时不要再引入第二个时钟源**；`services/auth.py` 的 JWT `exp` 属于应用域时间，不走 DB，保持用 `datetime.now(UTC)`。诊断法：`SELECT (processed_at < uploaded_at), count(*) FROM operation_logs WHERE processed_at IS NOT NULL GROUP BY 1;`——修复前留下的 2 条历史行仍是倒挂，属正常。
- 同一次普通用户走查还确认：`/admin/*` 对普通用户是 403、公共 provider 对其不可见、越权引用公共 provider 是 400、无可用模型时后台落 `failed` 且文案可行动、失败后下载 400——这几条没问题。

### 7.4 真机一手结论（带日期的观测，不是回归基线）

- 真机跑过的一手结论（2026-11-15，qwen3.7-flash + 合成中文 PDF）：结构化链路 OK（`degraded=False`、4 条 的/地/得 全部精确定位）。但该端点回来的 `revised_text` 与原文一字不差——意见对、修订没落地。这是 prompt 产出质量，不是提取或解析缺陷，正属阶段 2 评测台要量化的范围，别在提取层"顺手改 prompt"掩盖它。
- **真实模型轮一手结论（2026-11-18，qwen3.8-max-0902 + 用户真实文档 6 份）**：
  - 4 份可处理文档全部 `done`、`degraded=False`、问题清单**定位率 100%**（11/11、10/10、8/8、0/0），修订文长度≈原文且**确实不同**——2026-11-15 那条"意见对但修订没落地"的旧缺陷在这条链路上没有复现。
  - **图文混排的宣教类 PDF 报 0 条问题**（1282 字符散在 9 页、92 张图）。`degraded=False` 说明模型守了格式、真的认为没问题。此时界面显示"批改完成，未发现问题"对用户是误导——文档有内容，只是这类内容不该走文字批改。要不要加"提取密度异常/图文混排"提示是产品决策，未做。
  - PDF→PPT 走真视觉模型跑通（**该流水线已于 11-21 被 ② 片1 忠实转换替换，见 §3.17**）：批评循环实际外呼 4 次（outline→critic→outline→critic），产出 12 页 pptx 可被 python-pptx 重读，单份 22,571 token（≈批改一份的 6 倍）。
  - 本轮 6 份合计 35,181 token。成本只能事后从 `llm_calls` 看，`provider.max_tokens` 拦不住（见 §3.6）。
### 7.5 路线状态与未做

- **路线 2026-11-16/17/19/20/21（见 SPEC §9.2）**：阶段 4（Postgres 基建，11-16）、阶段 5（PDF→PPT 大纲流水线，11-17 八切片；**11-21 被 ② 片1 忠实转换替换**）、任务 2（管理员 + 默认模型授权，11-17）、阶段 6（本地 embedding + pgvector 检索 + 条款注入，11-19 A1–A7）、MinerU 扫描件 OCR 兜底（11-20 A–E，见 §3.15 / SPEC §9.15）、① md 转换任务（11-20，零 LLM，见 §3.16 / SPEC §9.16）、**② PDF→PPT 忠实转换片1/片2/片3（11-21 全部完成：块驱动渲染器+B 重排/A 兜底 §3.17 / 坏块 VLM 修复 §3.18 / 版面批评循环 §3.19，详见 SPEC §9.17）** 均已完成。**阶段5 大纲流水线模块（ppt_graph / pptx_builder / ppt_outline / vlm_critique 及其单测）已于 11-21 按用户决定整体删除**（§3.11）、**注册默认配置（4 任务+通用参考文档，§3.20 / SPEC §9.18）已完成**。仍然没做：`interrupt` 人工确认分块方案（要先有 UI）、长文两段式摘要、`checkpoints/` 体积治理与 TTL、引用点击回跳规范原文、`format_tier` 落库（改表要先问）。
- **更大的未做清单**：限流、api_key 字段级加密、Alembic、Docker / 多 worker、表格提取（docx 表格和 PDF 表格都不进正文）、加密 PDF 解密、成本价格表（计量只存 token 不算钱）、问题清单写进交付 docx、本地 OCR 推理（云兜底已做，见 §3.15）。评测台的下一步：真实文档夹具、第二厂商当裁判、同夹具跑重复轮次（单轮会漂）、把 `format_tier` 落进 `llm_calls`（改表结构，做前先问）。

### 7.6 已落地功能的决策记录
- **管理员 + 默认模型授权（已于 2026-11-17 实施，简单版）**：`users.role` / `users.can_use_default` / `model_providers.is_public` 三列 + `require_admin` + `/admin/*` 三端点 + 前端 Admin 页。不做 audit / 跨用户 provider 管理（用户拍板）。授权判据集中在 `services/llm.py:provider_usable_by`，改任何 provider 可见性逻辑先看 SPEC §9.11 的实施清单。**admin 路由是 §3.1 隔离铁律的声明式例外**——新加管理端点挂 `require_admin` 即可，不要"顺手"补 user_id 过滤把 admin 视图废掉。
- **阶段 5 PDF→PPT（历史记录，现役见 §3.17–§3.19）**：当年 6 项决策已敲定（VLM 走 mock 起步、OCR / 图片 / 真云端 VLM 是显式扩展口子）。**大纲流水线已于 2026-11-21 整体删除**（模块+单测 git rm，历史可找回；有效机制已迁入 §3.11 保留说明与 §3.19）。⚠ 同批设计的 `services/pdf.py:extract_text_from_pdf(fallback_to_ocr=False)` **至今没有实现**——真实签名一直是 `extract_text_from_pdf(file_bytes)`（见 §7.7 阶段 6 前置约束），OCR 兜底落在路由层。完整计划见 SPEC §9.12，替换过程见 §9.17。
### 7.7 阶段 6 前置约束与实测事实（2026-11-19 核实，动手前先读）

这一节是"当时探明环境后定下的策略依据"，§3.12 / §3.13 / §3.14 里所有"别这么做"的条款都回指这里。
  - **环境**：`vector 0.8.7` 已装进 `fileagent-postgres`；镜像里**没有 `pg_jieba` / `zhparser`** → 混合检索的中文分词这条路当前走不通，**先纯向量**。入库已跑通：`spec_docs` 有 1 篇（教学示例稿规范，10,889 字）、`spec_chunks` 66 块，检索层可以直接拿它当数据。
  - **embedding 端点**：LM Studio `http://127.0.0.1:11386/v1`，模型 `text-embedding-bge-m3`（文件 `D:\aiagent\app\LM-studio\model\gpustack\bge-m3-GGUF\bge-m3-Q8_0.gguf`）。实测 **1024 维、模长 1.0（已归一化）、CLS pooling、非因果** → `Vector(1024)` + `vector_cosine_ops` 可定。⚠ 同一服务还挂着 `text-embedding-nomic-embed-text-v1.5` = **768 维**，模型名必须写死。
    **好消息（2026-11-19 实测）**：`spec_chunks.embedding` 建成 `vector(1024)` 后，维度由 pgvector 硬约束——插 3 维直接报 `expected 1024 dimensions, not 3`。所以选错模型是**响亮失败**，不会静默混存两种维度；启动期校验仍要做（早报错比写到一半炸好），但它不是唯一防线。
    ⚠ 查维度别用 `atttypid::regtype::text`，它只渲染成 `vector`；维度在 `pg_attribute.atttypmod`。`migrate_add_spec_embedding.py` 第一版就因此误报过。
    ⚠ SQL 脚本里写这张表的列要注意 `end` 是 Postgres 保留字，得 `"end"`（ORM 自动加引号，应用侧不受影响）。
  - **三个静默坑**（都会让检索"看起来能跑，其实不准"）：① query 端检索前缀**必须应用侧自己加**——GGUF 元数据里没有任何前缀信息，实测不加 top1 相似度 0.4643、加了 0.5143 且 top10 排序改变；② **超长输入不报错、静默截断**（16,280 字仍返回 1024 维，与截断到 1200 字的余弦只有 0.8811），切块长度得自己控；③ `usage.prompt_tokens` 恒 0 → **embedding 成本无法从响应得到**，要记账只能自己数文本长度。
  - **确定性**：同一句两次编码余弦 0.9999999275，**不是逐位一致**（GPU 非确定性）。缓存键用文本哈希，别比向量。
  - **批量只比逐条快 1.6×**（66 条 2.17s vs 3.46s）→ 全库回填很便宜，**不必为回填设计异步任务体系**，轻量方案站得住。
  - **索引**：小库先精确扫描，**别急着上 HNSW**——ANN 索引带 `user_id` 过滤会静默少返回 top-k（§3.1 的隔离要求正好触发这个坑）。到万级再上，并配合过滤策略重审。**已按这条定策：迁移脚本刻意不建任何 ANN 索引**，检索层第一版直接顺序扫描。
  - **embedding 成本怎么记（已定）**：端点 `usage.prompt_tokens` 恒 0，拿不到真实 token。成本代理 = **文本字符数**，而 `spec_docs.char_count` 与 `spec_chunks.text` 本来就在库里，`sum(len(chunk.text))` 就是编码量——**不需要为它加新列**。别为了记账去编一个 token 数字进 `llm_calls`。
  - **切块（2026-11-19 已解决）**：原先复用批改的 `split_document`，10,889 字 / 66 条款切成 6 块、**5 个边界全部把条款拦腰截断**，断点正好落在【判定标准】之前——检索拿到的是缺判据的半条条款。现在改用 `services/spec_chunking.py:split_by_clause`：**一块一条款**，章标题并入其下首条，超长条款才退回按句细分，切完自查三条不变式（切片相等 / 不重叠 / `reassemble == 原文`），破了就抛而不是静默入库。
    实测复验：同一份规范重传后 **66 块、0 块缺【判定标准】、0 块塞多条条款、平均每块 165 字**。
    ⚠ 语义变更：`spec_chunk_chars` 现在是"**单条条款上限**"，不是装箱目标（`config.py` 注释已改）。
    ⚠ 验不变式别用裸 `"".join`——块之间隔着被丢掉的空白，join 一定短几个字符，那不是我方缺陷，要走 `reassemble`。
  - **规范库不可变（已用测试钉住）**：`spec_storage` 只暴露 `ingest_spec_doc` / `delete_spec_doc`，没有 UPDATE 口子；`tests/test_spec_storage.py` 有两条回归——同名重传是新增一行而非覆盖、且模块里不存在 `update*/edit*/modify*` 函数。改了删了重传，别开原地更新的头。
  - **效度边界**：规范文档与评测夹具都是 AI 生成的，机制能学透，但**数字不能当真实世界结论**（简历别写"接入 RAG 提升 X%"，那是循环自证）。
  - **资产位置**：`server/fixtures/specs/文字编辑校对规范（教学示例稿）.md|.docx`（66 条款、四段式标签已统一为【判定标准】、零表格）；`eval/cases.py` 现有 **15 条夹具**（新 10 条带 `expect_clauses`，指向上述条款编号）。原始生成稿不归档——两份草稿 txt 已删且从未入库，别再引用 `_ai_generated_drafts/` 这个目录。
  - **embedding 列已加、向量已回填（2026-11-19 A1 收口）**：`server/migrate_add_spec_embedding.py` 已执行（幂等 `ADD COLUMN IF NOT EXISTS embedding vector(1024)`，故意不建 ANN 索引）；`models/spec_doc.py` 同步声明了该列，所以新库 `create_all()` 也直接带。66 块全部有向量（`backfill_spec_embeddings.py`，一批 2.17s / 全库含启动 26s）。回滚是 `DROP COLUMN`。
  - **`fallback_to_ocr` 参数仍未加进提取器**：`services/pdf.py` 的真实签名一直是 `extract_text_from_pdf(file_bytes)`。OCR 兜底最终落在**路由层**（上传受理 `needs_ocr` + 处理阶段 MinerU，§3.15），提取器自己不偷跑——与 SPEC §9.12 "由调用方决定要不要 OCR"的语义一致。双轨输出也已按该节落地：`plain_text` 守 §3.8 同构不变式，`structured`（full.md + content_list.json + images）落 `outputs/mineru/{log_id}/` 给 RAG 与未来的 PDF 原生批注。

## 8. 两份副本（**2026-11-16 起不再同步**）

`D:\Projects\demo\FileAgent` 是仓库外的独立交付副本（无 `.git`），原本给"不熟 Python 的开发者能直接部署"用，包含 `README.md` / `DEPLOY.md` / `deploy.ps1` / `requirements.txt`、注释按"去 AI 味"重写、剥掉 Java 对比块。

⚠ **用户 2026-11-16 决定不再投入精力同步 demo 副本**。现状：
- demo 落后主仓库 ≥ 5 轮（阶段 0/1/1.5/3/4 全套）；
- 已**不再以"补回逻辑"为目标**——下次有人问起 demo，回答"那是历史快照，最新代码看主仓库"；
- demo 目录本身**不删**，作为学习项目早期交付形态的存档；如果哪天真要给外人用，重写比追同步便宜。

这一段保留只为让下一个会话不被旧的"demo 落后四轮要不要同步"问题误导。

## 9. 提交

SPEC §3.3 约定 conventional commits（`feat: ...` / `fix: ...`），现有历史是中文短语（"优化1"/"优化2"）；新提交按 SPEC 约定写。
**不要自动 push / 部署 / 发布**；删文件、删数据、清历史前先问。