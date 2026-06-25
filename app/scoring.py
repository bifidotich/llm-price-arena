"""Value Score: качество (winProb) на доллар (blended price), с весами β/γ.

См. SPEC.md §2. Метрика осмысленна для сравнения ВНУТРИ одного пресета.
"""
from __future__ import annotations


def win_prob(rating: float, anchor: float) -> float:
    """Вероятность победы модели над якорным рейтингом (логистика Elo)."""
    return 1.0 / (1.0 + 10.0 ** ((anchor - rating) / 400.0))


def blended_price(input_price: float, output_price: float, token_share: float) -> float:
    """Средневзвешенная цена $/1M: token_share вход + (1-token_share) выход."""
    return token_share * input_price + (1.0 - token_share) * output_price


def value_score(
    rating: float,
    input_price: float,
    output_price: float,
    *,
    anchor: float,
    token_share: float,
    beta: float,
    gamma: float,
) -> float | None:
    """Возвращает value или None, если цена неизвестна/некорректна."""
    price = blended_price(input_price, output_price, token_share)
    if price <= 0:
        return None
    q = win_prob(rating, anchor)
    return (q ** beta) / (price ** gamma) * 100.0
