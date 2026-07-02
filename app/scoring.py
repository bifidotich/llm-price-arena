"""Value Score: качество (winProb) на доллар (blended price), с весами β/γ.

См. SPEC.md §2. Метрика осмысленна для сравнения ВНУТРИ одного пресета.
"""
from __future__ import annotations


import math

def blended_price(input_price: float, output_price: float, token_share: float) -> float:
    """Средневзвешенная цена $/1M: token_share вход + (1-token_share) выход."""
    return token_share * input_price + (1.0 - token_share) * output_price


def value_score(
    rating: float,
    input_price: float,
    output_price: float,
    *,
    median_rating: float,
    token_share: float,
    k: float,
    gamma: float,
) -> float | None:
    """Возвращает value или None, если цена неизвестна/некорректна.
    
    Алгоритм Экспоненциальной Эффективной Цены (Exponential Effective Price):
    - Расстояние до медианы: Δ = Rating - MedianTopN
    - Эффективная цена: Price_eff = Price * e^(-k * Δ)
    - Итоговый скор: 100 / (Price_eff^γ)
    """
    price = blended_price(input_price, output_price, token_share)
    if price <= 0:
        return None
        
    delta = rating - median_rating
    
    try:
        price_eff = price * math.exp(-k * delta)
    except OverflowError:
        # В случае невероятно огромной разницы
        if delta < 0:
            return 0.0  # Огромный штраф = 0 скор
        return 1e9      # Огромная награда = макс скор
        
    if price_eff <= 0:
        return None
        
    return 100.0 / (price_eff ** gamma)
