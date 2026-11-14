# AGENTS.md — FileAgent

本文件只记录**本项目特有、读代码不容易立刻发现**的约定与陷阱。
设计背景、Java→Python 对照、完整 API 表、DDL、缺陷修复台账见 [SPEC.md](SPEC.md)（权威设计文档），此处不重复。

## 1. 项目速览

Word 文档 AI 处理平台：上传 `.docx` → 按用户自定义 Prompt 调 LLM → 下载结果（批改类任务输出红字 diff 标注）。
前后端分离单体，当前阶段无消息队列、无 Redis、无 Docker。

| 层 | 位置 | 技术 |
|---|---|---|
| 后端 | `server/` | FastAPI + SQLAlchemy 2.0 + PyMySQL；Python 3.12（`.python-version`）；uv 管依赖 |
| 前端 | `client/` | Vue 3 `<script setup>` + TS + Vite + Element Plus + Pinia |
| 数据库 | MySQL `localhost:3306/file_agent` | 5 表：`users` / `model_providers` / `tasks` / `operation_logs` / `llm_calls` |

分层：`api/v1`（路由）→ `services`（业务）→ `models`（ORM）；`schemas` 是 Pydantic 出入参；`middleware/auth.py` 提供 `get_current_user` 依赖。
核心链路三段分离：`POST /upload` → `POST /process/{log_id}` → `GET /download/{log_id}`，状态存在 `operation_logs`（`pending → processing → done / failed`）。

## 2. 常用命令（均已实测）

后端一律先 `cd server`——pytest 的 `pythonpath=["."]` 和 `.env` 定位都以此为基准：

```bash
uv sync                                        # 装依赖（含 dev 组）
uv run uvicorn main:app --reload --port 8000    # 起后端，Swagger 在 /docs
uv run pytest -q                               # 62 passed；纯函数 + 假 client + 内存 SQLite，不连 MySQL 不出网
uv run ruff check .                            # 全绿
uv run ruff format --check .                   # 全部已格式化
uv lock --check                                # 依赖锁一致性
uv run python migrate_add_provider_capabilities.py   # 幂等：补 provider.capabilities 列，新库部署后要跑
uv run python test_e2e.py                      # 端到端 33 项断言：需后端已在 8000 运行，会造数据，见 §7
```

前端 `cd client`：

```bash
npm install
npm run dev      # 5173，/api 代理到 8000
npm run build    # = vue-tsc -b && vite build，唯一的类型门禁（无 eslint / prettier）
```

跑后端前需要 `server/.env`（不入库）：复制 `server/.env.example` 为 `.env`，填 `DATABASE_URL` 和 `JWT_SECRET_KEY`。
全部字段见 `server/config.py`；**新增字段必须同时补进 `.env.example`**。

## 3. 后端不可违反的约定

### 3.1 数据隔离（安全底线）

- **每个查询都带 `user_id == current.id`**，providers / tasks / logs / process 四个路由无一例外，新接口照抄。
- `user_id` 只取自 `Depends(get_current_user)`，**绝不接受请求体传入**（`server/api/v1/providers.py:50`、`server/api/v1/tasks.py:46`）。
- 越权一律 **404 而非 403**，不暴露"资源存在但不属于你"（`providers.py:71`、`tasks.py:63`）。
- task 引用 provider 时必须校验 `provider.user_id == current.id`（`tasks.py:41`、`process.py:151`）。
- `api_key` **永不出现在响应里**：走 `ProviderResponse.from_orm_masked()`（前 4 位 + `****`）。LLM 错误响应体截断 500 字符，`error_message` 截断 2000。

### 3.2 删除顺序（否则外键约束 → 500）

必须**先置空引用再删**：

- 删 provider → 先 `Task.provider_id = None`（`providers.py:100`）
- 删 task → 先 `OperationLog.task_id = None`（`tasks.py:92`）

模型侧**声明**了 `ON DELETE SET NULL`（`task.provider_id` / `log.task_id`），但本机老库里实测是 **`NO ACTION`**——`create_all()` 从不 ALTER，s2 修复只动了 ORM（见 §5）。所以应用层那次置空**不是双保险，而是唯一防线**：新加删除路径必须照抄，漏了就 500。
`llm_calls` 是新表，外键规则确实是 `SET NULL` / `CASCADE`（实测 `information_schema.REFERENTIAL_CONSTRAINTS`）。日志和计量靠 `task_name` / `provider_name` 快照在关联被删后仍可读。

