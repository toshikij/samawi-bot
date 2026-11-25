# One image, two entrypoints (mirrors Seduh):
#   pipeline (default): samawi-bot run <job>   — EventBridge Scheduler → ECS RunTask
#   dashboard:          samawi-bot admin        — FastAPI on :3200 under /samawi, reached by SSM tunnel only
# The Playwright base image carries Chromium and its system libraries for rendering.
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright SNAPSHOT_DIR=/data/snapshot
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
WORKDIR /app
# Amazon RDS CA bundle, so DATABASE_URL can use sslmode=verify-full.
ADD --chmod=644 https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /etc/ssl/certs/rds-global-bundle.pem
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
# The bundled snapshot of the app's assets (refreshed at runtime when SAMAWI_REPO_TOKEN is set).
COPY snapshot /data/snapshot
RUN useradd -m samawi && mkdir -p /data/hadith && chown -R samawi /data /app
USER samawi
EXPOSE 3200
ENTRYPOINT ["samawi-bot"]
CMD ["run", "healthcheck"]
