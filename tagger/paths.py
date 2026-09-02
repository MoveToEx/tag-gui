from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIRECTORY = PROJECT_ROOT / "data"
TAG_LIBRARY_PATH = DATA_DIRECTORY / "danbooru_tags.csv"
SETTINGS_PATH = DATA_DIRECTORY / "settings.ini"


def ensure_data_directory() -> Path:
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
    return DATA_DIRECTORY
