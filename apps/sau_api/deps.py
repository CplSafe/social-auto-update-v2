import os
import secrets

from fastapi import Header, HTTPException, status

_TOKEN_ENV = "SAU_INTERNAL_TOKEN"
_MIN_TOKEN_LEN = 16


def _load_token() -> str:
    token = os.getenv(_TOKEN_ENV, "")
    if not token or len(token) < _MIN_TOKEN_LEN:
        raise RuntimeError(
            f"{_TOKEN_ENV} must be set and >= {_MIN_TOKEN_LEN} chars at process start"
        )
    return token


_EXPECTED_TOKEN = _load_token()


async def verify_sau_token(
    x_sau_token: str | None = Header(default=None, alias="X-Sau-Token"),
) -> None:
    if x_sau_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Sau-Token",
        )
    if not secrets.compare_digest(x_sau_token, _EXPECTED_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid X-Sau-Token",
        )
