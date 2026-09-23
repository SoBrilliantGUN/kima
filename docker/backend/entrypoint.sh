#!/usr/bin/env sh
set -e

# 先跑迁移再起服务（容器首启对空库建表；DATABASE_URL 由 compose 注入，指向 db 服务）。
# 迁移失败直接退出，避免带半成品 schema 的服务在健康检查前就报「表不存在」。
alembic upgrade head

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
