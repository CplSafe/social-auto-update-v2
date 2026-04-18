# Fork maintenance — Dify integration

> Upstream: <https://github.com/dreammis/social-auto-upload>
> This fork keeps a `sau-api` (FastAPI) + `sau-worker` (Celery) shell for
> Dify's publish-center to call. Everything lives in additive paths so the
> upstream `git pull` flow stays clean.

## Layout (fork-only additions)

| Path | Purpose |
|---|---|
| `apps/sau_api/` | FastAPI process (`uvicorn apps.sau_api.main:app`) |
| `apps/sau_worker/` | Celery process (`celery -A apps.sau_worker.celery_app`) |
| `packages/sau_contracts/` | Shared task/queue name constants — single source of truth |
| `docker/` | New `Dockerfile` + `docker-compose.yml` (does NOT replace upstream `Dockerfile`) |
| `scripts/` | `ensure_cookie_dir.sh`, `smoke_p0.sh` |
| `sau_data/cookies/` | Runtime cookie volume (host mounts here) |

## Files we modify in upstream

Only **`pyproject.toml`** — appended `fastapi`, `uvicorn[standard]`,
`celery`, `redis`, `gevent`, plus an editable `[tool.uv.sources]` entry
for `sau-contracts`. Keep both sides during merge.

Everything else is additive and never conflicts.

## Pulling upstream

```bash
git fetch upstream
git checkout -b sync-upstream-$(date +%Y%m%d) upstream/main
git checkout main
git merge sync-upstream-$(date +%Y%m%d)
uv sync
uv run patchright install chromium
docker compose -f docker/docker-compose.yml build --no-cache
./scripts/smoke_p0.sh
```

## Running locally

```bash
cp docker/.env.example docker/.env
# Edit SAU_INTERNAL_TOKEN and confirm SAU_NETWORK_NAME against `docker network ls`.
docker compose -f docker/docker-compose.yml up -d
./scripts/smoke_p0.sh
```
