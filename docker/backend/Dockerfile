# kima 后端（FastAPI + LangGraph Copilot）。
#
# 目的：把后端进程放进 Linux 容器，消除「Windows 裸跑 + ProactorEventLoop 下 psycopg
# async 不可用 → AsyncPostgresSaver 降级 InMemorySaver」的环境分裂（见 main.py:79-102）。
# 容器内是 Linux + SelectorEventLoop，psycopg async 正常，Copilot checkpoint 落 Postgres、
# 跨重启可恢复。
#
# 用 uv 装依赖（仓库以 uv.lock 锁定版本）。Playwright 无头浏览器随镜像安装，用于
# SPA 页面抓取（静态抓取为空壳时回退到浏览器渲染，见 integrations/web.py）。

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# UV_LINK_MODE=copy：避免 .venv 内软链接在容器层级拷贝时失效
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# 依赖清单先行，依赖未变时命中层缓存
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Playwright 无头浏览器：chromium + 系统依赖（SPA 抓取需要，约 +200MB 镜像体积）
RUN playwright install --with-deps chromium

# 全量源码 + 项目本体 + 启动脚本
COPY backend/ ./
COPY docker/backend/entrypoint.sh /entrypoint.sh
RUN uv sync --frozen --no-dev \
    && chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
