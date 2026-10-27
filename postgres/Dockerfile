# PostgreSQL 17 (Alpine, so existing databases keep their musl collation) with pgvector.
FROM postgres:17-alpine

ARG PGVECTOR_VERSION=0.8.1

# Built without -march=native so the image runs on any x86-64 CPU, and without LLVM
# bitcode, which only serves JIT and would need the exact LLVM PostgreSQL was built with.
RUN apk add --no-cache --virtual .pgvector-build build-base git \
    && git clone --depth 1 --branch "v${PGVECTOR_VERSION}" https://github.com/pgvector/pgvector.git /tmp/pgvector \
    && make -C /tmp/pgvector OPTFLAGS="" with_llvm=no \
    && make -C /tmp/pgvector install with_llvm=no \
    && rm -rf /tmp/pgvector \
    && apk del .pgvector-build
