from pathlib import Path

try:
    from conf import BASE_DIR
except ModuleNotFoundError:
    BASE_DIR = Path(__file__).parent.parent.resolve()

Path(BASE_DIR / "cookies").mkdir(exist_ok=True)