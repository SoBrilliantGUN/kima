# kima 前端（React + Vite + pnpm）。
#
# 多阶段构建：构建层用 node 装依赖 + vite build 出静态产物，运行层用 nginx 托管并
# 反代 /api、/health 到 backend 服务。生产走同源反代（前端 BASE_URL=''，全相对路径），
# 前端代码无需改动、CORS 不参与。
#
# context 用仓库根（docker-compose 里 context: .），与 backend 镜像对称；
# 根 .dockerignore 排除 node_modules/dist 等，保证构建上下文轻量。

FROM node:20-bookworm-slim AS build

WORKDIR /app

# 用 corepack 启用 package.json 声明的 pnpm 版本（12.3.4）
RUN corepack enable

# 依赖清单先行，依赖未变时命中层缓存。pnpm-workspace.yaml 必须随清单带入：pnpm 12
# 的 allowBuilds（esbuild/@parcel/watcher 构建脚本白名单）放在这里，缺了会报
# ERR_PNPM_IGNORED_BUILDS。.npmrc 的 minimumReleaseAge=0 同理，否则拒绝「发布未满 24h」。
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/.npmrc frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

# 全量源码 + 构建（tsc --noEmit && vite build）
COPY frontend/ ./
RUN pnpm build

# 运行层：nginx 托管静态产物，反代配置见 docker/frontend/nginx.conf
FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY docker/frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
