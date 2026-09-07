# LLM Value Tracker — SPEC

Веб-сервис, который собирает Elo-рейтинги моделей (LMArena) и цены (OpenRouter),
матчит их по именам, считает **Value Score** и отдаёт через REST API + статичный
дашборд. Данные обновляются по расписанию и кэшируются; падение источника не роняет
сервис.

---

## 1. Источники данных

| Что | Откуда | Как |
|-----|--------|-----|
| Рейтинги (Elo) | HuggingFace dataset `lmarena-ai/leaderboard-dataset` | parquet сплита по HTTP с пином commit sha (`lmarena.fetch_snapshot`), фильтр по `category` — в памяти |
| Цены | OpenRouter `GET /api/v1/models` | `pricing.prompt` / `pricing.completion` — цена за **токен**, привести к $/1M (× 1e6) |

**Не** парсить HF Space `lmarena-ai/arena-leaderboard` — это iframe-обёртка вокруг
сайта, данных оттуда удобно не достать.

Категории вкладок UI — это не отдельные subset'ы датасета, а `category` внутри
текстовой арены (+ `webdev` как отдельная арена для кода). Маппинг
`вкладка → (subset, category)` задаётся в конфиге, не хардкодится.

### Главный риск — маппинг имён

Имена не совпадают: LMArena `claude-opus-4-6-thinking` ↔ OpenRouter
`anthropic/claude-opus-4.6`. Часть моделей (preview / thinking-варианты) на
OpenRouter может вообще отсутствовать. Поэтому:

- `model_aliases` в конфиге — ядро, не опция.
- Несматченные модели **не выбрасываются молча** — попадают в `unmatched` снапшота
  и в `/api/health`.

---

## 2. Value Score

Используется алгоритм **Экспоненциальной Эффективной Цены (Exponential Effective Price)**, который опирается на медиану Топ-N моделей.

```
M_top_N = median(rating_1, ..., rating_N)             # медиана топ-N моделей
Δ       = rating - M_top_N                            # расстояние до медианы
price   = token_share·input + (1 - token_share)·output # средневзвешенная цена $/1M
price_eff = price * e^(-k * Δ)                        # эффективная цена
value   = 100 / price_eff^γ
```

- `top_n_for_median` (дефолт 10), `token_share` (0.75 ≈ 3:1 вход:выход) — в конфиге.
- `k` (sensitivity) и `γ` (gamma) задаются **пресетами**. Value осмыслен для сравнения **внутри одного пресета**,
  не как абсолют. Чем больше `k`, тем сильнее штраф за отставание от медианы и скидка за опережение.

| Пресет | k | γ | Смысл |
|--------|---|---|-------|
| `quality`  | 0.015 | 0.3 | топ важнее цены, огромный штраф за низкий рейтинг |
| `balanced` | 0.010 | 0.5 | качество на доллар, умеренная чувствительность к цене (дефолт) |
| `budget`   | 0.005 | 1.0 | честная стоимость владения, считаем каждый доллар |

---

## 3. Архитектура

```
                ┌──────────── worker (APScheduler) ────────────┐
                │  каждые refresh_interval:                     │
LMArena HF ─────┤   1. fetch ratings (latest)                   │
OpenRouter  ────┤   2. fetch prices                             │──► Cache
                │   3. match по aliases → blended_price, value  │    (snapshot.json)
                │   4. atomic write snapshot                    │
                └───────────────────────────────────────────────┘
                                                                      │
   static dashboard ──GET /api/models?category=&preset=───────────► FastAPI ◄┘
                    ──GET /api/health, /api/meta──────────────►
```

Сервис стартует с последним снапшотом из кэша (если есть), worker обновляет в фоне.
Первый прогрев — при старте, если кэш пуст.

---

## 4. Запуск

```bash
git clone <repo> && cd llm-value-tracker
cp .env.example .env        # при необходимости вписать HF_TOKEN
docker compose up
# UI:   http://localhost:8000
# API:  http://localhost:8000/api/models?category=coding&preset=balanced
```

Вся конфигурация — `config.yaml` + переопределение через env. Тот же образ
поднимается на внешнем хостинге (порт/конфиг наружу через env).

---

## 5. API

| Метод | Ответ |
|-------|-------|
| `GET /api/models?category=&preset=` | отсортированный список: rating, input/output/blended price, value |
| `GET /api/health` | статус + время последнего успешного фетча каждого источника + кол-во unmatched |
| `GET /api/meta` | список категорий, пресетов, версия/время снапшота |

---

## 6. Кэш / хранение

Абстрактный интерфейс `Cache`. Старт — `FileCache` (`data/snapshot.json`, атомарная
запись через temp + rename). Позже без переписывания логики подменяется на
Redis/SQLite. Снапшот хранит: модели по категориям, время обновления, статус
источников, список unmatched.

---

## 7. Вне MVP

- Публичный доступ: хостинг, healthcheck, rate-limit на API.
- Файловый кэш → Redis.
- История во времени: датасет даёт `full` split → график «рейтинг/value во времени».
- **Лицензия:** перед публичным развёртыванием свериться с актуальными условиями
  использования LMArena dataset и OpenRouter API (на момент написания лицензия
  датасета обсуждалась в HF discussions).

---

## 8. Стек

Python 3.12 · FastAPI · APScheduler · `datasets` (HF) · `httpx` · PyYAML · Uvicorn.
