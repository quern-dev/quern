"""Quern — server package."""

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_version() -> str:
    """Return the project version from pyproject.toml. Single source of truth."""
    path = Path(__file__).resolve().parent
    for _ in range(5):
        candidate = path / "pyproject.toml"
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                if line.startswith("version"):
                    return line.split('"')[1]
            break
        parent = path.parent
        if parent == path:
            break
        path = parent
    return "unknown"
