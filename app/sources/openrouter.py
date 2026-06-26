"""Цены из OpenRouter: GET /api/v1/models.

Ответ (упрощённо):
  {"data": [{"id": "anthropic/claude-opus-4.6",
             "pricing": {"prompt": "0.000005", "completion": "0.000025"}}, ...]}
pricing.* — цена за ОДИН токен (строкой). Приводим к $/1M умножением на 1e6.
"""
from __future__ import annotations

import os

import httpx

PER_MILLION = 1_000_000


def fetch_raw_models(url: str, timeout: float = 30.0) -> list[dict]:
    """Возвращает raw список моделей из OpenRouter API.

    Используется для автоматического матчинга (matcher.py).
    """
    headers = {}
    if key := os.environ.get("OPENROUTER_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"

    resp = httpx.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("data", [])


def fetch_prices(url: str, timeout: float = 30.0) -> dict[str, dict[str, float]]:
    """Возвращает {openrouter_id: {"input": $/1M, "output": $/1M}}."""
    data = fetch_raw_models(url, timeout=timeout)

    prices: dict[str, dict[str, float]] = {}
    for item in data:
        model_id = item.get("id")
        pricing = item.get("pricing") or {}
        try:
            prompt = float(pricing.get("prompt", 0)) * PER_MILLION
            completion = float(pricing.get("completion", 0)) * PER_MILLION
        except (TypeError, ValueError):
            continue
        if model_id and prompt > 0:
            prices[model_id] = {"input": prompt, "output": completion}
    return prices
