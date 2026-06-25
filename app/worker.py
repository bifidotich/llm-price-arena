"""Worker: тянет источники, матчит по алиасам, считает value, пишет снапшот в кэш."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .cache import Cache
from .scoring import blended_price, value_score
from .sources import lmarena, openrouter

log = logging.getLogger("worker")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_snapshot(cfg: dict) -> dict:
    """Собирает полный снапшот по всем категориям и пресетам.

    Источники тянутся независимо: сбой одного фиксируется в status, но не
    отменяет другой. Сборка value требует обоих — если рейтинги не пришли,
    отдаём пустые категории, но не падаем.
    """
    sc = cfg["scoring"]
    aliases: dict[str, str] = cfg.get("model_aliases") or {}
    or_cfg = cfg["sources"]["openrouter"]
    lm_cfg = cfg["sources"]["lmarena"]

    status = {"openrouter": "ok", "lmarena": "ok"}

    # 1. Цены
    try:
        prices = openrouter.fetch_prices(or_cfg["url"])
    except Exception as e:  # noqa: BLE001 — фиксируем и продолжаем
        log.warning("OpenRouter fetch failed: %s", e)
        prices, status["openrouter"] = {}, f"error: {e}"

    categories: dict[str, list[dict]] = {}
    unmatched: set[str] = set()

    # 2. Рейтинги по категориям + матчинг + value
    for tab, spec in lm_cfg["categories"].items():
        try:
            ratings = lmarena.fetch_ratings(
                lm_cfg["dataset"], spec["subset"], lm_cfg["split"], spec["category"]
            )
        except Exception as e:  # noqa: BLE001
            log.warning("LMArena fetch failed for %s: %s", tab, e)
            status["lmarena"] = f"error: {e}"
            categories[tab] = []
            continue

        rows = []
        for r in ratings:
            or_id = aliases.get(r["model"])
            price = prices.get(or_id) if or_id else None
            if not price:
                unmatched.add(r["model"])
                continue

            row = {
                "model": r["model"],
                "rating": r["rating"],
                "rank": r["rank"],
                "input_price_1M": round(price["input"], 4),
                "output_price_1M": round(price["output"], 4),
                "blended_price_1M": round(
                    blended_price(price["input"], price["output"], sc["token_share"]), 4
                ),
                "value": {},
            }
            for preset, w in sc["presets"].items():
                v = value_score(
                    r["rating"], price["input"], price["output"],
                    anchor=sc["anchor"], token_share=sc["token_share"],
                    beta=w["beta"], gamma=w["gamma"],
                )
                row["value"][preset] = round(v, 2) if v is not None else None
            rows.append(row)

        categories[tab] = rows

    return {
        "updated_at": _now_iso(),
        "status": status,
        "unmatched": sorted(unmatched),
        "presets": list(sc["presets"].keys()),
        "default_preset": sc.get("default_preset", "balanced"),
        "categories": categories,
    }


def refresh(cfg: dict, cache: Cache) -> dict:
    """Полный цикл обновления; пишет в кэш и возвращает снапшот."""
    t0 = time.monotonic()
    snap = build_snapshot(cfg)
    cache.write(snap)
    log.info(
        "snapshot updated in %.1fs · status=%s · unmatched=%d",
        time.monotonic() - t0, snap["status"], len(snap["unmatched"]),
    )
    return snap
