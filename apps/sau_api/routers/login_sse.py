from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

router = APIRouter()


class LoginRequest(BaseModel):
    tenant_id: str
    platform: Literal["douyin", "xhs", "ks"]
    sau_account_id: str


@router.post("/login")
async def start_login(req: LoginRequest) -> dict[str, str]:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="login (SSE) is implemented in P1",
    )
