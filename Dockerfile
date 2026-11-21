# ==============================================================================
# Heimdall Trading Platform - 后端服务生产级 Dockerfile (FastAPI + Multi-Agent)
# ==============================================================================
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY heimdall ./heimdall
COPY config ./config
COPY migrations ./migrations

RUN mkdir -p /app/data

EXPOSE 8000

# 保持 --workers 1 以确保 BotManager、后台调度器与 WebSocket 连接池状态一致
CMD ["uvicorn", "heimdall.api.server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
