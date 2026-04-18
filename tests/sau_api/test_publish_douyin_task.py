"""Worker-level tests for publish_douyin.

Since P4 the publish task is a thin wrapper around
``apps.sau_worker._publish_runner.run_publish`` — these tests therefore
monkey-patch helpers on the *runner* module (not the task module) so the
suite runs without booting Playwright. Tests cover the cookie-missing
fast-fail, the title-required guard, the success path, the upstream-
error classification, and the rate-limit gates.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from apps.sau_worker import _publish_rate_limit, _publish_runner
from apps.sau_worker.tasks import publish_douyin as task_module


@pytest.fixture
def cookie_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SAU_COOKIE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def bypass_rate_limit_and_concurrency(monkeypatch: pytest.MonkeyPatch):
    """Stub the P3 limiter + gate so unit tests exercise the publish flow
    without booting a real Redis. Tests that explicitly want to assert on
    rate-limit / gate behaviour can monkey-patch ``hooks_for`` /
    ``tenant_gate`` inline."""
    allow_bucket = SimpleNamespace(
        wait_or_acquire=lambda *_a, **_k: True,
        try_acquire=lambda *_a, **_k: SimpleNamespace(allowed=True, retry_after_seconds=0),
    )

    class _AlwaysAcquireGate:
        def slot(self, *_a, **_k):
            @contextmanager
            def cm():
                yield True

            return cm()

        def try_acquire(self, *_a, **_k):
            return True

        def release(self, *_a, **_k):
            return None

        def wait_or_acquire(self, *_a, **_k):
            return True

    allow_hooks = SimpleNamespace(
        per_account=lambda: allow_bucket,
        platform=lambda: allow_bucket,
    )
    monkeypatch.setattr(
        task_module, "hooks_for", lambda _platform: allow_hooks
    )
    monkeypatch.setattr(
        task_module, "tenant_gate", lambda: _AlwaysAcquireGate()
    )
    # Drop any singletons that earlier tests in this process may have
    # cached so each test starts with a clean slate.
    _publish_rate_limit.reset_for_tests()
    yield


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
    import inside the runner resolves to it. Returns a dict that lets the
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
    def test_rejects_dotdot_in_tenant_id(self):
        with pytest.raises(ValueError):
            _publish_runner.resolve_cookie_path("../../etc", "douyin", "acc")


class TestVideoUrlPath:
    def test_downloads_url_when_video_path_missing(
        self, cookie_root, tmp_path, monkeypatch
    ):
        cookie = cookie_root / "tenant_t" / "douyin" / "acc.json"
        cookie.parent.mkdir(parents=True)
        cookie.write_text("{}")
        captured = _install_fake_uploader(monkeypatch)
        # Redirect tmp dir so we can assert + clean up easily.
        monkeypatch.setenv("SAU_TMP_DIR", str(tmp_path / "sau-tmp"))

        downloads: list[str] = []

        def fake_download(url, *, dest):
            downloads.append(url)
            dest.write_bytes(b"streamed bytes")
            return dest

        monkeypatch.setattr(_publish_runner, "download_video_url", fake_download)

        result = task_module.publish_douyin.run(
            tenant_id="t",
            sau_account_id="acc",
            payload={"title": "hi"},
            video_url="https://signed/url",
        )
        assert result["success"] is True
        # Worker materialised the URL once.
        assert downloads == ["https://signed/url"]
        # And handed the resulting on-disk path to DouYinVideo.
        assert captured["init_kwargs"]["file_path"].endswith(".mp4")
        # And cleaned the file up after.
        assert not Path(captured["init_kwargs"]["file_path"]).exists()

    def test_returns_failed_when_download_raises(
        self, cookie_root, tmp_path, monkeypatch
    ):
        cookie = cookie_root / "tenant_t" / "douyin" / "acc.json"
        cookie.parent.mkdir(parents=True)
        cookie.write_text("{}")
        _install_fake_uploader(monkeypatch)
        monkeypatch.setenv("SAU_TMP_DIR", str(tmp_path / "sau-tmp"))

        def boom(url, *, dest):
            raise RuntimeError("403 forbidden")

        monkeypatch.setattr(_publish_runner, "download_video_url", boom)

        result = task_module.publish_douyin.run(
            tenant_id="t",
            sau_account_id="acc",
            payload={"title": "hi"},
            video_url="https://signed/url",
        )
        assert result["success"] is False
        assert result["status"] == "upload_failed"
        assert "403" in result["message"]


class TestRateLimiterGating:
    def test_per_account_rate_limit_returns_rate_limited(
        self, cookie_root, temp_video, monkeypatch
    ):
        cookie = cookie_root / "tenant_t" / "douyin" / "acc.json"
        cookie.parent.mkdir(parents=True)
        cookie.write_text("{}")

        # Force the per-account bucket to refuse, but keep the platform
        # bucket allowing — the publish runner short-circuits on the first
        # refusal so this is enough.
        deny_bucket = SimpleNamespace(wait_or_acquire=lambda *_a, **_k: False)
        allow_bucket = SimpleNamespace(wait_or_acquire=lambda *_a, **_k: True)
        deny_hooks = SimpleNamespace(
            per_account=lambda: deny_bucket,
            platform=lambda: allow_bucket,
        )
        monkeypatch.setattr(
            task_module, "hooks_for", lambda _platform: deny_hooks
        )

        result = task_module.publish_douyin.run(
            tenant_id="t",
            sau_account_id="acc",
            payload={"title": "hi"},
            video_path=str(temp_video),
        )
        assert result["success"] is False
        assert result["status"] == "rate_limited"
        # Cleaned up the temp file even though we never started the upload.
        assert not temp_video.exists()
