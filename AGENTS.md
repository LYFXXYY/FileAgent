# AGENTS.md — FileAgent

本文件只记录**本项目特有、读代码不容易立刻发现**的约定与陷阱。
设计背景、Java→Python 对照、完整 API 表、DDL、缺陷修复台账见 [SPEC.md](SPEC.md)（权威设计文档），此处不重复。

## 1. 项目速览

文档 AI 处理平台：上传文档 → 按用户自定义 Prompt 调 LLM → 下载结果。**当前输入支持 `.docx` 与文字型 `.pdf`**（派发表 `api/v1/process.py:TEXT_EXTRACTORS`，白名单由它派生），批改类任务输出红字 diff 标注 + 可定位的问题清单；**结果永远是 `.docx`**。
平台定位已不止 Word，但扫描件（无文字层）不做 OCR、表格不进正文——改文案前先认清这一点，别把 UI 写成"任意文档全能处理"。
**批改类长文档**（> `LONGDOC_CHUNK_CHARS`，默认 6000 字）走 LangGraph 分块图扇出批改，见 §3.10；非批改类任务不分块。
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
uv run pytest -q                               # 142 passed；纯函数 + 假 client + 内存 SQLite + 合成 PDF，不连 MySQL 不出网
uv run ruff check .                            # 全绿
uv run ruff format --check .                   # 全部已格式化
uv lock --check                                # 依赖锁一致性
uv run python migrate_add_provider_capabilities.py         # 幂等：补 provider.capabilities
uv run python migrate_add_log_structured_output.py         # 幂等：补 log.structured_output
uv run python test_e2e.py                      # 端到端 33 项：需后端在 8000，会造数据，见 §7
uv run uvicorn test_llm_server:app --port 19000            # 假 OpenAI 端点（另开终端）
uv run python test_e2e_structural.py           # 结构化批改 + PDF + 分块端到端 52 项，零真实 token
uv run python -m eval.runner --provider 5 --case clean   # 评测台冒烟：**会真花钱**，先单条再全量
```

⚠ **`test_e2e_structural.py` 第 10 节要后端带 `LONGDOC_CHUNK_CHARS=60` 启动**才会真的切块；用默认 6000 时那节 12 项断言会失败（不是回归，是预算没到分块门槛）。跑完 `server/checkpoints/` 下会留两个 sqlite（图状态 + 按块缓存），已 gitignore，可随手删。

⚠ **8000 端口在本机可能"看不见地被占用"**：WSL 侧若有服务监听 8000，因 mirrored 网络，Windows `netstat` 查不到该监听，现象是 uvicorn 报 `[Errno 10048]` 而 `curl localhost:8000` 却能收到对方的 404。查证要跑 `wsl -e bash -lc "ss -ltnp | grep ':8000'"`。换端口时 `test_e2e_structural.py` 走 `FA_API` / `FA_MOCK_LLM` 环境变量，`test_e2e.py` 仍是硬编码 8000。

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
- 正文权威副本在 `operation_logs.original_content`，磁盘文件只是审计留存。两列声明已改为 `Text().with_variant(LONGTEXT, "mysql")`，与本机老库一致（MySQL `TEXT` 是 64KB **字节**上限，中文两万字就撞，长 PDF 是常态）；`structured_output` 仍是 `Text`，因为它只装问题清单。
- 提取层两条硬边界：`extract_text_from_docx` **只读 `doc.paragraphs`**（不含表格/文本框），`extract_text_from_pdf` **不读表格、不做 OCR**——已知功能边界，不是 bug。细则见 §3.8。
- 上传白名单 = `TEXT_EXTRACTORS.keys()`（`.docx` / `.pdf`），落盘名 `uuid4().hex + ext`。
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

### 3.7 结构化批改（阶段 1）

- **绝不要求模型输出坐标。** LLM 不数字符，让它给 `paragraph_index` / `char_offset` 会大面积错位且错位无信号，批注指到错位置比不指更糟。模型只逐字摘 `original` 片段，`start/end` 由 `services/grading.py:_find_unused_span` 在 `original_content` 上精确匹配；匹配不到留 `None`，前端显示"未定位"。改这条前先想清楚怎么验证错位——无法验证的东西不要交给模型。
- **同名片段依次认领不同出现位置**：`claimed` 区间列表保证多条相同 `original` 不会全指到第一处，且区间互不重叠。
- **解析失败必须降级而不是 fail**：端点吐自由文本、缺 `revised_text`、被围栏包裹都是常态。`parse_grading_output` 退回"整段输出当修订文 + `degraded=True`"，diff 下载行为与阶段 0 完全一致。用户的文档必须拿到一个结果。
- **`revised_text` 是结构化清单的前提**：缺它就降级，不去猜一份修订文。批改任务仍把修订文写进 `processed_content`，所以 `create_diff_docx` 一行都不用改。
- 问题类型是**开放输入 + 后端归一化**：`normalize_type()` 中英别名收进 8 个 canonical 值，认不出归 `other`。加枚举要同时改 `schemas/grading.py` 的 `_TYPE_ALIASES` 和前端 `TYPE_COLOR` / `TYPE_LABEL`。硬枚举会让换模型时整片报错。
- 单条意见不合契约只丢那一条，不连累整份清单。
- `structured_output` 为 NULL 表示非批改任务或历史数据；有值但 `degraded=true` 表示模型没守格式。**两者语义不同，`grading_degraded` 必须单独透给前端**，否则"没发现问题"和"没结构化"都显示 0 条，等于撒谎。
- 日志列表只带 `issue_count`，不带整份清单（`api/v1/logs.py:_to_response` 解析 JSON 取条数）。
- 计量 `purpose` 现在分 `chat` / `grading` / `grading_degraded`，评测台按它分组。新增调用点要传对的 purpose。
- 结构化只在 `task.is_grading == True` 生效，普通任务走 `chat` 不走 `chat_json`。

### 3.8 文档提取层（阶段 1.5）

- **PDF 的提取结果必须与 docx 同构**（`\n` 分隔的自然段）。diff、`_find_unused_span` 定位、`create_diff_docx`、前端 `buildParagraphs` 全押在"一行一段"这个约定上；PDF 交出"每视觉行一段"就会把半句话当段落、批注指到换行处。要改 `services/pdf.py` 先跑 `test_e2e_structural.py` 第 8 节，它直接断言两种来源提取出的字符串相等。
- **两遍拼接**：block 内按行拼（Word/LaTeX 导出件），block 间按几何邻接拼（幻灯导出件每行一个 block）。邻接合并三条同时成立才合：垂直间隙 ≤5pt、左边缘差 ≤3pt、上一段没以句末标点收束。**左边缘条件不能删**——它挡的是双栏排版里上下对齐的两栏，跨栏缝合比切段更糟。
- **拼接规则里 CJK 不加空格**是必须的：加了空格 `original` 片段与 `original_content` 就对不上，整条定位链路失效。
- **页码判据必须带位置条件**：纯数字/罗马数字 + 落在上 8% 或下 20% 留白带。只按内容判会把正文里的年份、条款号、表格序号当页码删掉（`2024` 恰好是四位数）。
- **跨页高频装饰用频次判，不硬编码字符串**，且三条护栏：文档 ≥3 页、文本 ≤20 字符、**带句末标点的一律放过**。丢内容的代价远大于留噪声。
- 失败一律 `PdfError` → 路由转 400（打不开 / 有打开密码 / 没有文字层）。**扫描件绝不回空文本**——那会让用户以为文档本来就是空的。
- 依赖：`pymupdf` 2.x 把版面分析（`pymupdf-layout` → onnxruntime + numpy）和 Markdown 导出（`pymupdf4llm` → networkx + tabulate）做成了**硬依赖**。`services/pdf.py` 只用 `get_text("blocks")`，对这些零引用，所以 `pyproject.toml` 的 `[tool.uv] override-dependencies` 用"永不成立的 marker"把它们摘掉了：锁 **75 包**、`.venv` **233M**（摘之前是 84 包 / 337M）。**阶段 5 真要用版面分析时，删掉那几行 override 就能装回来**，别把它当成已经付过的成本。`import pymupdf`，**不要用 `fitz`**（2.x 里已弃用，会打 FutureWarning）。

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

## 4. 前端不可违反的约定

- **token 双写**：登录成功同时写 Pinia store 和 `localStorage`；axios 拦截器只读 `localStorage`（在 API 层拿不到 store）。key 常量集中在 `client/src/api/client.ts`（`fileagent_token` / `fileagent_username`），别处不要硬编码。- 401 处理要排除 `/auth/login`（那是密码错误），用 `window.location.replace` 带 `redirect` 参数（拦截器层拿不到 router 实例）。
- 自动重试**只针对无响应的 GET**，1 次 / 800ms；写操作绝不重试。
- 处理结果**一律靠轮询 `status` 拿**，不要依赖 `POST /process` 的响应体（它只返回 processing）。`POLL_INTERVAL=5000`、`MAX_POLL_COUNT=120`（10 分钟上限）。
- `AppLayout.vue:119` 用 `<keep-alive>` 包住 router-view，工作台切页不销毁 → 必须成对处理 `onActivated`（恢复状态 + 重启轮询）/ `onDeactivated`（停轮询）。
- **Pinia setup store 只用 `ref` + `watch`**，不要用对象字面量 getter/setter 代理闭包变量：Pinia 会把访问器转成普通数据属性，导致 `restore()` 后 UI 拿不到值、setter 不触发保存（`client/src/stores/home.ts:16`）。
- 持久化用 `sessionStorage`（key `fileagent_home_state`），**不存 File 对象**，恢复时 `pickedFile` 置 null。
- 路径别名 `@` → `src`，`vite.config.ts` 与 `tsconfig.app.json` 两处都要配。
- Element Plus 是**按需引入**（`vite.config.ts` 的 `unplugin-vue-components` + `ElementPlusResolver`），首屏 JS 从 893kB 降到 ~436kB，构建的体积告警随之消失。三件插件管不到的东西必须在 `main.ts` 手工接上，漏了就是"构建通过、界面上裸样式"：**ElMessage / ElMessageBox / v-loading**（函数式与指令，不是标签，扫不到）以及它们各自的样式、还有中文 locale（改成 App.vue 里的 `el-config-provider`，不要再 `app.use(ElementPlus)`）。`src/components.d.ts` 是构建生成的，已 ignore，删掉也能重新生成。
- 改了样式装载方式，验证不能只看 `npm run build` 过了：把 `dist/assets/*.css` 拼起来 grep 每个用过的 `el-*` 类名，才能确认样式真的进了产物（`el-option` / `el-tab-pane` / `el-config-provider` 三个是名字对不上，不是缺样式——它们的 style 入口引的是父组件 CSS）。真实交互（弹窗样式、loading 遮罩、分页中文文案）仍未在浏览器里跑过。
- provider 的 `capabilities` 后端存逗号分隔**字符串**、表单要的是**数组**，转换只认 `KNOWN_CAPABILITIES` 里的已知值（`client/src/views/Providers.vue` 的 `toCapabilityList()` / `buildPayload()`）——顺带做了顺序归一化和脏值丢弃，未知标记不会把表单卡死也不会吃 422。提交**必须走 `buildPayload()`**，别把带 `capabilityList` 临时字段的 form 整个发出去。
- 上传格式只在两处声明：`Home.vue` 的 `accept=".docx,.pdf"` 和上传区文案。**前端不做扩展名校验**，`accept` 只管文件选择对话框，拖拽照样能拖进 `.txt`——真校验在后端白名单，400 文案已是给用户看的。加格式时要同时改这两处 + 后端派发表。
- 结构化批改的原文高亮在 `Home.vue:buildParagraphs()`：按 `\n` 分段，用全局偏移 `start/end` 落到段内区间再套 span。**非高亮片段也必须给 `index: null`**（`Span.index` 是 `number | null` 必填），漏了会让相邻片段的 `seg.index` 是 `undefined`，点击时把 `activeIssueIndex` 写成 `undefined` 从而永不匹配。匹配不到位置的 issue 显示"未定位"，不要猜位置。
- `gradingIssues` / `gradingDegraded` 和 `processedContent` 一样要走 `syncToStore()` + `home.ts` 持久化，并在 `reset()` / `goBackToUpload()` 里成对清空；`applyResult()` 是唯一铺结果到 UI 的入口（3 处轮询/上传回调都调它），别在调用点各自赋值。

## 5. 数据库变更

**没有 Alembic**。启动跑 `Base.metadata.create_all()`，它**只建新表、绝不 ALTER 已有表**。
改列/加约束要写一次性幂等迁移脚本，范例 `server/migrate_add_provider_user_id.py`、`server/migrate_add_provider_capabilities.py`（先 `SHOW COLUMNS` 判存在再动；前者回填目标用户名硬编码 `LYF`，已执行过）。
新增 ORM 类必须在 `server/models/__init__.py` 里 import，否则 `create_all()` 发现不了这张表。

⚠ **"新库"和"本机老库"的结构已经不一样了**（2026-11-14 实测 `SHOW COLUMNS` + `information_schema.REFERENTIAL_CONSTRAINTS`），排查外键类问题前先认清在哪套库上：

| 项 | ORM / SPEC 声明 | 本机老库实际 |
|---|---|---|
| `tasks.provider_id` / `operation_logs.task_id` 删除规则 | `ON DELETE SET NULL` | **NO ACTION** |
| `operation_logs.status` | `nullable=False` | **NULL 允许**，default `pending` |
| `original_content` / `processed_content` | ~~`Text`~~ 已改 `with_variant(LONGTEXT, "mysql")` | **LONGTEXT**（与新声明一致） |
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

当前路线（阶段 0–5，含为什么这么排）在 SPEC §9.2；阶段 0 交付与验证明细在 SPEC §9.3，阶段 1 在 §9.4，阶段 1.5（PDF 输入 + 去 Word 化）在 §9.5，阶段 2 最小版（评测台 + 基线数字）在 §9.6，阶段 3（长文档分块批改图）在 §9.7，缺陷台账第五轮在 §10，改 bug 前先看这几张表。

门禁基线（均已实测通过）：`uv run pytest -q` → **142 passed**；`uv run ruff check .` → 全绿；`ruff format --check .` → 54 files already formatted；`uv lock --check` → 一致（**75 包 / .venv 233M**，摘掉 pymupdf 的版面分析死重之前是 84 包 / 337M）；`npm run build` → 通过；`uv run python test_e2e.py` → 33/33；`uv run python test_e2e_structural.py` → **52/52**（第 10 节要 `LONGDOC_CHUNK_CHARS=60`）。


- **`server/test_e2e.py` 是仓库自带的端到端冒烟脚本，不是遗留物，别删**：需后端已在 8000 运行、连真实 MySQL。它会注册 `tester_*` / `other_*` 两个临时账号，造 1 条日志和 1 个上传文件；脚本自身只删 task/provider，**账号、日志、落盘文件、以及新增的 `llm_calls` 行要跑完手动清**。第 7 节现在真走重试路径（连不上假 provider → 重试 3 次 → failed），第 10 节直接改库把日志置为 done 来验证下载，都属预期行为。
- **`server/test_e2e_structural.py` + `test_llm_server.py` 是阶段 1 新增的结构化端到端**，同样别删：需后端 + 假 LLM 两个进程都在跑，全程零真实 token。mock 端点**刻意不支持 `response_format`**（回 400）来如实模拟国产兼容端点并逼出 `chat_json` 的降级路径，所以 LLM 日志里出现的 `400 Bad Request`（每次 `chat_json` 两条：`json_schema` 和 `json_object` 各一档）是预期行为不是故障。第 8 节是阶段 1.5 加的 **PDF 输入链路**：用 pymupdf 现场合成"每视觉行一个 block"的中文 PDF，断言提取结果与 docx **逐字相等**、定位成功、下载名换成 `.docx`——改提取器必跑它。它注册 `e2e_s_*` 账号，跑完清理方式和上面一样。
- 单测**不连 MySQL 也不出网**：HTTP 走假 client（替 `services.llm.get_client`），provider 路由走内存 SQLite。所以 `pytest` 绿不代表能连上数据库，端到端才是那一层的验证。加测试请保持这个边界，也别引入 `pytest-asyncio`——现有测试直接 `asyncio.run()` 就够。
- `uploads/`、`outputs/`、`.env`、各类缓存已 ignore；`.env.example` 靠 `.gitignore` 里的 `!.env.example` 例外入库。
- SPEC §11 示例写 `JWT_EXPIRE_HOURS=2`，本机 `.env` 实际是 24；`.env` 不入库，以本机为准。
- 未做：限流、api_key 字段级加密、Alembic、Docker / 多 worker、表格提取（docx 表格和 PDF 表格都不进正文）、扫描件 OCR、加密 PDF 解密、老库外键规则对齐、成本价格表（计量只存 token 不算钱）、问题清单写进交付 docx。阶段 3 未做：`interrupt` 人工确认分块方案（要先有 UI）、长文摘要的两段式（逐块摘要 + 整篇汇总）、`checkpoints/` 体积治理与 TTL、多 worker 下的 Postgres saver/store。评测台的下一步：真实文档夹具、第二厂商当裁判、同夹具跑重复轮次（单轮会漂）、把 `format_tier` 落进 `llm_calls`（改表结构，做前先问）。
- 真机跑过的一手结论（2026-11-15，qwen3.7-flash + 合成中文 PDF）：结构化链路 OK（`degraded=False`、4 条 的/地/得 全部精确定位）。但该端点回来的 `revised_text` 与原文一字不差——意见对、修订没落地。这是 prompt 产出质量，不是提取或解析缺陷，正属阶段 2 评测台要量化的范围，别在提取层"顺手改 prompt"掩盖它。

## 8. 两份副本，手动同步

`D:\Projects\demo\FileAgent` 是**仓库外的独立交付副本**（无 `.git`），给不熟 Python 的开发者部署用：

- 它有 `README.md` / `DEPLOY.md` / `deploy.ps1` / `.env.example` / `requirements.txt`，**没有 SPEC.md**。
- 它的注释已按"去 AI 味"重写，**剥掉了 Java 对比块**；主仓库保留。
- 两边业务逻辑应保持一致，但**没有任何自动同步**：主仓库改了逻辑要手动同步过去（反之亦然），注释风格各自保留。
- ⚠ **pip 侧表达不了这个 override**：主仓库靠 `pyproject.toml` 的 `[tool.uv] override-dependencies` 摘掉 pymupdf 的版面分析死重，demo 副本的 `requirements.txt` 用 pip 装 `pymupdf` 会把 onnxruntime + numpy 一起拖回来（多约 100M）。要在 demo 侧也省掉，只能写 `--no-deps` 的手工装法或者接受体积差异 —— 属交付决定，做前先问用户。
- ⚠ **demo 副本目前落后主仓库四轮（阶段 0 + 1 + 1.5 + 3）**，实测确认它仍是老版：`services/llm.py` 只有 `call_llm`，没有 `models/llm_call.py`、没有 `capabilities`、没有 `structured_output`、没有 `services/pdf.py`、没有 `services/chunking.py` / `graphs/`。同步时要一并带过去：新版 `services/llm.py`、`services/grading.py`、`services/pdf.py`、`services/chunking.py`、`graphs/__init__.py` + `graphs/grading_graph.py`、`schemas/grading.py`、`models/llm_call.py`、两个 migrate 脚本、`test_llm_server.py` 与 `test_e2e_structural.py`、`.env.example` 的新配置项（6 个 LLM/日志 + 3 个分块），以及前端 provider 能力多选、Home.vue 问题清单卡片、上传区 `.pdf` 白名单与文案。demo 侧 `pyproject.toml` 已是 `fileagent-server`，**`requirements.txt` 必须补 `pymupdf` + `langgraph` + `langgraph-checkpoint-sqlite`**（阶段 1.5 与 3 的运行时依赖，漏 pymupdf 则 PDF 上传 500，漏 langgraph 则长文档批改 500）；`.gitignore` 里没有 `.env.example` 之外还要留 `checkpoints/`。注释仍按 demo 的去 AI 味风格重写（不带 Java 对比块）。要不要同步、同步到哪一轮，属于交付决定，先问用户。

## 9. 提交

SPEC §3.3 约定 conventional commits（`feat: ...` / `fix: ...`），现有历史是中文短语（"优化1"/"优化2"）；新提交按 SPEC 约定写。
**不要自动 push / 部署 / 发布**；删文件、删数据、清历史前先问。