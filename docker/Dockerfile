FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
        libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libxkbcommon0 libasound2 libcups2 libpango-1.0-0 \
        libcairo2 fonts-noto-cjk curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 sau

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY --chown=sau:sau pyproject.toml uv.lock README.md ./
COPY --chown=sau:sau packages/sau_contracts ./packages/sau_contracts

# Upstream `uv.lock` does not yet include our P0 deps (fastapi/uvicorn/celery
# /redis/gevent/sau-contracts), so we cannot use `--frozen`. The build will
# resolve and lock during install. After the first successful build, run
# `uv lock` locally and commit `uv.lock` so subsequent images can use
# `--frozen` for reproducibility.
RUN uv sync --no-install-project --extra-index-url https://pypi.org/simple

RUN uv run patchright install chromium --with-deps

COPY --chown=sau:sau . .

RUN chmod +x docker/entrypoint-api.sh docker/entrypoint-worker.sh \
        scripts/ensure_cookie_dir.sh

USER sau

EXPOSE 8001

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["docker/entrypoint-api.sh"]
