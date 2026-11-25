#!/usr/bin/env bash
# Regenerate the Linux golden PNGs inside the production image (same Chromium and fonts as CI; built native on Apple silicon — Chromium under amd64 emulation crashes).
set -euo pipefail
cd "$(dirname "$0")/.."
docker build --platform linux/arm64 -t samawi-bot-test -f - . <<'DOCKER'
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY . .
RUN uv sync --frozen
DOCKER
docker run --rm --platform linux/arm64 -e UPDATE_GOLDEN=1 -v "$PWD:/app" samawi-bot-test \
  python -m pytest -q tests/test_render_renderer.py -p no:cacheprovider
