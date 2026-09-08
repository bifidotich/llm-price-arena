"""Цены из OpenRouter.

Два эндпоинта, и разница между ними — суть модуля:

  GET /api/v1/models
      Каталог. `pricing` здесь — тариф ОДНОГО эндпоинта (дефолтного для
      роутинга), а не минимум по рынку. Нужен для матчинга и как запасной
      вариант, если детальный запрос не прошёл.

  GET /api/v1/models/{slug}/endpoints
      Все варианты обслуживания слага. Замер 2026-09-07 на выборке 41 модели:
      у 51% провайдер не один, а у 7 из 21 мультипровайдерных каталожная цена
      выше минимальной на 10-180% (qwen3.8-27b: $0.42 против $0.15).

Но не всякий дешёвый эндпоинт — это «та же модель дешевле». Критерий один:
совпадают ли веса с теми, на которых снят рейтинг арены.
  * Тир обслуживания веса не меняет. У gpt-5.6-sol-pro один слаг стоит $1
    (`openai/flex`), $2 (`openai`) и $4 (`openai/fast`) — это одна и та же
    модель с разной очередью, и $1 у неё реально можно купить. Тиры в выборе
    участвуют.
  * Квантизация веса меняет. Дно рынка у открытых моделей — `fp4`/`int4`,
    и спаривать их цену с рейтингом неурезанной модели нельзя. Исключены.
Оба списка настраиваются в `sources.openrouter.price` (config.yaml):
`exclude_tiers` по умолчанию пуст, `exclude_quantizations` — нет.

pricing.* — цена за ОДИН токен строкой; приводим к $/1M умножением на 1e6.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

from ..scoring import blended_price

log = logging.getLogger("openrouter")

PER_MILLION = 1_000_000

DEFAULT_URL = "https://openrouter.ai/api/v1/models"

#: Как выбирается цена из списка эндпоинтов.
#:   min     — самый дешёвый сопоставимый (дно рынка)
#:   median  — медианный сопоставимый (типичная цена, устойчива к выбросам)
#:   catalog — как в /models, без детального запроса
POLICIES = ("min", "median", "catalog")

#: Суффиксы тега, по которым эндпоинт выбрасывается из выбора. Пусто:
#: тир обслуживания не меняет веса, а значит и качество, которое мерила арена.
#: Тег устроен как `provider[/суффикс...]`: `openai/flex`, `google-vertex/global/priority`.
DEFAULT_EXCLUDE_TIERS: tuple[str, ...] = ()

#: Квантизации, которые нельзя сопоставлять с рейтингом неурезанных весов.
DEFAULT_EXCLUDE_QUANTIZATIONS = ("fp4", "int4")


def _headers() -> dict[str, str]:
    key = os.environ.get("OPENROUTER_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _usd_per_million(pricing: dict, key: str) -> float:
    """Тариф из `pricing` в $/1M токенов; 0.0 если поля нет или оно кривое."""
    try:
        return float(pricing.get(key) or 0.0) * PER_MILLION
    except (TypeError, ValueError):
        return 0.0


# ============================================================
# Каталог
# ============================================================


def fetch_raw_models(url: str = DEFAULT_URL, timeout: float = 30.0) -> list[dict]:
    """Raw список моделей из каталога — для матчинга (matcher.py)."""
    resp = httpx.get(url, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("data", [])


def catalog_prices(raw_models: list[dict]) -> dict[str, tuple[float, float]]:
    """{or_id: (input $/1M, output $/1M)} по каталожному тарифу.

    Нулевые цены не отбрасываются: `:free`-варианты — валидная точка в
    «цена против качества», а не мусор.
    """
    out: dict[str, tuple[float, float]] = {}
    for item in raw_models:
        if model_id := item.get("id"):
            pricing = item.get("pricing") or {}
            out[model_id] = (
                _usd_per_million(pricing, "prompt"),
                _usd_per_million(pricing, "completion"),
            )
    return out


# ============================================================
# Эндпоинты слага
# ============================================================


@dataclass(frozen=True)
class Endpoint:
    """Один вариант обслуживания слага: провайдер + тир + квантизация."""

    provider: str
    tag: str
    quantization: str
    context_length: int | None
    #: 0 — рабочий; отрицательный — деранкнут или отключён самим OpenRouter.
    status: int
    input: float
    output: float

    def tier(self, exclude: frozenset[str]) -> str | None:
        """Нестандартный тир из тега, если он там есть: `openai/flex` -> `flex`."""
        return next((p for p in self.tag.split("/")[1:] if p in exclude), None)


def _endpoint(raw: dict) -> Endpoint | None:
    pricing = raw.get("pricing") or {}
    if "prompt" not in pricing:
        return None
    try:
        status = int(raw.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    return Endpoint(
        provider=raw.get("provider_name") or "",
        tag=raw.get("tag") or "",
        quantization=(raw.get("quantization") or "unknown").lower(),
        context_length=raw.get("context_length"),
        status=status,
        input=_usd_per_million(pricing, "prompt"),
        output=_usd_per_million(pricing, "completion"),
    )


def fetch_endpoints(
    model_id: str,
    catalog_url: str = DEFAULT_URL,
    client: httpx.Client | None = None,
    timeout: float = 30.0,
) -> list[Endpoint]:
    """Все варианты обслуживания одного слага.

    Вариантные слаги (`...:free`, `...:batch`) сюда не приходят: у них выбор
    провайдера уже сделан суффиксом, см. quote_prices.
    """
    url = f"{catalog_url.rstrip('/')}/{model_id}/endpoints"
    get = client.get if client is not None else httpx.get
    resp = get(url, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    raw = resp.json().get("data", {}).get("endpoints", []) or []
    return [e for e in map(_endpoint, raw) if e is not None]


# ============================================================
# Политика выбора цены
# ============================================================


@dataclass(frozen=True)
class PricePolicy:
    """Чем именно считать «цену модели». Собирается из config.yaml."""

    policy: str = "min"
    token_share: float = 0.75
    exclude_tiers: frozenset[str] = field(default_factory=lambda: frozenset(DEFAULT_EXCLUDE_TIERS))
    exclude_quantizations: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_EXCLUDE_QUANTIZATIONS)
    )
    #: Потолок на число детальных запросов за цикл (один запрос на слаг).
    max_models: int = 500
    max_workers: int = 8
    timeout: float = 30.0

    @classmethod
    def from_config(cls, or_cfg: dict, token_share: float) -> PricePolicy:
        p = or_cfg.get("price") or {}
        policy = str(p.get("policy", "min")).lower()
        if policy not in POLICIES:
            log.warning("неизвестная price.policy %r, беру 'min' (есть: %s)",
                        policy, ", ".join(POLICIES))
            policy = "min"
        return cls(
            policy=policy,
            token_share=token_share,
            exclude_tiers=frozenset(p.get("exclude_tiers", DEFAULT_EXCLUDE_TIERS)),
            exclude_quantizations=frozenset(
                str(q).lower() for q in p.get("exclude_quantizations", DEFAULT_EXCLUDE_QUANTIZATIONS)
            ),
            max_models=int(p.get("max_models", 500)),
            max_workers=int(p.get("max_workers", 8)),
            timeout=float(p.get("timeout", 30.0)),
        )


@dataclass(frozen=True)
class PriceQuote:
    """Цена модели вместе с ответом на вопрос «чья это цена».

    Без provider/tag/quantization число в таблице непроверяемо: $0.87 у
    deepseek-v4-pro — это DigitalOcean, а $1.91 за тот же слаг — Azure.
    """

    input: float
    output: float
    #: "endpoints" — выбор среди сопоставимых; "endpoints-all" — сопоставимых
    #: не осталось, выбирали из всех; "catalog" — детальных данных нет.
    source: str
    policy: str
    provider: str | None = None
    tag: str | None = None
    quantization: str | None = None
    context_length: int | None = None
    #: Сколько эндпоинтов участвовало в выборе / сколько их всего у слага.
    endpoints: int = 0
    endpoints_total: int = 0
    min_blended: float | None = None
    max_blended: float | None = None

    def as_dict(self) -> dict:
        """Блок `price_source` строки снапшота."""
        return {
            "from": self.source,
            "policy": self.policy,
            "provider": self.provider,
            "tag": self.tag,
            "quantization": self.quantization,
            "context_length": self.context_length,
            "endpoints": self.endpoints,
            "endpoints_total": self.endpoints_total,
            "min_blended_1M": (
                None if self.min_blended is None else round(self.min_blended, 4)
            ),
            "max_blended_1M": (
                None if self.max_blended is None else round(self.max_blended, 4)
            ),
        }


def comparable(endpoints: list[Endpoint], pol: PricePolicy) -> list[Endpoint]:
    """Эндпоинты на тех же весах, на которых снят рейтинг арены.

    Отсекаются деранкнутые провайдеры (отрицательный `status`) и урезанные
    веса. Тир обслуживания по умолчанию не отсекается: `openai/flex` — та же
    модель, только в отложенной очереди, и её цену реально можно заплатить.
    """
    return [
        e
        for e in endpoints
        if e.status >= 0
        and e.tier(pol.exclude_tiers) is None
        and e.quantization not in pol.exclude_quantizations
    ]


def select_price(
    endpoints: list[Endpoint],
    catalog: tuple[float, float] | None,
    pol: PricePolicy,
) -> PriceQuote:
    """Применяет политику к списку эндпоинтов; каталог — запасной вариант."""
    pool = comparable(endpoints, pol)
    source = "endpoints"
    if not pool and endpoints:
        # Слаг целиком живёт на нестандартном тире (или на fp4) — честнее
        # показать его цену с пометкой, чем выбросить модель из таблицы.
        pool, source = endpoints, "endpoints-all"

    if not pool:
        inp, out = catalog or (0.0, 0.0)
        return PriceQuote(
            input=inp, output=out, source="catalog", policy=pol.policy,
            endpoints_total=len(endpoints),
        )

    ranked = sorted(pool, key=lambda e: blended_price(e.input, e.output, pol.token_share))
    lo = blended_price(ranked[0].input, ranked[0].output, pol.token_share)
    hi = blended_price(ranked[-1].input, ranked[-1].output, pol.token_share)

    if pol.policy == "catalog" and catalog is not None:
        inp, out = catalog
        chosen = None
    elif pol.policy == "median":
        chosen = ranked[(len(ranked) - 1) // 2]  # median-low: всегда реальный эндпоинт
    else:
        chosen = ranked[0]
    if chosen is not None:
        inp, out = chosen.input, chosen.output

    return PriceQuote(
        input=inp,
        output=out,
        source="catalog" if chosen is None else source,
        policy=pol.policy,
        provider=chosen.provider if chosen else None,
        tag=chosen.tag if chosen else None,
        quantization=chosen.quantization if chosen else None,
        context_length=chosen.context_length if chosen else None,
        endpoints=len(pool),
        endpoints_total=len(endpoints),
        min_blended=lo,
        max_blended=hi,
    )


def quote_prices(
    model_ids: list[str],
    raw_models: list[dict],
    pol: PricePolicy,
    catalog_url: str = DEFAULT_URL,
) -> tuple[dict[str, PriceQuote], dict[str, int]]:
    """Цена для каждого слага: один детальный запрос на слаг, параллельно.

    Возвращает (котировки, счётчики). Падение отдельного запроса не роняет
    цикл — такая модель получает каталожную цену с пометкой `from: catalog`.
    """
    catalog = catalog_prices(raw_models)
    stats = {"queried": 0, "failed": 0, "from_endpoints": 0, "from_catalog": 0}

    ids = sorted(set(model_ids))
    # Вариантный слаг (`:free`, `:batch`) — выбор уже сделан суффиксом,
    # детальный запрос вернул бы эндпоинты базовой модели, т.е. другую цену.
    detailed = [i for i in ids if ":" not in i]
    if len(detailed) > pol.max_models:
        log.warning(
            "детальных запросов %d > max_models %d — остальные по каталогу",
            len(detailed), pol.max_models,
        )
        detailed = detailed[: pol.max_models]

    fetched: dict[str, list[Endpoint]] = {}
    if detailed:
        limits = httpx.Limits(max_connections=pol.max_workers)
        with httpx.Client(limits=limits, timeout=pol.timeout) as client:
            def _one(model_id: str) -> tuple[str, list[Endpoint] | None]:
                try:
                    return model_id, fetch_endpoints(model_id, catalog_url, client, pol.timeout)
                except Exception as e:  # noqa: BLE001
                    log.warning("endpoints failed for %s: %s", model_id, e)
                    return model_id, None

            with ThreadPoolExecutor(max_workers=pol.max_workers) as ex:
                for model_id, eps in ex.map(_one, detailed):
                    if eps is None:
                        stats["failed"] += 1
                    else:
                        fetched[model_id] = eps
        stats["queried"] = len(detailed)

    quotes: dict[str, PriceQuote] = {}
    for model_id in ids:
        quote = select_price(fetched.get(model_id, []), catalog.get(model_id), pol)
        stats["from_catalog" if quote.source == "catalog" else "from_endpoints"] += 1
        quotes[model_id] = quote

    log.info(
        "prices: policy=%s · %d слагов · из эндпоинтов %d · из каталога %d · сбоев %d",
        pol.policy, len(ids), stats["from_endpoints"], stats["from_catalog"], stats["failed"],
    )
    return quotes, stats
