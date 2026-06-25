"""Слой кэша. Старт — файловый; интерфейс позволяет подменить на Redis/SQLite."""
from __future__ import annotations

import json
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class Cache(ABC):
    @abstractmethod
    def read(self) -> dict[str, Any] | None: ...

    @abstractmethod
    def write(self, snapshot: dict[str, Any]) -> None: ...


class FileCache(Cache):
    """JSON-файл с атомарной записью (temp + os.replace)."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def write(self, snapshot: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)  # атомарно
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def make_cache(cfg: dict) -> Cache:
    backend = cfg.get("backend", "file")
    if backend == "file":
        return FileCache(cfg.get("path", "data/snapshot.json"))
    raise ValueError(f"Unknown cache backend: {backend!r}")  # TODO: redis
