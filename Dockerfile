# 多阶段构建:uv 装依赖 -> slim runtime
#
# 多架构构建(可选,跨 arm64/amd64):
#   docker buildx create --use                  # 一次性
#   docker buildx build --platform linux/arm64,linux/amd64 -t readflow:0.3 --push
# 注意:多架构镜像必须 --push 到 registry(--load 不支持多平台 list)。
# 单机自托管用 docker compose(默认构建当前主机架构)即可。

# ---- stage 1: 用 uv 装依赖到独立 venv ----
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app

# 先拷依赖清单,利用层缓存(代码改不动重装依赖)
COPY pyproject.toml uv.lock ./

# 装依赖到 /app/.venv(不装项目本身,--no-install-project)
# --frozen:严格按 lock,不解析;--no-cache:层缓存已够
RUN uv sync --frozen --no-install-project --no-dev

# ---- stage 2: runtime ----
FROM python:3.13-slim-bookworm AS runtime

WORKDIR /app

# 拷装好的 venv
COPY --from=builder /app/.venv /app/.venv

# 拷应用代码(foliate-js 是 git submodule,须在构建前初始化:
#   git submodule update --init
# 此处校验其存在,缺失则在 build 阶段即失败,避免运行时阅读页静默坏掉)
COPY app/ ./app/
RUN test -f /app/app/static/foliate-js/view.js \
    || { echo "ERROR: foliate-js submodule 缺失,请先 git submodule update --init"; exit 1; }

# 容器内固定挂载点(宿主路径由 compose 配)
ENV READFLOW_DATA_DIR=/data \
    READFLOW_LIBRARY_DIR=/books-library \
    READFLOW_HOST=0.0.0.0 \
    READFLOW_PORT=8765 \
    PATH="/app/.venv/bin:$PATH"

# 挂载点目录由 compose 创建,这里 ensure 一下
RUN mkdir -p /data /books-library

EXPOSE 8765

# 健康检查:进程存活 + HTTP 响应
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3); sys.exit(0)" || exit 1

# 单进程 uvicorn + watcher(同进程 lifespan),单 worker 无 reload
CMD ["python", "-m", "app"]
