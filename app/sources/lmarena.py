"""Рейтинги из HF dataset lmarena-ai/leaderboard-dataset.

Публичного API у арены нет — HF-датасет и есть официальный канал. Читаем его
parquet напрямую, без зависимости `datasets`:

  1. resolve_revision() — `main` -> commit sha, один раз за цикл обновления.
     Все subsets одного цикла читаются с одной ревизии, поэтому вкладки не
     могут разъехаться по разным состояниям датасета.
  2. fetch_snapshot() — качает parquet сплита целиком (`latest` ~ 0.6 MB на
     subset) и отдаёт все строки. Фильтрация по category — на стороне воркера,
     чтобы не тянуть файл заново на каждую вкладку.

Схема (revision bc50ae8, 2026-09-06):
  обычные subsets (text*, vision*, search*, document*, webdev, *_to_*):
    model_name, organization, license, rating, rating_lower, rating_upper,
    variance, vote_count, rank, category, leaderboard_publish_date
  agent-subsets вместо rating/vote_count: score, score_ci_lower,
    score_ci_upper, observation_count, session_count.

ВНИМАНИЕ: `score` агентных арен лежит в диапазоне 0..1, а не в Elo-шкале.
Подставлять его в scoring.value_score с `k`, подобранным под очки Elo, нельзя —
рейтинг перестанет влиять на результат. См. Leaderboard.scale.

Границы доверительного интервала читаются наравне с самим рейтингом: разница
между соседями в верхушке таблицы меньше ширины CI (на ревизии bc50ae8 в топ-10
`overall` 25 пар из 45 статистически неразличимы), поэтому метрика считается по
нижней границе, а не по точечной оценке — см. `scoring.rating_for_metric`.
"""
from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass, field

import httpx
import pyarrow.parquet as pq

log = logging.getLogger("lmarena")

HF_API = "https://huggingface.co/api/datasets"
HF_RESOLVE = "https://huggingface.co/datasets"

DEFAULT_SUBSET = "text_style_control"
DEFAULT_SPLIT = "latest"
DEFAULT_REVISION = "main"

COL_MODEL = "model_name"
COL_ORG = "organization"
COL_RANK = "rank"
COL_CATEGORY = "category"
COL_DATE = "leaderboard_publish_date"
# Elo-шкала (обычные арены) / доля 0..1 (агентные арены) — берём что есть.
COL_RATING = "rating"
COL_SCORE = "score"
COL_VOTES = "vote_count"
COL_OBSERVATIONS = "observation_count"
# Границы 95% CI. У обычных арен они же выводятся из `variance`
# (rating ± 1.96·√variance), у агентных лежат под своими именами.
COL_RATING_LOWER = "rating_lower"
COL_RATING_UPPER = "rating_upper"
COL_SCORE_LOWER = "score_ci_lower"
COL_SCORE_UPPER = "score_ci_upper"

_WANTED_COLUMNS = (
    COL_MODEL, COL_ORG, COL_RANK, COL_CATEGORY, COL_DATE,
    COL_RATING, COL_SCORE, COL_VOTES, COL_OBSERVATIONS,
    COL_RATING_LOWER, COL_RATING_UPPER, COL_SCORE_LOWER, COL_SCORE_UPPER,
)

_SHA_RE = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Leaderboard:
    """Один сплит одного subset, прочитанный с конкретной ревизии."""

    subset: str
    split: str
    revision: str
    #: "elo" для обычных арен, "score" для агентных (диапазон 0..1).
    scale: str
    publish_date: str | None
    rows: list[dict] = field(default_factory=list)

    def categories(self) -> set[str]:
        return {r["category"] for r in self.rows if r.get("category")}

    def by_category(self, category: str | None) -> list[dict]:
        """Строки одной категории; None — все строки сплита."""
        if category is None:
            return list(self.rows)
        return [r for r in self.rows if r.get("category") == category]


