from pathlib import Path

try:
    from conf import BASE_DIR
except ModuleNotFoundError:
    from pathlib import Path
    BASE_DIR = Path(__file__).parent.parent.parent.resolve()

Path(BASE_DIR / "cookies" / "tk_uploader").mkdir(exist_ok=True)