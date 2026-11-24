"""Configuration read from environment variables (12-factor friendly)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    busy_timeout_ms: int = 5_000

    @classmethod
    def from_env(cls) -> "Settings":
        raw_path = os.environ.get("PPI_DB_PATH", "./data/ppi.sqlite3")
        return cls(
            db_path=Path(raw_path).expanduser(),
            busy_timeout_ms=int(os.environ.get("PPI_BUSY_TIMEOUT_MS", "5000")),
        )
