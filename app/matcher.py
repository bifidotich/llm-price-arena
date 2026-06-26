"""Автоматический матчинг моделей LM Arena -> OpenRouter.

4 слоя без ручных алиасов:
  1. Точное совпадение по нормализованному имени
  2. HuggingFace ID (если есть у OpenRouter)
  3. Organization + Name (группировка по организации)
  4. Prefix/Suffix stripping (даты, версии, -thinking и т.д.)
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any

log = logging.getLogger("matcher")


# ============================================================
# Нормализация
# ============================================================

_STRIP_SUFFIXES = [
    r"-latest$",
    r"-preview$",
    r"-thinking.*$",
    r"-chat$",
    r"-instruct$",
    r"-turbo$",
    r"-snapshot$",
    r"-exp\d+$",
    r"-\d{6}$",          # -20250219
    r"-\d{4}-\d{2}-\d{2}$",  # -2025-02-19
]


def normalize(name: str) -> str:
    """Приводит имя к нижнему регистру, заменяет разделители на '-', обрезает суффиксы."""
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    name = name.strip("-")
    for pat in _STRIP_SUFFIXES:
        name = re.sub(pat, "", name)
    name = name.strip("-")
    return name


def org_normalize(org: str) -> str:
    """Нормализует название организации для сравнения."""
    org = org.lower().strip().replace(" ", "-")
    org = re.sub(r"[^a-z0-9-]+", "", org)
    return org


# ============================================================
# Маппинг названий организаций LM Arena -> OpenRouter
# ============================================================

ORG_MAP: dict[str, str] = {
    "alibaba": "qwen",
    "ant-group": "anthropic",
    "deepseek": "deepseek",
    "google": "google",
    "meta": "meta-llama",
    "mistral": "mistralai",
    "moonshot": "moonshotai",
    "openai": "openai",
    "xai": "x-ai",
    "zai": "z-ai",
    "amazon": "amazon",
    "nvidia": "nvidia",
    "cohere": "cohere",
    "ibm": "ibm",
    "xiaomi": "minimax",
    "baidu": "baidu",
    "inception-ai": "inception",
    "meituan": "meituan",
    "step": "stepfun",
    "tencent": "tencent",
    "xverse": "xverse",
    "01-ai": "01-ai",
    "ai2": "ai2",
    "anthopic": "anthropic",
    "ant-thropy": "anthropic",
    "bytedance": "bytedance",
    "inflection": "inflection",
    "reka": "reka",
    "snowflake": "snowflake",
    "together": "together",
    "upstage": "upstage",
    "writer": "writer",
}


# ============================================================
# Pre-индексация OpenRouter для быстрого поиска
# ============================================================


def index_openrouter(or_models: list[dict]) -> tuple[dict[str, dict], dict[str, str]]:
    """Индексирует OpenRouter: возвращает (by_norm, hf_to_or).

    by_norm: {нормализованное_имя: {or_id, price, org}}
    hf_to_or: {нормализованное_имя_из_hf: or_id}
    """
    by_norm: dict[str, dict] = {}
    hf_to_or: dict[str, str] = {}

    for m in or_models:
        or_id: str = m["id"]
        parts = or_id.split("/")
        name_part = parts[-1].rstrip(":free") if parts else or_id
        org = parts[0].lstrip("~").lower() if len(parts) > 1 else ""

        n = normalize(name_part)

        pricing = m.get("pricing", {})
        price_data = {
            "input": float(pricing.get("prompt", 0)),
            "output": float(pricing.get("completion", 0)),
        }

        # Основной индекс по нормализованному имени (без организации)
        if n and or_id not in by_norm:
            by_norm[n] = {"id": or_id, "org": org, "price": price_data, "name_orig": name_part}

        # HuggingFace ID индекс
        hf = m.get("hugging_face_id")
        if hf and "/" in hf:
            hf_name = hf.split("/")[-1]
            hf_n = normalize(hf_name)
            if hf_n and hf_n not in hf_to_or:
                hf_to_or[hf_n] = or_id

    return by_norm, hf_to_or


# ============================================================
# Матчинг одной LM модели
# ============================================================


def _match_single(
    lm_name: str,
    lm_norm: str,
    lm_org: str,
    or_by_norm: dict[str, dict],
    hf_to_or: dict[str, str],
    or_by_org: dict[str, list[dict]],
) -> str | None:
    """Пытается найти OpenRouter ID для одной LM модели.

    Слои:
      1. Точное совпадение по нормализованному имени
      2. Совпадение по HuggingFace ID (нормализованному)
      3. Поиск по организации + fuzzy по имени
      4. Fuzzy по имени (кросс-организация, low confidence)
    """
    # Layer 1: Точное совпадение
    if lm_norm in or_by_norm:
        return or_by_norm[lm_norm]["id"]

    # Layer 2: HuggingFace ID
    if lm_norm in hf_to_or:
        return hf_to_or[lm_norm]

    # Layer 3: Organization + Name
    mapped_org = ORG_MAP.get(lm_org, lm_org)
    candidates = or_by_org.get(mapped_org, [])
    if candidates:
        best_id: str | None = None
        best_ratio = 0.4
        for c in candidates:
            c_norm = normalize(c["name"])
            ratio = SequenceMatcher(None, lm_norm, c_norm).ratio()
            # Также проверяем, что одно имя содержит другое
            if lm_norm in c_norm or c_norm in lm_norm:
                ratio = max(ratio, 0.75)
            if ratio > best_ratio:
                best_ratio = ratio
                best_id = c["id"]
        if best_id:
            return best_id

    # Layer 4: Fuzzy across all (low confidence, only if ratio > 0.85)
    best_id = None
    best_ratio = 0.85
    for n, info in or_by_norm.items():
        ratio = SequenceMatcher(None, lm_norm, n).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_id = info["id"]

    return best_id


# ============================================================
# Полный пайплайн
# ============================================================


def auto_match_all(
    lm_models: list[dict[str, Any]],
    or_models: list[dict[str, Any]],
) -> tuple[dict[str, dict], set[str]]:
    """Матчит все LM модели против OpenRouter.

    Args:
        lm_models: [{model, rating, rank}, ...] из fetch_ratings
        or_models: список OpenRouter API моделей

    Returns:
        (matched, unmatched):
          matched: {or_id: {input, output, rating, rank}}
          unmatched: set LM model names без матча
    """
    or_by_norm, hf_to_or = index_openrouter(or_models)

    # Группируем OpenRouter по организации
    or_by_org: dict[str, list[dict]] = {}
    for m in or_models:
        parts = m["id"].split("/")
        org = parts[0].lstrip("~").lower() if len(parts) > 1 else ""
        name_part = parts[-1].rstrip(":free")
        or_by_org.setdefault(org, []).append({"id": m["id"], "name": name_part})

    matched: dict[str, dict] = {}
    unmatched: set[str] = set()

    for lm in lm_models:
        lm_name = lm["model"]
        lm_norm = normalize(lm_name)
        lm_org = org_normalize(lm.get("org", ""))

        or_id = _match_single(lm_name, lm_norm, lm_org, or_by_norm, hf_to_or, or_by_org)

        if or_id and or_id in or_by_norm:
            price = or_by_norm[or_id]["price"]
            if or_id not in matched:  # первый матч
                matched[or_id] = {
                    "input": price["input"],
                    "output": price["output"],
                    "rating": lm["rating"],
                    "rank": lm["rank"],
                }
        else:
            unmatched.add(lm_name)

    log.info(
        "auto_match_all: %d matched, %d unmatched",
        len(matched),
        len(unmatched),
    )
    return matched, unmatched
