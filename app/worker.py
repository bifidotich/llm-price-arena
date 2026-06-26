"""Worker: тянет источники, матчит по алиасам, считает value, пишет снапшот в кэш."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .cache import Cache
from .matcher import auto_match_all
from .scoring import blended_price, value_score
from .sources import lmarena, openrouter

log = logging.getLogger("worker")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_snapshot(cfg: dict) -> dict:
    """Собирает полный снапшот по всем категориям и пресетам.

    Матчинг LM Arena → OpenRouter полностью автоматический (4 слоя),
    без ручных алиасов. Модели без матча попадают в unmatched.
    """
    sc = cfg["scoring"]
    or_cfg = cfg["sources"]["openrouter"]
    lm_cfg = cfg["sources"]["lmarena"]

    status = {"openrouter": "ok", "lmarena": "ok"}

    # 1. OpenRouter цены (все модели, фетчим один раз)
    try:
        prices = openrouter.fetch_prices(or_cfg["url"])
    except Exception as e:  # noqa: BLE001
        log.warning("OpenRouter fetch failed: %s", e)
        prices, status["openrouter"] = {}, f"error: {e}"

    categories: dict[str, list[dict]] = {}
    all_unmatched: set[str] = set()

    # 2. По каждой категории: рейтинги → матчинг → value
    for tab, spec in lm_cfg["categories"].items():
        try:
            lm_models = lmarena.fetch_ratings(
                lm_cfg["dataset"], spec["subset"], lm_cfg["split"], spec["category"]
            )
        except Exception as e:  # noqa: BLE001
            log.warning("LMArena fetch failed for %s: %s", tab, e)
            status["lmarena"] = f"error: {e}"
            categories[tab] = []
            continue

        # Автоматический матчинг
        matched_or, unmatched_lm = auto_match_all(lm_models, prices)
        all_unmatched |= unmatched_lm

        rows = []
        for or_id, info in matched_or.items():
            price = {"input": info["input"], "output": info["output"]}
            row = {
                "model": or_id,
                "rating": info["rating"],
                "rank": info["rank"],
                "input_price_1M": round(price["input"], 4),
                "output_price_1M": round(price["output"], 4),
                "blended_price_1M": round(
                    blended_price(price["input"], price["output"], sc["token_share"]), 4
                ),
                "value": {},
            }
            for preset, w in sc["presets"].items():
                v = value_score(
                    info["rating"], price["input"], price["output"],
                    anchor=sc["anchor"], token_share=sc["token_share"],
                    beta=w["beta"], gamma=w["gamma"],
                )
                row["value"][preset] = round(v, 2) if v is not None else None
            rows.append(row)

        categories[tab] = rows

    return {
        "updated_at": _now_iso(),
        "status": status,
        "unmatched": sorted(all_unmatched),
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
