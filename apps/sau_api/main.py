from fastapi import Depends, FastAPI

from apps.sau_api.deps import verify_sau_token
from apps.sau_api.routers import accounts, health, login_sse, publish, tasks

app = FastAPI(title="sau-api", version="0.1.0")

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
