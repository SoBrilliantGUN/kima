# 自建 Postgres 镜像：在 pgvector/pgvector:pg16 基础上编译安装 pg_jieba 中文全文检索扩展。
# 多阶段构建：builder 编译扩展并安装，runtime 只拷贝产物，保持最终镜像精简。
#
# pg_jieba 默认提供文本检索配置 `jiebacfg`（精确分词模式），配合
# `to_tsvector('jiebacfg', content)` / `plainto_tsquery('jiebacfg', q)` 使用。

# ---- 构建阶段：编译 pg_jieba ----
FROM pgvector/pgvector:pg16 AS builder

# GitHub 镜像源（国内访问 github.com 不稳定时，构建时用 --build-arg 覆盖）：
#   例：docker compose build --build-arg GIT_MIRROR=https://ghproxy.com/https://github.com db
ARG GIT_MIRROR=https://github.com

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        postgresql-server-dev-16 \
        libpq-dev \
        cmake \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 浅克隆 + 重试；submodule 同样浅克隆。用 insteadOf 让主仓与 submodule 统一走镜像源。
RUN git config --global url."${GIT_MIRROR}/".insteadOf "https://github.com/" \
    && set -eux; \
    for i in 1 2 3; do \
        git clone --depth 1 https://github.com/jaiminpan/pg_jieba.git /tmp/pg_jieba && break; \
        echo "clone 失败，第 ${i} 次重试"; sleep 5; \
    done; \
    test -d /tmp/pg_jieba/.git; \
    cd /tmp/pg_jieba \
    && git submodule update --init --recursive --depth 1 \
    && mkdir build && cd build \
    && cmake .. \
        -DPostgreSQL_TYPE_INCLUDE_DIR="$(/usr/lib/postgresql/16/bin/pg_config --includedir-server)" \
        -DPostgreSQL_LIBRARY_DIR="$(/usr/lib/postgresql/16/bin/pg_config --libdir)" \
        -DPostgreSQL_INCLUDE_DIR="$(/usr/lib/postgresql/16/bin/pg_config --includedir)" \
    && make \
    && make install

# ---- 运行阶段：拷贝编译产物 ----
FROM pgvector/pgvector:pg16

# 扩展 .so、SQL/control、以及 tsearch_data 下的 jieba 分词词典
COPY --from=builder /usr/lib/postgresql/16/lib/pg_jieba.so /usr/lib/postgresql/16/lib/pg_jieba.so
COPY --from=builder /usr/share/postgresql/16/extension/ /usr/share/postgresql/16/extension/
COPY --from=builder /usr/share/postgresql/16/tsearch_data/ /usr/share/postgresql/16/tsearch_data/