### 3.3 处理必须后台化

`POST /process/{log_id}` **立即返回 processing**，真正干活在 `_run_processing`（`server/api/v1/process.py:176`）：

- 必须用独立 `SessionLocal()`——请求级 `db` 在响应发出后就关闭了。
- **每一条失败路径都要 `_mark_failed()`**，绝不允许日志停在 processing（前端会无限轮询）。
- 只有 `pending` / `failed` 可触发；`GET /process/{id}/status` 纯只读，不会重复触发 LLM。
- `BackgroundTasks` 不持久化 → `server/main.py:75` 启动时把僵尸 processing 重置为 failed。**该 reaper 只在单 worker 下正确**；上多 worker 前必须换成带超时阈值的 reaper 或任务队列。
- provider 一律走 `resolve_provider()`：task 指定 > 当前用户默认 active（按 id 升序 + 按 `capabilities` 筛）。指定的 provider 失效或能力不匹配要**显式报错**，不许静默 fallback 到别的模型。
- 请求里已对"task 指定了 provider"提前校验（`process.py:154`），让这类错误在返回 processing **之前**就是 400，而不是后台跑一半才 failed。

### 3.4 配置读取

**新增配置项必须先在 `config.py` 的 `Settings` 里声明字段**，再用 `get_settings().xxx` 读。
pydantic-settings 只把 `.env` 注入 Settings 对象，**不写 `os.environ`** → `os.getenv("XXX")` 读 `.env` 永远拿到 None。CORS 曾因此成为死配置（SPEC §10 s10）。

### 3.5 其他后端事实

- 同步/异步约定：纯 DB 操作用 `def`（FastAPI 自动丢线程池），涉及外部 HTTP 才 `async def`。已知同步阻塞点用 `# noqa: ASYNC230` 并写明理由（`process.py:89`）。
- 日志用 `logging.getLogger("fileagent...")`，**不要 print**（Windows GBK 控制台中文乱码）；独立脚本需 `sys.stdout.reconfigure(encoding="utf-8")`。root handler 已在 `main.py:46` 的 `_setup_logging()` 里配好，级别走 `LOG_LEVEL`。⚠ 那个 `basicConfig(force=True)` 不能省——`--reload` 会反复重导入模块，不清掉旧 handler 每行日志会打印两遍。
- `LogStatus` 是 `enum.StrEnum`，配 `Enum(..., values_callable=...)` 存 **value**（`"pending"`）而非 name；MySQL 列类型为 `enum('pending','processing','done','failed')`。改枚举要连带回归后端状态流转与前端字符串比较。
- 正文权威副本在 `operation_logs.original_content`，磁盘文件只是审计留存。ORM 声明的是 `Text`，但**本机真实列是 `LONGTEXT`**（早期按 SPEC §6 手工建的表，`create_all()` 不会 ALTER），所以实际没有 65KB 限制；新建库会拿到 `TEXT`，两者行为不同要留意。
- `extract_text_from_docx` **只读 `doc.paragraphs`，不含表格/文本框**（`server/services/word.py:30`）——已知功能边界，不是 bug。
- 上传白名单只有 `.docx`，落盘名 `uuid4().hex + ext`。
- 认证：直接用 bcrypt，**不要引 passlib**（1.7.4 与 bcrypt 4.1+ 不兼容），密码截 72 字节；JWT HS256，`sub` 是字符串化 user_id；无 `/auth/me`、无刷新 token。
- 下载中文文件名必须 RFC 5987：`filename*=UTF-8''{quote(...)}`（`process.py:324`）。
- diff 用 `SequenceMatcher(autojunk=False)`——**关 autojunk 对中文是必须的**，否则"的/是/在"被当噪声导致结果不准（`server/services/diff.py:30`）。
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

## 4. 前端不可违反的约定

