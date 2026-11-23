# FileAgent 后端镜像（docker-compose 的 backend 服务用）
# 构建上下文是**仓库根**，所以路径都带 server/ 前缀：
#   docker compose build backend   或   docker build -f deploy/Dockerfile .
#
# 注意：run_backend.py 里那段 Windows SelectorEventLoop 补丁在 Linux 上是空操作
# （代码里有 sys.platform == "win32" 判断），Linux 直接跑 asyncio 默认循环即可。

FROM python:3.12-slim

# uv 官方镜像里只取一个二进制，比 pip install uv 少一层版本漂移
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 先只拷锁文件，让依赖层能被 Docker 缓存住（改代码不会重装依赖）
COPY server/pyproject.toml server/uv.lock ./

# --no-dev：生产镜像不带 pytest / ruff
RUN uv sync --frozen --no-dev --no-install-project

COPY server/ ./
RUN uv sync --frozen --no-dev

# 运行期产生的三类目录都指到卷上（compose 里挂载），镜像本身不写数据
ENV PYTHONUNBUFFERED=1 \
    UPLOAD_DIR=uploads \
    OUTPUT_DIR=outputs \
    LOG_FILE=logs/backend.log

EXPOSE 8000

# 必须走这个入口，不能换成 uvicorn main:app：
# 它负责事件循环策略、NO_PROXY 归一化与 uvicorn loop 工厂补丁（AGENTS §2）
CMD ["uv", "run", "python", "run_backend.py"]
