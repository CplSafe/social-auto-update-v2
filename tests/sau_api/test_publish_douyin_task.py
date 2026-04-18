"""Worker-level tests for publish_douyin.

We monkey-patch the lazy import inside ``_run_douyin_publish`` so the suite
runs without booting Playwright. Tests cover the cookie-missing fast-fail,
the title-required guard, the success path and the upstream-error
classification.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from apps.sau_worker.tasks import publish_douyin as task_module


@pytest.fixture
def cookie_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SAU_COOKIE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def temp_video(tmp_path: Path) -> Path:
    f = tmp_path / "vid.mp4"
    f.write_bytes(b"fake video")
    return f


def _install_fake_uploader(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cookie_auth_returns: bool = True,
    upload_raises: BaseException | None = None,
) -> dict[str, Any]:
    """Install a fake `uploader.douyin_uploader.main` module so the lazy
    import inside the task resolves to it. Returns a dict that lets the
    test peek at what was constructed."""
    captured: dict[str, Any] = {}

    cookie_auth = AsyncMock(return_value=cookie_auth_returns)

    class FakeDouYinVideo:
        def __init__(self, **kwargs: Any) -> None:
            captured["init_kwargs"] = kwargs

        async def main(self) -> None:
            captured["main_called"] = True
            if upload_raises is not None:
                raise upload_raises

    fake_mod = types.SimpleNamespace(
        cookie_auth=cookie_auth,
        DouYinVideo=FakeDouYinVideo,
    )
    monkeypatch.setitem(sys.modules, "uploader", types.SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "uploader.douyin_uploader",
        types.SimpleNamespace(main=fake_mod),
    )
    monkeypatch.setitem(sys.modules, "uploader.douyin_uploader.main", fake_mod)
    return captured


# ---------- pre-flight guards (don't need the fake uploader) ----------


class TestCookieMissingFastFail:
    def test_returns_cookie_invalid_when_file_absent(
        self, cookie_root, temp_video, monkeypatch
    ):
        # No cookie file written → fast fail without spinning up a browser.
        # ``.run(...)`` is the bound Celery task method — `self` is already
        # the task instance, so we just pass kwargs.
        result = task_module.publish_douyin.run(
            tenant_id="t",
            sau_account_id="acc",
            video_path=str(temp_video),
            payload={"title": "hi"},
        )
        assert result["success"] is False
        assert result["status"] == "cookie_invalid"
        # Temp file got cleaned up.
        assert not temp_video.exists()


class TestTitleRequired:
    def test_returns_upload_failed_when_title_missing(
        self, cookie_root, temp_video, monkeypatch
    ):
        cookie = cookie_root / "tenant_t" / "douyin" / "acc.json"
        cookie.parent.mkdir(parents=True)
        cookie.write_text("{}")
        result = task_module.publish_douyin.run(
            tenant_id="t",
            sau_account_id="acc",
            video_path=str(temp_video),
            payload={"title": ""},
        )
        assert result["success"] is False
        assert result["status"] == "upload_failed"
        assert "title" in result["message"]
        assert not temp_video.exists()


# ---------- happy path + upstream classification ----------


class TestRunDouyinPublishWiring:
    def test_successful_upload_returns_success(
        self, cookie_root, temp_video, monkeypatch
    ):
        cookie = cookie_root / "tenant_t" / "douyin" / "acc.json"
        cookie.parent.mkdir(parents=True)
        cookie.write_text("{}")
        captured = _install_fake_uploader(monkeypatch)

        result = task_module.publish_douyin.run(
    
            tenant_id="t",
            sau_account_id="acc",
            video_path=str(temp_video),
            payload={"title": "hi", "tags": ["a", "b"], "desc": "d"},
        )
        assert result["success"] is True
        assert result["status"] == "success"
        # Constructor saw all the fields.
        assert captured["init_kwargs"]["title"] == "hi"
        assert captured["init_kwargs"]["tags"] == ["a", "b"]
        assert captured["init_kwargs"]["desc"] == "d"
        # Tmp file always unlinked.
        assert not temp_video.exists()

    def test_cookie_auth_false_short_circuits_to_cookie_invalid(
        self, cookie_root, temp_video, monkeypatch
    ):
        cookie = cookie_root / "tenant_t" / "douyin" / "acc.json"
        cookie.parent.mkdir(parents=True)
        cookie.write_text("{}")
        _install_fake_uploader(monkeypatch, cookie_auth_returns=False)

        result = task_module.publish_douyin.run(
    
            tenant_id="t",
            sau_account_id="acc",
            video_path=str(temp_video),
            payload={"title": "hi"},
        )
        assert result["success"] is False
        assert result["status"] == "cookie_invalid"

    def test_upload_exception_classified_as_upload_failed(
        self, cookie_root, temp_video, monkeypatch
    ):
        cookie = cookie_root / "tenant_t" / "douyin" / "acc.json"
        cookie.parent.mkdir(parents=True)
        cookie.write_text("{}")
        _install_fake_uploader(monkeypatch, upload_raises=RuntimeError("xpath gone"))

        result = task_module.publish_douyin.run(
    
            tenant_id="t",
            sau_account_id="acc",
            video_path=str(temp_video),
            payload={"title": "hi"},
        )
        assert result["success"] is False
        assert result["status"] == "upload_failed"
        assert "xpath gone" in result["message"]


class TestPathTraversalGuard:
    def test_rejects_dotdot_in_tenant_id(self, cookie_root, temp_video, monkeypatch):
        with pytest.raises(ValueError):
            task_module._resolve_cookie_path("../../etc", "acc")
