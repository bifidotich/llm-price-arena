"""Value Score: цена, приведённая к качеству, и её нормировка внутри категории.

См. SPEC.md §2. Метрика считается в две ступени:

1. `effective_price` — «сколько стоила бы модель, будь у неё рейтинг медианы»:
   `price_eff = blended · e^(-k·Δ)`, где `Δ = rating - median`. Величина
   остаётся в $/1M и читается глазами: меньше — выгоднее.
2. `value_score` — нормировка внутри категории: лучшая модель = 100,
   остальные = `100 · (price_eff_best / price_eff)^γ`, то есть «сколько
   процентов от эффективности лидера». Значение лежит в (0, 100] и сравнимо
   только внутри одной категории и одного пресета.

Порядок строк задаёт **только `k`**: value монотонно убывает по `price_eff`,
а возведение в степень `γ > 0` — монотонное преобразование, оно растягивает
шкалу, но не меняет ранжирование.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Iterable

# Нижняя граница цены $/1M. Бесплатные (`:free`) и грошовые слаги иначе дают
# деление на ноль и бесконечный value; порог — «даром не бывает».
DEFAULT_PRICE_FLOOR_1M = 0.01

# Предел |k·Δ| в экспоненте. На Elo-шкале осмысленный максимум ≈ 10
# (Δ = 700 очков при k = 0.015); ограничение защищает от чужой шкалы
# и мусорного рейтинга, заменяя прежнюю ветку OverflowError с магическим 1e9.
MAX_EXPONENT = 30.0


def blended_price(input_price: float, output_price: float, token_share: float) -> float:
    """Средневзвешенная цена $/1M: token_share вход + (1-token_share) выход."""
    return token_share * input_price + (1.0 - token_share) * output_price


def median_top_rating(ratings: Iterable[float], top_n: int) -> float:
    """Медиана `top_n` лучших по рейтингу — якорь Δ (SPEC.md §2).

    Именно по рейтингу, а не по дате: `created` — это дата появления слага
    в OpenRouter, а не релиза модели (REWORK.md §2), и якорь метрики от неё
    зависеть не должен.
    """
    top = sorted((r for r in ratings if r is not None), reverse=True)[: max(1, top_n)]
    return statistics.median(top) if top else 0.0


def effective_price(
    rating: float,
    input_price: float,
    output_price: float,
    *,
    median_rating: float,
    token_share: float,
    k: float,
    price_floor: float = DEFAULT_PRICE_FLOOR_1M,
) -> float | None:
    """Цена $/1M, приведённая к рейтингу медианы. None — если цена неизвестна.

    Отставание от медианы дорожает экспоненциально, опережение — дешевеет;
    `k` задаёт, во сколько раз стоит одно очко рейтинга.
    """
    price = blended_price(input_price, output_price, token_share)
    if not math.isfinite(price) or price < 0:
        return None

    price = max(price, price_floor)
    exponent = -k * (rating - median_rating)
    exponent = max(-MAX_EXPONENT, min(MAX_EXPONENT, exponent))
    return price * math.exp(exponent)


def value_score(
    price_eff: float | None, best_price_eff: float | None, *, gamma: float
) -> float | None:
    """0..100 внутри категории: 100 — лучшая эффективная цена в ней.

    `gamma` растягивает шкалу (0.3 — плотно у сотни, 1.0 — линейно по цене),
    но не меняет порядок строк.
    """
    if not price_eff or not best_price_eff or price_eff <= 0 or best_price_eff <= 0:
        return None
    return 100.0 * (best_price_eff / price_eff) ** gamma
