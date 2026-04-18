"""Worker-level tests for publish_xhs and publish_ks.

These mirror ``test_publish_douyin_task.py`` to cover the P4 multi-
platform shape: each platform task is a thin wrapper around the shared
``run_publish`` runner, so we mostly verify that

1. the right uploader module gets resolved (xhs ≠ ks ≠ douyin),
2. the platform-extras hook fires (xhs squeezes location into desc, ks
   silently drops it), and
3. cookie-missing / title-missing fast-fails still hit.
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

from apps.sau_worker import _publish_rate_limit
from apps.sau_worker._publish_runner import apply_location_into_desc
from apps.sau_worker.tasks import publish_ks as ks_task_module
from apps.sau_worker.tasks import publish_xhs as xhs_task_module


@pytest.fixture
def cookie_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SAU_COOKIE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def temp_video(tmp_path: Path) -> Path:
    f = tmp_path / "vid.mp4"
    f.write_bytes(b"fake video")
    return f


@pytest.fixture(autouse=True)
def bypass_rate_limit_and_concurrency(monkeypatch: pytest.MonkeyPatch):
    """Drop in always-allow buckets + an always-acquire concurrency gate
    on both task modules so we can exercise the publish flow without
    standing up redis."""
    allow_bucket = SimpleNamespace(wait_or_acquire=lambda *_a, **_k: True)
    allow_hooks = SimpleNamespace(
        per_account=lambda: allow_bucket,
        platform=lambda: allow_bucket,
    )

    class _AlwaysAcquireGate:
        def slot(self, *_a, **_k):
            @contextmanager
            def cm():
                yield True

            return cm()

    gate = _AlwaysAcquireGate()
    for mod in (xhs_task_module, ks_task_module):
        monkeypatch.setattr(mod, "hooks_for", lambda _platform: allow_hooks)
        monkeypatch.setattr(mod, "tenant_gate", lambda: gate)
    _publish_rate_limit.reset_for_tests()
    yield


# ---------- platform-specific fake uploader installers ----------


def _install_fake_xhs_uploader(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    cookie_auth = AsyncMock(return_value=True)

    class FakeXHS:
        def __init__(self, **kwargs: Any) -> None:
            captured["init_kwargs"] = kwargs

        async def main(self) -> None:
            captured["main_called"] = True

    fake_mod = types.SimpleNamespace(cookie_auth=cookie_auth, XiaoHongShuVideo=FakeXHS)
    monkeypatch.setitem(sys.modules, "uploader", types.SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "uploader.xiaohongshu_uploader",
        types.SimpleNamespace(main=fake_mod),
    )
    monkeypatch.setitem(sys.modules, "uploader.xiaohongshu_uploader.main", fake_mod)
    return captured


def _install_fake_ks_uploader(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    cookie_auth = AsyncMock(return_value=True)

    class FakeKS:
        def __init__(self, **kwargs: Any) -> None:
            captured["init_kwargs"] = kwargs

        async def main(self) -> None:
            captured["main_called"] = True

    fake_mod = types.SimpleNamespace(cookie_auth=cookie_auth, KSVideo=FakeKS)
    monkeypatch.setitem(sys.modules, "uploader", types.SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "uploader.ks_uploader",
        types.SimpleNamespace(main=fake_mod),
    )
    monkeypatch.setitem(sys.modules, "uploader.ks_uploader.main", fake_mod)
    return captured


# ---------- xhs ----------


class TestXhsPublish:
    def test_cookie_missing_fast_fail(self, cookie_root, temp_video):
        result = xhs_task_module.publish_xhs.run(
            tenant_id="t",
            sau_account_id="acc",
            video_path=str(temp_video),
            payload={"title": "hi"},
        )
        assert result["status"] == "cookie_invalid"
        assert not temp_video.exists()

    def test_success_path_and_location_squeezed_into_desc(
        self, cookie_root, temp_video, monkeypatch
    ):
        cookie = cookie_root / "tenant_t" / "xhs" / "acc.json"
        cookie.parent.mkdir(parents=True)
        cookie.write_text("{}")
        captured = _install_fake_xhs_uploader(monkeypatch)

        result = xhs_task_module.publish_xhs.run(
            tenant_id="t",
            sau_account_id="acc",
            video_path=str(temp_video),
            payload={
                "title": "hi",
                "tags": ["a"],
                "desc": "base desc",
                "platform_payload": {"location": "Shanghai"},
            },
        )
        assert result["status"] == "success"
        # P4 simplification: location lands as a desc footer.
        assert "📍 Shanghai" in captured["init_kwargs"]["desc"]
        assert captured["init_kwargs"]["desc"].startswith("base desc")
        # XHS uploader doesn't accept a `location` kwarg — runner shouldn't
        # try to pass one through extra_kwargs.
        assert "location" not in captured["init_kwargs"]


# ---------- ks ----------


class TestKsPublish:
    def test_cookie_missing_fast_fail(self, cookie_root, temp_video):
        result = ks_task_module.publish_ks.run(
            tenant_id="t",
            sau_account_id="acc",
            video_path=str(temp_video),
            payload={"title": "hi"},
        )
        assert result["status"] == "cookie_invalid"
        assert not temp_video.exists()

    def test_success_path_drops_platform_payload(
        self, cookie_root, temp_video, monkeypatch
    ):
        cookie = cookie_root / "tenant_t" / "ks" / "acc.json"
        cookie.parent.mkdir(parents=True)
        cookie.write_text("{}")
        captured = _install_fake_ks_uploader(monkeypatch)

        result = ks_task_module.publish_ks.run(
            tenant_id="t",
            sau_account_id="acc",
            video_path=str(temp_video),
            payload={
                "title": "hi",
                "desc": "base",
                # KS uploader has no location support — runner should
                # silently drop this rather than crash.
                "platform_payload": {"location": "Shanghai"},
            },
        )
        assert result["status"] == "success"
        # Desc unchanged — KS extras hook is the no-op variant.
        assert captured["init_kwargs"]["desc"] == "base"
        assert "location" not in captured["init_kwargs"]


# ---------- apply_location_into_desc edge cases ----------


class TestApplyLocationIntoDesc:
    def test_string_location_appended_with_emoji_footer(self):
        desc, extras = apply_location_into_desc("base", {"location": "Shanghai"})
        assert desc == "base\n📍 Shanghai"
        assert extras == {}

    def test_blank_string_location_dropped(self):
        desc, extras = apply_location_into_desc("base", {"location": "   "})
        assert desc == "base"
        assert extras == {}

    def test_missing_platform_payload_handled(self):
        desc, extras = apply_location_into_desc("base", None)  # type: ignore[arg-type]
        assert desc == "base"
        assert extras == {}

    def test_non_string_location_coerced_not_crashed(self):
        # Codex P2 fix: a non-string ``location`` must coerce rather than
        # AttributeError out before the task's try/finally cleans up the
        # tmp video.
        desc, extras = apply_location_into_desc("base", {"location": 12345})
        assert desc == "base\n📍 12345"
        assert extras == {}

    def test_none_location_value_treated_as_blank(self):
        desc, extras = apply_location_into_desc("base", {"location": None})
        assert desc == "base"
        assert extras == {}
