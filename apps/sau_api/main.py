import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from apps.sau_api.deps import verify_sau_token
from apps.sau_api.login_sessions import registry
from apps.sau_api.routers import accounts, health, login_sse, publish, tasks

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup: preflight the upstream uploader when real login is enabled
    so the process refuses to start under a misconfiguration instead of
    discovering it on first request via a 30s timeout.

    Shutdown: cancel any in-flight scan-to-auth Playwright runs so the
    browser doesn't outlive the API process.
    """
    if os.getenv("SAU_ENABLE_REAL_LOGIN", "").lower() in ("1", "true", "yes"):
        try:
            import uploader.douyin_uploader.main  # noqa: F401
        except Exception:
            logger.exception(
                "SAU_ENABLE_REAL_LOGIN is on but the upstream uploader cannot be "
                "imported — refusing to start."
            )
            raise

    try:
        yield
    finally:
        cancelled = await registry.cancel_all()
        if cancelled:
            logger.info("cancelled %d in-flight login sessions on shutdown", cancelled)


app = FastAPI(title="sau-api", version="0.1.0", lifespan=lifespan)
app.include_router(health.router, tags=["health"])

_protected = (
    (accounts.router, "accounts"),
    (login_sse.router, "login"),
    (publish.router, "publish"),
    (tasks.router, "tasks"),
)
for router, tag in _protected:
    app.include_router(
        router,
        tags=[tag],
        dependencies=[Depends(verify_sau_token)],
    )
