"""Value Score: цена, приведённая к качеству, и её нормировка внутри категории.

См. SPEC.md §2. Метрика считается в две ступени:

1. `effective_price` — «сколько стоила бы модель, будь у неё рейтинг медианы»:
   `price_eff = blended · e^(-k·Δ)`, где `Δ = rating - median`. Величина
   остаётся в $/1M и читается глазами: меньше — выгоднее.
2. `value_score` — нормировка внутри категории на **медиану**:
   `100 · (price_eff_anchor / price_eff)^γ`, то есть «сколько процентов от
   эффективности медианной модели категории». 100 — как у медианы, больше —
   выгоднее её, меньше — дороже. Сравнимо только внутри одной категории и
   одного пресета.

   Якорь — медиана, а не минимум: минимум держала одна произвольная строка,
   и вся шкала категории зависела от того, попала ли в неё аномально дешёвая
   модель. Из выборки якоря исключены строки, чью цену подменил `price_floor`
   (`price_is_floored`): у них `price_eff` — не рынок, а константа, и одна
   `:free`-модель утаскивала на себя всю сотню.

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


def price_is_floored(
    input_price: float,
    output_price: float,
    *,
    token_share: float,
    price_floor: float = DEFAULT_PRICE_FLOOR_1M,
) -> bool:
    """True, если в метрику пошла не цена модели, а сам `price_floor`.

    Такая строка (`:free`, грошовый слаг, битая цена) не участвует в выборе
    якоря шкалы: её `price_eff` — константа конфига, а не рынок, и якорь от
    неё зависеть не должен.
    """
    price = blended_price(input_price, output_price, token_share)
    return not math.isfinite(price) or price < price_floor


def anchor_effective_price(prices: Iterable[float | None]) -> float | None:
    """Медиана `price_eff` — якорь шкалы value (SPEC.md §2).

    Медиана, а не минимум: минимум держит одна строка, поэтому появление
    в категории аномально дешёвой модели сдвигало value всем остальным.
    """
    known = [p for p in prices if p is not None and p > 0]
    return statistics.median(known) if known else None


def value_score(
    price_eff: float | None, anchor_price_eff: float | None, *, gamma: float
) -> float | None:
    """Проценты от эффективности якоря: 100 — как у медианной модели категории.

    Сверху не ограничено: больше 100 — выгоднее медианы. Взамен шкала не
    зависит от того, есть ли в категории одна аномально дешёвая строка.
    `gamma` растягивает её (0.3 — плотно у сотни, 1.0 — линейно по цене),
    но не меняет порядок строк.
    """
    if not price_eff or not anchor_price_eff or price_eff <= 0 or anchor_price_eff <= 0:
        return None
    return 100.0 * (anchor_price_eff / price_eff) ** gamma
