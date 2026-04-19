from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
XHS_SERVER = "http://127.0.0.1:11901"  # only used by xhs-related flows
LOCAL_CHROME_PATH = ""  # optional, e.g. C:/Program Files/Google/Chrome/Application/chrome.exe
import os as _os
# 通过 SAU_PUBLISH_HEADLESS 环境变量控制发布时的浏览器是否无头。
# 默认 True（生产）；本地调试要看验证弹窗时设 SAU_PUBLISH_HEADLESS=0 即可。
LOCAL_CHROME_HEADLESS = _os.getenv("SAU_PUBLISH_HEADLESS", "1").lower() not in ("0", "false", "no")
DEBUG_MODE = True  # default debug behavior
