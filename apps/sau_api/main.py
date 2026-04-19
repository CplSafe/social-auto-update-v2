import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from apps.sau_api.deps import verify_sau_token
from apps.sau_api.login_sessions import registry
from apps.sau_api.routers import accounts, challenge, health, login_sse, publish, tasks

# Make our own loggers (apps.*, uploader.*) visible at the uvicorn console.
# Without this, uvicorn's default logging config swallows everything that
# isn't `uvicorn.access`, so our SMS challenge diagnostics ("step=chooser",
# "consumed user action", etc.) never show up — which makes flow debugging
# impossible. Setting it on the root logger is the simplest reach-all.
# Honour SAU_LOG_LEVEL so prod can dial it back to WARNING.
_log_level = os.getenv("SAU_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

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
    (challenge.router, "challenge"),
)
for router, tag in _protected:
    app.include_router(
        router,
        tags=[tag],
        dependencies=[Depends(verify_sau_token)],
    )
