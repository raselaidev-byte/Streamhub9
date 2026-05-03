"""
Local storage utilities: list, move, delete recordings.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List

from config.settings import settings
from monitoring.logger import get_logger

log = get_logger(__name__)


class LocalStorage:
    """Per-user local file operations."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.base: Path = settings.user_storage_path(user_id)

    def list_files(self) -> List[Path]:
        return sorted(self.base.glob("job_*"), key=lambda p: p.stat().st_mtime, reverse=True)

    def delete_file(self, path: Path) -> bool:
        try:
            path.unlink()
            log.info("file_deleted", path=str(path))
            return True
        except FileNotFoundError:
            return False
        except OSError as e:
            log.error("file_delete_error", path=str(path), error=str(e))
            return False

    def total_size_mb(self) -> float:
        return sum(f.stat().st_size for f in self.list_files() if f.exists()) / 1e6

    def move_to_completed(self, path: Path) -> Path:
        """Move file into user's 'completed' sub-folder."""
        completed = self.base / "completed"
        completed.mkdir(exist_ok=True)
        dest = completed / path.name
        shutil.move(str(path), str(dest))
        return dest
