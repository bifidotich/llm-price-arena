"""Рейтинги из HF dataset lmarena-ai/leaderboard-dataset (конфиг text).

Схема (2026-06-26):
  model_name (string) rating (float64) rank (float64)
  category: overall, coding, english, creative_writing, chinese, frensh, german,
            expert, hard_prompts, hard_prompts_english, industry_*
"""
from __future__ import annotations

import os

from datasets import load_dataset


CONFIG = "text"
COL_MODEL = "model_name"
COL_RATING = "rating"
COL_RANK = "rank"
COL_CATEGORY = "category"


def fetch_ratings(
    dataset: str,
    subset: str | None = None,
    split: str = "latest",
    category: str | None = None,
) -> list[dict]:
    """Возвращает [{"model": str, "rating": float, "rank": int}, ...] для категории."""
    subset = subset or CONFIG
    token = os.environ.get("HF_TOKEN")
    ds = load_dataset(
        dataset,
        subset,
        split=split,
        token=token,
        streaming=True,
    )

    out: list[dict] = []
    for row in ds:
        if category and row.get(COL_CATEGORY) != category:
            continue
        try:
            out.append(
                {
                    "model": row[COL_MODEL],
                    "rating": float(row[COL_RATING]),
                    "rank": int(row[COL_RANK]) if row.get(COL_RANK) is not None else None,
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out
