"""sau_worker package — ensure project root is importable for legacy `conf` module."""
import sys
from pathlib import Path

_project_root = str(Path(__file__).parent.parent.parent.resolve())
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
