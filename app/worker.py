"""Worker: тянет источники, матчит по алиасам, считает value, пишет снапшот в кэш."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .cache import Cache
from .matcher import auto_match_all
from .scoring import (
    DEFAULT_PRICE_FLOOR_1M,
    anchor_effective_price,
    blended_price,
    effective_price,
    market_price_slope,
    median_top_rating,
    price_is_floored,
    rating_for_metric,
    value_score,
)
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

    # 1. OpenRouter: каталог. Он нужен для матчинга; цена из него — тариф
    #    дефолтного эндпоинта, поэтому дальше уточняется по /endpoints.
    raw_models: list[dict] = []
    try:
        raw_models = openrouter.fetch_raw_models(or_cfg["url"])
    except Exception as e:  # noqa: BLE001
        log.warning("OpenRouter fetch failed: %s", e)
        status["openrouter"] = f"error: {e}"

    categories: dict[str, list[dict]] = {}
    all_unmatched: set[str] = set()

    top_n = sc.get("top_n_for_median", 10)
    price_floor = sc.get("price_floor_1M", DEFAULT_PRICE_FLOOR_1M)
    rating_basis = sc.get("rating_basis", "lower")

    # Эмпирический ориентир для `k`: наклон ln(price) по рейтингу на самих
    # данных. Пишется в снапшот по каждой вкладке, чтобы выбор `k` можно было
    # сверить с рынком, а не держать «на глаз» (REWORK.md §3).
    calibration: dict[str, dict] = {}

    # 2. LMArena: ревизия резолвится один раз на цикл, каждый subset качается
    #    один раз (а не по разу на вкладку) и фильтруется по категориям в памяти.
    lm_revision: str | None = None
    try:
        lm_revision = lmarena.resolve_revision(
            lm_cfg["dataset"], lm_cfg.get("revision", "main")
        )
    except Exception as e:  # noqa: BLE001
        log.warning("LMArena revision resolve failed: %s", e)
        status["lmarena"] = f"error: {e}"

    boards: dict[str, lmarena.Leaderboard] = {}
    if lm_revision:
        for subset in sorted({s["subset"] for s in lm_cfg["categories"].values()}):
            try:
                boards[subset] = lmarena.fetch_snapshot(
                    lm_cfg["dataset"], subset, lm_cfg["split"], lm_revision
                )
            except Exception as e:  # noqa: BLE001
                log.warning("LMArena fetch failed for subset %s: %s", subset, e)
                status["lmarena"] = f"error: {e}"

    # 3. Матчинг по всем вкладкам сразу: полный набор слагов нужно знать
    #    до похода за ценами, иначе детальные запросы уйдут по разу на вкладку.
    matched_by_tab: dict[str, dict[str, dict]] = {}
    for tab, spec in lm_cfg["categories"].items():
        board = boards.get(spec["subset"])
        if board is None:
            matched_by_tab[tab] = {}
            continue

        lm_models = board.by_category(spec["category"])
        if not lm_models:
            # Раньше опечатка в категории давала молча пустую вкладку.
            log.warning(
                "LMArena: категория %r отсутствует в %s/%s (есть: %s)",
                spec["category"], spec["subset"], lm_cfg["split"],
                ", ".join(sorted(board.categories())) or "—",
            )
            matched_by_tab[tab] = {}
            continue

        # Автоматический матчинг (передаём raw OpenRouter модели, не prices dict)
        matched_or, unmatched_lm = auto_match_all(lm_models, raw_models)
        all_unmatched |= unmatched_lm
        matched_by_tab[tab] = matched_or

    # 4. Цены: по одному запросу /endpoints на слаг вместо каталожного тарифа
    #    дефолтного эндпоинта. Слаги объединяются по всем вкладкам, поэтому
    #    модель из трёх категорий стоит один запрос, а не три.
    price_policy = openrouter.PricePolicy.from_config(or_cfg, sc["token_share"])
    all_or_ids = {or_id for matched in matched_by_tab.values() for or_id in matched}
    quotes: dict[str, openrouter.PriceQuote] = {}
    price_stats: dict[str, int] = {}
    if all_or_ids:
        try:
            quotes, price_stats = openrouter.quote_prices(
                sorted(all_or_ids), raw_models, price_policy, or_cfg["url"]
            )
        except Exception as e:  # noqa: BLE001
            log.warning("OpenRouter endpoint prices failed: %s", e)
            status["openrouter"] = f"error: {e}"

    # 5. По каждой вкладке: медиана → value → строки
    for tab, matched_or in matched_by_tab.items():
        if not matched_or:
            categories[tab] = []
            continue

        # В метрику идёт не точечная оценка, а нижняя граница CI: в верхушке
        # разрыв между соседями меньше ширины интервала, и точечный рейтинг
        # выдаёт за качество шум выборки голосов (SPEC.md §2). Показывается
        # при этом по-прежнему `rating`.
        metric_rating = {
            or_id: rating_for_metric(
                info["rating"], info.get("rating_lower"), basis=rating_basis
            )
            for or_id, info in matched_or.items()
        }

        # Якорь Δ — медиана топ-N по рейтингу (SPEC.md §2). Раньше медиана
        # считалась по самым свежим моделям, то есть зависела от `created` =
        # даты появления слага в OpenRouter, а не даты релиза модели.
        median_rating = median_top_rating(metric_rating.values(), top_n)

        closest_model_id = None
        min_diff = float('inf')
        for or_id in matched_or:
            diff = abs(metric_rating[or_id] - median_rating)
            if diff < min_diff:
                min_diff = diff
                closest_model_id = or_id

        # Котировка есть почти всегда; info[input/output] — каталожный тариф
        # из матчера, запасной вариант на случай сбоя всего блока цен.
        row_prices: dict[str, tuple[float, float]] = {}
        for or_id, info in matched_or.items():
            quote = quotes.get(or_id)
            row_prices[or_id] = (
                quote.input if quote else info["input"],
                quote.output if quote else info["output"],
            )

        # Строки, чью цену подменил price_floor (`:free` и грошовые слаги):
        # их price_eff — константа конфига, поэтому в якорь они не идут.
        floored = {
            or_id: price_is_floored(
                in_price, out_price,
                token_share=sc["token_share"], price_floor=price_floor,
            )
            for or_id, (in_price, out_price) in row_prices.items()
        }

        # Эффективная цена по каждому пресету + якорь категории: value
        # нормируется на медиану платных строк, поэтому шкала не зависит ни от
        # абсолютного уровня цен в категории, ни от одной аномально дешёвой
        # модели (раньше якорем был минимум, и его держал `:free` с полом).
        effs: dict[str, dict[str, float | None]] = {}
        anchor_eff: dict[str, float | None] = {}
        for preset, w in sc["presets"].items():
            effs[preset] = {
                or_id: effective_price(
                    metric_rating[or_id], in_price, out_price,
                    median_rating=median_rating, token_share=sc["token_share"],
                    k=w["k"], price_floor=price_floor,
                )
                for or_id, (in_price, out_price) in row_prices.items()
            }
            anchor = anchor_effective_price(
                v for or_id, v in effs[preset].items() if not floored[or_id]
            )
            if anchor is None:
                # Платных строк в категории нет вовсе — опереться не на что,
                # кроме пола; пустая колонка value была бы хуже.
                anchor = anchor_effective_price(effs[preset].values())
            anchor_eff[preset] = anchor

        # Наклон рынка: во сколько раз он сам берёт за очко рейтинга.
        # `k` выше наклона — метрика тянет к качеству, ниже — к цене.
        priced = [
            (metric_rating[or_id], blended_price(i, o, sc["token_share"]))
            for or_id, (i, o) in row_prices.items()
            if not floored[or_id]
        ]
        calibration[tab] = {
            "market_slope": market_price_slope(
                (r for r, _ in priced), (p for _, p in priced)
            ),
            "rating_basis": rating_basis,
            "n_priced": len(priced),
        }

        rows = []
        for or_id, info in matched_or.items():
            in_price, out_price = row_prices[or_id]
            quote = quotes.get(or_id)
            row = {
                "model": or_id,
                "rating": info["rating"],
                # Границы 95% CI: в верхушке они шире разрыва между соседями,
                # поэтому таблица показывает ±, а метрика берёт нижнюю.
                "rating_lower": info.get("rating_lower", info["rating"]),
                "rating_upper": info.get("rating_upper", info["rating"]),
                "rank": info["rank"],
                "input_price_1M": round(in_price, 4),
                "output_price_1M": round(out_price, 4),
                "blended_price_1M": round(
                    blended_price(in_price, out_price, sc["token_share"]), 4
                ),
                # Чья это цена: провайдер, тир, квантизация, разброс по слагу.
                "price_source": quote.as_dict() if quote else None,
                # Цена ниже price_floor: в метрику пошёл сам порог, поэтому
                # строка не участвовала в якоре и помечается в UI звёздочкой.
                "price_is_floored": floored[or_id],
                "is_median": or_id == closest_model_id,
                "created": info.get("created", 0),
                # Цена, приведённая к рейтингу медианы — то, что стоит за
                # value: её, в отличие от индекса, можно прочитать как $/1M.
                "effective_price_1M": {},
                "value": {},
            }
            for preset, w in sc["presets"].items():
                eff = effs[preset][or_id]
                v = value_score(eff, anchor_eff[preset], gamma=w["gamma"])
                row["effective_price_1M"][preset] = round(eff, 4) if eff is not None else None
                row["value"][preset] = round(v, 2) if v is not None else None
            rows.append(row)

        categories[tab] = rows

    return {
        "updated_at": _now_iso(),
        "status": status,
        "sources": {
            "lmarena": {
                "dataset": lm_cfg["dataset"],
                "split": lm_cfg["split"],
                # Полный sha: снапшот воспроизводим, дрейф `latest` виден.
                "revision": lm_revision,
                "publish_date": {
                    subset: b.publish_date for subset, b in boards.items()
                },
            },
            "openrouter": {
                "url": or_cfg["url"],
                # Чем именно считается «цена модели» — иначе числа в таблице
                # нечем поверить: у одного слага цены расходятся до 5x.
                "price": {
                    "policy": price_policy.policy,
                    "exclude_tiers": sorted(price_policy.exclude_tiers),
                    "exclude_quantizations": sorted(price_policy.exclude_quantizations),
                    **price_stats,
                },
            },
        },
        "unmatched": sorted(all_unmatched),
        # Наклон «цена ~ рейтинг» по каждой вкладке — ориентир для выбора `k`.
        "calibration": calibration,
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
