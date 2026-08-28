# syntax=docker/dockerfile:1
# ─── builder: resolve deps with uv ───
FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt \
    && uv pip install --system --no-cache .

COPY src ./src
COPY main.py ./

# ─── runtime: minimal image ───
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow \
    IN_DOCKER=true

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /build/src ./src
COPY --from=builder /build/main.py ./
COPY pyproject.toml ./

RUN mkdir -p data logs

CMD ["python", "main.py"]
