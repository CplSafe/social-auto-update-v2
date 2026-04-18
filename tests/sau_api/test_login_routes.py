"""Wire-level tests for the /login + /login/status routes.

These tests run in stub mode (``SAU_ENABLE_REAL_LOGIN`` unset), so they
don't pull Playwright. The real-login path is exercised manually with the
upstream Douyin UI.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import secrets
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[object]:
    """Build the FastAPI app under stub-mode config.

    Each module-level run uses a fresh cookie root so the path-creating
    side effect from ``/login`` doesn't pollute subsequent runs.
    """
    cookie_root = tmp_path_factory.mktemp("cookies")
    os.environ["SAU_INTERNAL_TOKEN"] = secrets.token_hex(32)
    os.environ["SAU_BROKER_URL"] = "memory://"
    os.environ["SAU_RESULT_BACKEND"] = "cache+memory://"
    os.environ["SAU_COOKIE_ROOT"] = str(cookie_root)
    os.environ.pop("SAU_ENABLE_REAL_LOGIN", None)

    # Force a fresh import of the app modules under the new env vars.
    for name in [m for m in sys.modules if m.startswith("apps.sau_api")]:
        sys.modules.pop(name, None)
    main = importlib.import_module("apps.sau_api.main")
    yield main.app
    for name in [m for m in sys.modules if m.startswith("apps.sau_api")]:
        sys.modules.pop(name, None)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_header() -> dict[str, str]:
    return {"X-Sau-Token": os.environ["SAU_INTERNAL_TOKEN"]}


class TestLoginStubMode:
    def test_returns_qr_and_creates_session(self, client, auth_header):
        sid = str(uuid.uuid4())
        resp = client.post(
            "/login",
            headers=auth_header,
            json={"tenant_id": "t1", "platform": "douyin", "session_id": sid},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["qr_image_base64"].startswith("data:image/png;base64,")
        assert body["expires_in"] == 180

        # Session is now in the registry as waiting.
        status = client.get(f"/login/status/{sid}", headers=auth_header)
        assert status.status_code == 200
        assert status.json()["status"] == "waiting"

    def test_creates_tenant_scoped_cookie_dir(self, client, auth_header):
        sid = str(uuid.uuid4())
        client.post(
            "/login",
            headers=auth_header,
            json={"tenant_id": "tenant-xyz", "platform": "douyin", "session_id": sid},
        )
        cookie_root = Path(os.environ["SAU_COOKIE_ROOT"])
        # Parent dir for a freshly minted account_file must exist with the
        # right tenant scoping (the file itself isn't written until the real
        # Playwright path completes).
        assert (cookie_root / "tenant_tenant-xyz" / "douyin").exists()

    def test_rejects_unsupported_platform(self, client, auth_header):
        # P5: ks is no longer a member of the Platform Literal, so the
        # pydantic validator rejects the request at the wire layer with
        # 422 (a stronger guarantee than P4's 400 inside the handler).
        resp = client.post(
            "/login",
            headers=auth_header,
            json={"tenant_id": "t1", "platform": "ks", "session_id": "sid-bad"},
        )
        assert resp.status_code == 422

    def test_xhs_login_supported_in_stub_mode(self, client, auth_header):
        # P4: xhs scan-to-auth must reach the QR-stub branch, not the
        # 400 platform-unsupported branch.
        sid = str(uuid.uuid4())
        resp = client.post(
            "/login",
            headers=auth_header,
            json={"tenant_id": "t-xhs", "platform": "xhs", "session_id": sid},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["qr_image_base64"].startswith("data:image/png;base64,")
        cookie_root = Path(os.environ["SAU_COOKIE_ROOT"])
        assert (cookie_root / "tenant_t-xhs" / "xhs").exists()

    def test_rejects_path_traversal_in_tenant_id(self, client, auth_header):
        # cookie_paths.resolve_cookie_path raises HTTPException 400.
        resp = client.post(
            "/login",
            headers=auth_header,
            json={
                "tenant_id": "../../etc",
                "platform": "douyin",
                "session_id": "sid-evil",
            },
        )
        assert resp.status_code == 400

    def test_status_for_unknown_session_returns_404(self, client, auth_header):
        resp = client.get(
            "/login/status/this-session-was-never-created",
            headers=auth_header,
        )
        assert resp.status_code == 404

    def test_login_requires_token(self, client):
        resp = client.post(
            "/login",
            json={"tenant_id": "t1", "platform": "douyin", "session_id": "sid-noauth"},
        )
        assert resp.status_code == 401

    def test_status_requires_token(self, client):
        resp = client.get("/login/status/anything")
        assert resp.status_code == 401


class TestRealLoginQrTimeout:
    """Drive the real-login branch with a fake `douyin_cookie_gen` that
    never invokes the qrcode_callback, to verify the 30s QR-wait timeout
    cancels the task and marks the session failed instead of leaving a
    ghost session that can't be reconciled."""

    @pytest.fixture
    def real_login_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> Iterator[tuple[TestClient, dict[str, str]]]:
        # Re-import the app under SAU_ENABLE_REAL_LOGIN=1 so the route
        # takes the runner path.
        token = secrets.token_hex(32)
        monkeypatch.setenv("SAU_INTERNAL_TOKEN", token)
        monkeypatch.setenv("SAU_BROKER_URL", "memory://")
        monkeypatch.setenv("SAU_RESULT_BACKEND", "cache+memory://")
        monkeypatch.setenv("SAU_COOKIE_ROOT", str(tmp_path / "cookies"))
        monkeypatch.setenv("SAU_ENABLE_REAL_LOGIN", "1")

        # Inject a fake module so the lazy `from uploader.douyin_uploader.main
        # import douyin_cookie_gen` inside runner() resolves to our stub.
        import types

        async def fake_douyin_cookie_gen(*args, **kwargs):
            # Intentionally never call the qrcode_callback so /login times
            # out on the 30s wait — the cap is shrunk via monkeypatch below.
            await asyncio.sleep(60)
            return {"success": False, "status": "timeout", "message": "test"}

        fake_mod = types.SimpleNamespace(douyin_cookie_gen=fake_douyin_cookie_gen)
        monkeypatch.setitem(
            sys.modules,
            "uploader.douyin_uploader.main",
            fake_mod,  # type: ignore[arg-type]
        )

        for name in [m for m in sys.modules if m.startswith("apps.sau_api")]:
            sys.modules.pop(name, None)
        main = importlib.import_module("apps.sau_api.main")

        # Shrink the 30s QR wait so the test actually completes in seconds.
        login_sse = importlib.import_module("apps.sau_api.routers.login_sse")
        original_wait_for = login_sse.asyncio.wait_for

        async def fast_wait_for(awaitable, timeout=None):
            return await original_wait_for(awaitable, timeout=2.0)

        monkeypatch.setattr(login_sse.asyncio, "wait_for", fast_wait_for)

        yield TestClient(main.app), {"X-Sau-Token": token}

        for name in [m for m in sys.modules if m.startswith("apps.sau_api")]:
            sys.modules.pop(name, None)

    def test_qr_timeout_cancels_task_and_marks_session_failed(self, real_login_client):
        client, header = real_login_client
        sid = str(uuid.uuid4())
        resp = client.post(
            "/login",
            headers=header,
            json={"tenant_id": "t-real", "platform": "douyin", "session_id": sid},
        )
        assert resp.status_code == 504

        # Subsequent /login/status MUST return failed immediately, not waiting
        # for the 200s session TTL — that's the codex Q2 fix.
        status = client.get(f"/login/status/{sid}", headers=header)
        assert status.status_code == 200
        body = status.json()
        assert body["status"] == "failed"
        assert (body.get("message") or "").startswith("qr-generation timeout")
