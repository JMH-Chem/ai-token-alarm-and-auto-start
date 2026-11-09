import json
import time
from pathlib import Path


def read_usage(path):
    """Read the fake usage file, retrying while fake_claude replaces it (Windows file locking)."""
    for _ in range(100):
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError):
            time.sleep(0.02)
    raise RuntimeError("cannot read usage file")
