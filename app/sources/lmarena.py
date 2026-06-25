"""Рейтинги из HF dataset lmarena-ai/leaderboard-dataset.

ВАЖНО — сверить со схемой датасета перед боевым запуском:
  python -c "from datasets import load_dataset; \
             d = load_dataset('lmarena-ai/leaderboard-dataset', \
                 'text_style_control', split='latest'); print(d.features); print(d[0])"

TODO по результатам инспекции:
  • точные имена колонок (ниже предполагаются model_name / rating / rank / category);
  • реальные значения поля category для coding/math/research (в config.yaml — заглушки);
  • есть ли смысл в filters= на стороне load_dataset (быстрее), либо фильтровать после.
"""
from __future__ import annotations

import os

from datasets import load_dataset

# Предполагаемые имена колонок — ПРОВЕРИТЬ (см. docstring).
COL_MODEL = "model_name"
COL_RATING = "rating"
COL_RANK = "rank"
COL_CATEGORY = "category"


def fetch_ratings(
    dataset: str,
    subset: str,
    split: str,
    category: str,
) -> list[dict]:
    """Возвращает [{"model": str, "rating": float, "rank": int}, ...] для категории."""
    token = os.environ.get("HF_TOKEN")
    ds = load_dataset(
        dataset,
        subset,
        split=split,
        token=token,
        filters=[(COL_CATEGORY, "==", category)],  # TODO: убрать, если schema иная
    )

    out: list[dict] = []
    for row in ds:
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
