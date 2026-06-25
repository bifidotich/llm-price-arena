"""Загрузка config.yaml с переопределением через переменные окружения.

Env-переменная вида SECTION__KEY переопределяет config[section][key].
Пример: WORKER__REFRESH_INTERVAL_MINUTES=60
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def _coerce(value: str) -> Any:
    """Приводим строку из env к int/float/bool/str."""
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


def _apply_env_overrides(cfg: dict) -> dict:
    for env_key, raw in os.environ.items():
        if "__" not in env_key:
            continue
        path = [p.lower() for p in env_key.split("__")]
        node = cfg
        for part in path[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[path[-1]] = _coerce(raw)
    return cfg


def load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("CONFIG_PATH", "config.yaml")
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return _apply_env_overrides(cfg)


CONFIG = load_config()