- **token 双写**：登录成功同时写 Pinia store 和 `localStorage`；axios 拦截器只读 `localStorage`（在 API 层拿不到 store）。key 常量集中在 `client/src/api/client.ts`（`fileagent_token` / `fileagent_username`），别处不要硬编码。
- 401 处理要排除 `/auth/login`（那是密码错误），用 `window.location.replace` 带 `redirect` 参数（拦截器层拿不到 router 实例）。
- 自动重试**只针对无响应的 GET**，1 次 / 800ms；写操作绝不重试。
- 处理结果**一律靠轮询 `status` 拿**，不要依赖 `POST /process` 的响应体（它只返回 processing）。`POLL_INTERVAL=5000`、`MAX_POLL_COUNT=120`（10 分钟上限）。
- `AppLayout.vue:119` 用 `<keep-alive>` 包住 router-view，工作台切页不销毁 → 必须成对处理 `onActivated`（恢复状态 + 重启轮询）/ `onDeactivated`（停轮询）。
- **Pinia setup store 只用 `ref` + `watch`**，不要用对象字面量 getter/setter 代理闭包变量：Pinia 会把访问器转成普通数据属性，导致 `restore()` 后 UI 拿不到值、setter 不触发保存（`client/src/stores/home.ts:16`）。
- 持久化用 `sessionStorage`（key `fileagent_home_state`），**不存 File 对象**，恢复时 `pickedFile` 置 null。
- 路径别名 `@` → `src`，`vite.config.ts` 与 `tsconfig.app.json` 两处都要配。
- Element Plus 是**全量引入 + 全量 CSS**（`client/src/main.ts:10`），所以尽管路由做了懒加载，产物仍是单个 ~890kB chunk，build 的体积告警是已知现象。
- provider 的 `capabilities` 后端存逗号分隔**字符串**、表单要的是**数组**，转换只认 `KNOWN_CAPABILITIES` 里的已知值（`client/src/views/Providers.vue` 的 `toCapabilityList()` / `buildPayload()`）——顺带做了顺序归一化和脏值丢弃，未知标记不会把表单卡死也不会吃 422。提交**必须走 `buildPayload()`**，别把带 `capabilityList` 临时字段的 form 整个发出去。

## 5. 数据库变更

**没有 Alembic**。启动跑 `Base.metadata.create_all()`，它**只建新表、绝不 ALTER 已有表**。
改列/加约束要写一次性幂等迁移脚本，范例 `server/migrate_add_provider_user_id.py`、`server/migrate_add_provider_capabilities.py`（先 `SHOW COLUMNS` 判存在再动；前者回填目标用户名硬编码 `LYF`，已执行过）。
新增 ORM 类必须在 `server/models/__init__.py` 里 import，否则 `create_all()` 发现不了这张表。

⚠ **"新库"和"本机老库"的结构已经不一样了**（2026-11-14 实测 `SHOW COLUMNS` + `information_schema.REFERENTIAL_CONSTRAINTS`），排查外键类问题前先认清在哪套库上：

| 项 | ORM / SPEC 声明 | 本机老库实际 |
|---|---|---|
| `tasks.provider_id` / `operation_logs.task_id` 删除规则 | `ON DELETE SET NULL` | **NO ACTION** |
| `operation_logs.status` | `nullable=False` | **NULL 允许**，default `pending` |
| `original_content` / `processed_content` | `Text` | **LONGTEXT** |
| `model_providers.capabilities`、整张 `llm_calls` | — | 已按 ORM 建好，规则正确 |

根因就是 `create_all()` 不 ALTER：这几张表早期按 SPEC §6 手工建，后来的修复只动了 ORM。**要对齐老库得单独写重建外键的迁移脚本，属改表结构操作，做前先问用户。**

## 6. 注释与代码风格

