"""Worker-level tests for publish_xhs.

Covers cookie-missing fast-fail, the success path, and the P5 location
integration: ``platform_payload.location`` is forwarded as a constructor
kwarg to the upstream Video class (and ultimately to ``set_location()``
inside the patched ``upload()``), not stuffed into the desc footer.
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
from apps.sau_worker._publish_runner import apply_location_extras
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
    so we exercise the publish flow without standing up redis."""
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
    monkeypatch.setattr(xhs_task_module, "hooks_for", lambda _platform: allow_hooks)
    monkeypatch.setattr(xhs_task_module, "tenant_gate", lambda: gate)
    _publish_rate_limit.reset_for_tests()
    yield


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


# ---------- xhs publish flow ----------


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

    def test_success_path_forwards_location_as_kwarg(
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
        # P5: location reaches the constructor as a real kwarg, not
        # smuggled in as a desc footer.
        assert captured["init_kwargs"]["location"] == "Shanghai"
        # Desc stays untouched (no 📍 emoji hack).
        assert captured["init_kwargs"]["desc"] == "base desc"

    def test_success_path_without_location(
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
            payload={"title": "hi", "desc": "base"},
        )
        assert result["status"] == "success"
        # No platform_payload → no location kwarg, no desc mutation.
        assert "location" not in captured["init_kwargs"]
        assert captured["init_kwargs"]["desc"] == "base"


# ---------- apply_location_extras edge cases ----------


class TestApplyLocationExtras:
    def test_string_location_forwarded_as_kwarg(self):
        desc, extras = apply_location_extras("base", {"location": "Shanghai"})
        # P5: desc untouched, location surfaces in extras.
        assert desc == "base"
        assert extras == {"location": "Shanghai"}

    def test_blank_string_location_dropped(self):
        desc, extras = apply_location_extras("base", {"location": "   "})
        assert desc == "base"
        assert extras == {}

    def test_missing_platform_payload_handled(self):
        desc, extras = apply_location_extras("base", None)  # type: ignore[arg-type]
        assert desc == "base"
        assert extras == {}

    def test_non_string_location_coerced(self):
        # The runner is the trust boundary on the sau side — non-string
        # location must coerce rather than crash before try/finally.
        desc, extras = apply_location_extras("base", {"location": 12345})
        assert desc == "base"
        assert extras == {"location": "12345"}

    def test_none_location_value_treated_as_blank(self):
        desc, extras = apply_location_extras("base", {"location": None})
        assert desc == "base"
        assert extras == {}