def _headers() -> dict[str, str]:
    token = os.environ.get("HF_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def resolve_revision(
    dataset: str,
    revision: str = DEFAULT_REVISION,
    timeout: float = 30.0,
) -> str:
    """Разворачивает ref (`main`, тег, короткий sha) в полный commit sha.

    Полный sha пишется в снапшот: по нему сборка воспроизводится побайтово,
    а исчезнувшая ревизия даёт честный 404 вместо молча других чисел.
    """
    url = f"{HF_API}/{dataset}/revision/{revision}"
    resp = httpx.get(url, headers=_headers(), timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    sha = resp.json().get("sha")
    if not sha:
        raise ValueError(f"HF не вернул sha для {dataset}@{revision}")
    return sha


def _split_files(
    dataset: str,
    subset: str,
    split: str,
    revision: str,
    timeout: float = 30.0,
) -> list[str]:
    """Пути к parquet-шардам сплита на заданной ревизии.

    Имя шарда (`latest-00000-of-00001.parquet`) — конвенция шардирования и
    меняется, когда сплит перестаёт помещаться в один файл, поэтому список
    берём из tree API, а не хардкодим.
    """
    url = f"{HF_API}/{dataset}/tree/{revision}/{subset}?recursive=1"
    resp = httpx.get(url, headers=_headers(), timeout=timeout, follow_redirects=True)
    resp.raise_for_status()

    shard = re.compile(rf"^{re.escape(split)}(-\d+-of-\d+)?\.parquet$")
    files = []
    for entry in resp.json():
        if entry.get("type") != "file":
            continue
        path = entry["path"]
        if not path.endswith(".parquet"):
            continue
        # плоский layout (subset/latest-00000-of-00001.parquet) либо вложенный
        # (subset/latest/0000.parquet) — датасет уже переезжал между ними.
        if shard.match(path.rsplit("/", 1)[-1]) or f"/{split}/" in path:
            files.append(path)
    if not files:
        raise FileNotFoundError(f"{dataset}@{revision}: нет parquet для {subset}/{split}")
    return sorted(files)


def _read_parquet(dataset: str, path: str, revision: str, timeout: float) -> list[dict]:
    """Скачивает один шард и отдаёт только нужные колонки."""
    url = f"{HF_RESOLVE}/{dataset}/resolve/{revision}/{path}"
    resp = httpx.get(url, headers=_headers(), timeout=timeout, follow_redirects=True)
    resp.raise_for_status()

    buf = io.BytesIO(resp.content)
    available = set(pq.ParquetFile(buf).schema_arrow.names)
    buf.seek(0)
    columns = [c for c in _WANTED_COLUMNS if c in available]
    return pq.read_table(buf, columns=columns).to_pylist()


def _as_float(*candidates) -> float:
    """Первое значение, которое приводится к float; последнее — гарантия."""
    for c in candidates:
        try:
            if c is not None:
                return float(c)
        except (TypeError, ValueError):
            continue
    return 0.0


def _row(raw: dict) -> dict | None:
    """Приводит строку датасета к виду, который ждут matcher и worker."""
    model = raw.get(COL_MODEL)
    rating = raw.get(COL_RATING)
    if rating is None:
        rating = raw.get(COL_SCORE)
    if not model or rating is None:
        return None
    try:
        rating = float(rating)
    except (TypeError, ValueError):
        return None

    rank = raw.get(COL_RANK)
    votes = raw.get(COL_VOTES)
    if votes is None:
        votes = raw.get(COL_OBSERVATIONS)

    # Границы CI: у агентных арен под своими именами, а если их нет вовсе —
    # схлопываются в саму оценку, и метрика ведёт себя как раньше.
    lower = _as_float(raw.get(COL_RATING_LOWER), raw.get(COL_SCORE_LOWER), rating)
    upper = _as_float(raw.get(COL_RATING_UPPER), raw.get(COL_SCORE_UPPER), rating)

    return {
        "model": model,
        "rating": rating,
        "rating_lower": min(lower, rating),
        "rating_upper": max(upper, rating),
        "rank": int(rank) if rank is not None else None,
        # matcher.auto_match_all читает org для слоя "organization + name"
        "org": raw.get(COL_ORG) or "",
        "category": raw.get(COL_CATEGORY),
        "votes": int(votes) if votes is not None else None,
        "publish_date": raw.get(COL_DATE),
    }


def fetch_snapshot(
    dataset: str,
    subset: str | None = None,
    split: str = DEFAULT_SPLIT,
    revision: str | None = None,
    timeout: float = 120.0,
) -> Leaderboard:
    """Читает сплит целиком с зафиксированной ревизии.

    `revision` — полный sha (из resolve_revision) либо ref; None = `main`,
    и тогда ревизия резолвится здесь же, чтобы её всё равно можно было
    записать в снапшот.
    """
    subset = subset or DEFAULT_SUBSET
    if revision is None or not _SHA_RE.fullmatch(revision):
        revision = resolve_revision(dataset, revision or DEFAULT_REVISION, timeout=30.0)

    rows: list[dict] = []
    for path in _split_files(dataset, subset, split, revision):
        for raw in _read_parquet(dataset, path, revision, timeout):
            if (row := _row(raw)) is not None:
                rows.append(row)

    scale = "score" if subset.startswith("agent") else "elo"
    dates = {r["publish_date"] for r in rows if r.get("publish_date")}
    publish_date = max(dates) if dates else None
    board = Leaderboard(
        subset=subset,
        split=split,
        revision=revision,
        scale=scale,
        publish_date=publish_date,
        rows=rows,
    )
    log.info(
        "lmarena %s/%s @%s: %d rows, %d categories, published %s",
        subset, split, revision[:8], len(rows), len(board.categories()), publish_date,
    )
    return board


def fetch_ratings(
    dataset: str,
    subset: str | None = None,
    split: str = DEFAULT_SPLIT,
    category: str | None = None,
    revision: str | None = None,
) -> list[dict]:
    """Совместимость: одна категория одним вызовом.

    Качает весь сплит, поэтому для нескольких категорий используйте
    fetch_snapshot + Leaderboard.by_category — так файл берётся один раз.
    """
    return fetch_snapshot(dataset, subset, split, revision).by_category(category)