- 注释、docstring、错误消息、API 描述**全部中文**。
- 本仓库是学习项目：核心模块保留 `【Java 对比】` 教学块（Spring / JPA 类比），**有意为之，不要当冗余删掉**。
- 核心业务（隔离、后台处理、diff、状态机）注释可以详细；样板 CRUD 保持简洁。
- 不写"本次修复 / 为某需求添加"这类会随代码腐烂的注释。
- ruff：line-length 100、target py312。忽略项都有原因，别随手删——`B008`（FastAPI 把 `Depends()` 写在默认参数是框架约定）、`RET501`（204 显式 `return None` 更清楚）、`RUF001/002/003`（中文标点会被判歧义 Unicode，全量误报）。
- **`E501` 把东亚宽字符按宽度 2 计**，所以中文注释/字符串的实际字符数要砍到约 50 才稳；判超长别数字符，用 `unicodedata.east_asian_width` 算宽度。
- **`ASYNC109` 禁止 async 函数用名为 `timeout` 的参数**。要往下透传 `httpx.Timeout` 对象时把参数叫 `timeouts` 或 `read_timeout` 就没事，别为它加 `# noqa`——真有理由的阻塞点才用 `# noqa: ASYNC230` 并写明原因。

## 7. 当前基线与已知缺口

当前路线（阶段 0–5，含为什么这么排）在 SPEC §9.2；阶段 0 的交付与验证明细在 SPEC §9.3，改 bug 前先看那两张表。

门禁基线（均已实测通过）：`uv run pytest -q` → **62 passed**；`uv run ruff check .` → 全绿；`ruff format --check .` → 全部已格式化；`uv lock --check` → 一致（46 包，阶段 0 未新增依赖）；`npm run build` → 通过；`uv run python test_e2e.py` → 33/33。

- **`server/test_e2e.py` 是仓库自带的端到端冒烟脚本，不是遗留物，别删**：需后端已在 8000 运行、连真实 MySQL。它会注册 `tester_*` / `other_*` 两个临时账号，造 1 条日志和 1 个上传文件；脚本自身只删 task/provider，**账号、日志、落盘文件、以及新增的 `llm_calls` 行要跑完手动清**。第 7 节现在真走重试路径（连不上假 provider → 重试 3 次 → failed），第 10 节直接改库把日志置为 done 来验证下载，都属预期行为。
- 单测**不连 MySQL 也不出网**：HTTP 走假 client（替 `services.llm.get_client`），provider 路由走内存 SQLite。所以 `pytest` 绿不代表能连上数据库，端到端才是那一层的验证。加测试请保持这个边界，也别引入 `pytest-asyncio`——现有测试直接 `asyncio.run()` 就够。
- `uploads/`、`outputs/`、`.env`、各类缓存已 ignore；`.env.example` 靠 `.gitignore` 里的 `!.env.example` 例外入库。
- SPEC §11 示例写 `JWT_EXPIRE_HOURS=2`，本机 `.env` 实际是 24；`.env` 不入库，以本机为准。
- 未做：限流、api_key 字段级加密、Alembic、Docker / 多 worker、表格提取、老库外键规则对齐、成本价格表（计量只存 token 不算钱，留给评测台阶段）。

## 8. 两份副本，手动同步

`D:\Projects\demo\FileAgent` 是**仓库外的独立交付副本**（无 `.git`），给不熟 Python 的开发者部署用：

- 它有 `README.md` / `DEPLOY.md` / `deploy.ps1` / `.env.example` / `requirements.txt`，**没有 SPEC.md**。
- 它的注释已按"去 AI 味"重写，**剥掉了 Java 对比块**；主仓库保留。
- 两边业务逻辑应保持一致，但**没有任何自动同步**：主仓库改了逻辑要手动同步过去（反之亦然），注释风格各自保留。
- ⚠ **阶段 0 的改动目前只在主仓库，demo 副本落后一整轮**（缺 `llm_calls` 表、`capabilities` 列、新版 `services/llm.py` 及其接线、前端能力多选）。同步时注意 demo 那边：`pyproject.toml` 已经是 `fileagent-server` 且有 `requirements.txt`，`.env.example` 要一并补 6 个新配置项，`migrate_add_provider_capabilities.py` 得带上，注释仍按 demo 的去 AI 味风格重写（不带 Java 对比块）。要不要现在就同步，属于交付决定，先问用户。

## 9. 提交

SPEC §3.3 约定 conventional commits（`feat: ...` / `fix: ...`），现有历史是中文短语（"优化1"/"优化2"）；新提交按 SPEC 约定写。
**不要自动 push / 部署 / 发布**；删文件、删数据、清历史前先问。