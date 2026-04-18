"""Bridge between sau-api routers and upstream `uploader/*` packages.

P0 keeps this empty on purpose. P1+ will add:

- `qrcode_login(platform, account_file, qrcode_callback)` - wraps
  `uploader.douyin_uploader.main.douyin_cookie_gen` and the corresponding
  xhs/ks variants.
- `cookie_check(platform, account_file)` - wraps `cookie_auth(account_file)`.
- `publish_video(platform, account_file, payload)` - wraps `DouYinVideo`,
  `XhsVideo`, `KsVideo`.

When implementing, ALWAYS use the atomic write helper below to persist
cookies (mode 0o600 inside dir mode 0o700) so that file-system level
isolation between tenants holds.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_cookie_atomic(path: Path, data: dict[str, Any]) -> None:
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
