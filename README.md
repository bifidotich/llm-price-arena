# LLM Price Arena

Benchmark-to-price ratio analytics service. Aggregates Elo ratings from **LMArena** and inference pricing from **OpenRouter**, computes parametrizable **Value Score** metrics, and exposes results via a **REST API** and a **static dashboard**.

Full specification: [`SPEC.md`](./SPEC.md)

---

## Overview

LLM Price Arena is a monitoring and comparison tool that:

- Fetches **Elo rankings** from the `lmarena-ai/leaderboard-dataset` HuggingFace dataset (categories: overall, coding, math, research, agent)
- Retrieves **per-token pricing** from OpenRouter's public model registry
- Maps model names between the two sources via a configurable alias table
- Computes a **Value Score** metric: price adjusted for quality (Elo), then normalized within each category against the median model, which scores 100
- Caches results atomically and updates on a configurable schedule via APScheduler
- Serves data through a FastAPI application with background worker

---

## Quick Start

```bash
cp .env.example .env        # optionally set HF_TOKEN for authenticated datasets
cd llm-price-arena
docker compose up
```

- **Dashboard:** <http://localhost:8000>
- **API (example):** <http://localhost:8000/api/models?category=coding&preset=balanced>
- **Health endpoint:** <http://localhost:8000/api/health>

---

## Architecture

```
                     ┌──────── worker (APScheduler) ───────────┐
                     │  every refresh_interval:                 │
LMArena HF ──────────┤   1. fetch ratings (latest)              │
OpenRouter  ─────────┤   2. fetch prices                        │──► Cache
                     │   3. match via aliases → blended price,  │    (snapshot.json)
                     │      effective price, value score       │
                     │   4. atomic write snapshot               │
                     └──────────────────────────────────────────┘
                                                                       │
   static dashboard ──GET /api/models?category=&preset=───────────► FastAPI ◄┘
                    ──GET /api/health, /api/meta──────────────►
```

### Key Components

| Component | File | Responsibility |
|-----------|------|----------------|
| **API & Scheduler** | `app/main.py` | FastAPI application, lifespan-managed APScheduler, static file mount |
| **Worker** | `app/worker.py` | Orchestrates fetching, matching, value computation, cache writes |
| **Scoring Engine** | `app/scoring.py` | Value Score formula: quality-adjusted price (Elo → effective $/1M) normalized against the category median, with `k`/`γ` presets |
| **Cache Layer** | `app/cache.py` | Abstract `Cache` interface + `FileCache` with atomic temp+rename writes |
| **Config** | `app/config.py` | YAML configuration with environment variable overrides (`SECTION__KEY` syntax) |
| **LMArena Source** | `app/sources/lmarena.py` | HuggingFace dataset loader for Elo leaderboard |
| **OpenRouter Source** | `app/sources/openrouter.py` | HTTP client for model pricing data |
| **Dashboard** | `static/index.html` | Dynamic HTML/CSS/JS dashboard with scatter plots, bar charts, and sortable tables |

---

## Value Score Formula

The metric is computed in two steps: price is first adjusted for quality, then
normalized within the category.

```
quality   = rating_lower  (rating_basis: lower)             # lower 95% CI bound
M_top_N   = median(top_n highest by quality)                # Δ anchor
Δ         = quality - M_top_N                               # distance to the anchor
price     = token_share × input + (1 - token_share) × output    # blended $/1M
floored   = price < price_floor_1M                          # the floor replaced the price
price     = max(price, price_floor_1M)                      # :free must not divide by zero
price_eff = price × e^(-k·Δ)                                # $/1M at the anchor's quality
P_anchor  = median(price_eff over rows with floored = false)    # value scale anchor
value     = 100 × (P_anchor / price_eff)^γ                  # 100 = the category median
```

- **`price_eff`** — what the model would cost if it were rated at the anchor.
  It stays in dollars and ships in the snapshot as `effective_price_1M` per
  preset; unlike the index, it can be read directly.
- **`value`** — percent of the category median's efficiency. 100 matches the
  median, above 100 beats it, below 100 costs more. Unbounded above; comparable
  **within one category and one preset only**.
- The scale anchor is a **median, not a minimum**. A minimum is held by one
  arbitrary row, so a single unusually cheap model shifted everyone else's
  value: in the 2026-09-08 snapshot `thinkingmachines/inkling:free` took the
  100 and flattened the whole frontier to 3–4 points.
- Rows with `floored = true` (`price_is_floored` in the snapshot) are **left out
  of the anchor sample**: their `price_eff` is `price_floor_1M`, a config
  constant rather than a market price. They stay in the table, marked with an
  asterisk — their value rests on the floor and reads as "free", not as a
  measured quantity.
- The metric consumes the **lower bound of the 95% CI**, not the point estimate
  (`scoring.rating_for_metric`, switched by `scoring.rating_basis`). Near the top
  of the board the gap between neighbours is smaller than the interval: on the
  2026-09-02 slice the `overall` top 10 spans 20.4 points at a median CI
  half-width of 5.0, and 25 of its 45 pairs are statistically indistinguishable
  (`|Δ| > √(hw_i² + hw_j²)`) — the top four included. A point estimate would sell
  vote-sampling noise as quality; the lower bound withholds credit from models
  that have not yet earned the lead on votes. The table still shows `rating`,
  with `±` beside it.
- The Δ anchor is a median by **rating**, not by date: OpenRouter's `created` is
  when the slug appeared in the catalog, not when the model shipped.
- `top_n_for_median` (10), `token_share` (0.75 ≈ 3:1 in:out) and
  `price_floor_1M` (0.01, just under the cheapest paid model on the market —
  $0.025 as of 2026-09-08) live in the config.
- `k` and `γ` come from **presets**. `k` is how much rating buys back price and
  is the **only** parameter that changes the ordering; `γ` stretches the value
  scale around 100 (`γ < 1` packs the distribution) without reordering anything.

### Presets

| Preset | k | γ | Interpretation |
|--------|---|---|---------------|
| **quality** | 0.015 | 0.3 | 100 Elo points ≈ 4.5× the price; dense scale, lagging behind is barely punished by cost |
| **balanced** | 0.010 | 0.5 | Default: 100 Elo points ≈ 2.7× the price |
| **budget** | 0.005 | 1.0 | 100 Elo points ≈ 1.6× the price; value is linear in effective price |

Measured on the 2026-09-08 snapshot (`overall`, 175 models), the presets produce
different but strongly correlated orderings — Kendall's τ of 0.66–0.84, with
6–8 of the top 10 shared.

**Calibrating `k`.** The reference point is the market's own slope: a regression
of `ln(blended_price)` on rating over the paid rows of a category. It is measured
every cycle and written to the snapshot as `calibration.<tab>.market_slope`
(served from `/api/meta`), so the choice of `k` can be checked against the data
instead of resting on taste. On 2026-09-08 the slope is 0.0056–0.0069 across tabs
(`overall` 0.0069, R² 0.16 — rating explains only a sixth of the price spread).
Against it, `budget` (0.005) tracks the market, `balanced` (0.010) is ~1.5× as
steep, `quality` (0.015) ~2.2×.

Raising `k` much further does not help, and this is **not** a tuning problem: a
single `k` fixes the exchange rate across the whole range at once. Making the
20.4-point spread of the top 10 worth real money (`k` = 0.035 buys 2.04×) blows
the category's full 417-point span up to 2 200 000×, and what gets amplified is
mostly noise — the CI half-width in the top 10 is ±5.0 points, so the band of
indistinguishability (±7) is comparable to the spread itself. That is why the
metric reads the lower CI bound instead of leaning harder on `k`.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/models?category=coding&preset=balanced` | Sorted model list with ratings, prices, and value scores |
| `GET` | `/api/health` | Service status, last update timestamp, source health, unmatched model count |
| `GET` | `/api/meta` | Available categories, presets, default preset, snapshot metadata |

### Response Format

```json
{
  "category": "coding",
  "preset": "balanced",
  "updated_at": "2026-06-26T00:00:00",
  "count": 50,
  "models": [
    {
      "model": "claude-opus-4-6",
      "rating": 1535.3,
      "rank": 1,
      "input_price_1M": 5.0,
      "output_price_1M": 25.0,
      "blended_price_1M": 10.0,
      "value": { "quality": 24.5, "balanced": 42.3, "budget": 8.1 }
    }
  ]
}
```

---

## Configuration

All parameters are defined in [`config.yaml`](./config.yaml) and can be overridden via environment variables using double-underscore nesting (e.g., `SERVER__PORT=8080`).

### Critical configuration sections

- **`scoring.presets`** — `k`/`γ` weights for each value profile (`k` sets the ranking, `γ` only stretches the scale)
- **`scoring.price_floor_1M`** — price floor in $/1M so `:free` models get a score instead of `null`
- **`sources.lmarena.categories`** — mapping from UI tabs to dataset subsets and category filters
- **`model_aliases`** — model name mapping between LMArena and OpenRouter identifiers

> **Note:** The `model_aliases` table is the most fragile component. Models without a matching alias fall into the `unmatched` set visible via `/api/meta` and `/api/health`. Currently 35 aliases are registered; production deployments should verify and extend this mapping.

---

## Production Considerations

1. **Dataset schema verification** — confirm column names and category values before enabling production workflows:
   ```bash
   python -c "from app.sources.lmarena import fetch_snapshot as f; \
     b = f('lmarena-ai/leaderboard-dataset', 'text_style_control'); \
     print(b.revision, b.publish_date); print(sorted(b.categories())); print(b.rows[0])"
   ```
   Update constants in `app/sources/lmarena.py` and category filters in `config.yaml` accordingly.
   An unknown `category` no longer yields a silently empty tab — the worker logs a
   warning listing the categories the subset actually has.

2. **Model alias coverage** — incomplete aliases result in unmatched models (visible in health checks). Review and extend `config.yaml:model_aliases` as needed.

3. **Cache persistence** — the file cache (`data/snapshot.json`) survives container restarts when mounted as a Docker volume (configured in `docker-compose.yml`).

4. **License compliance** — verify terms of use for LMArena datasets and OpenRouter API before public deployment.

---

## Project Structure

```
├── app/
│   ├── main.py              # FastAPI application entry point
│   ├── worker.py             # Background data refresh worker
│   ├── scoring.py            # Value Score computation
│   ├── cache.py              # Cache abstraction and file implementation
│   ├── config.py             # YAML + environment variable configuration loader
│   └── sources/
│       ├── __init__.py
│       ├── openrouter.py     # OpenRouter pricing client
│       └── lmarena.py        # HuggingFace dataset loader
├── static/
│   └── index.html            # Dynamic dashboard (API-backed)
├── config.yaml               # Service configuration
├── SPEC.md                   # Full specification document
├── README.md                 # This file
├── README.ru.md              # Русская версия
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## Stack

| Component | Technology |
|-----------|-----------|
| Runtime | Python 3.12 |
| Web framework | FastAPI 0.115 |
| Scheduler | APScheduler 3.10 |
| HTTP client | httpx 0.27 |
| Dataset | HuggingFace parquet over HTTP, pinned revision (pyarrow 25.x) |
| Configuration | PyYAML 6.x |
| ASGI server | Uvicorn (included with FastAPI) |
| Dashboard | Vanilla HTML/CSS/JS, hand-rolled SVG scatter + DOM bar chart |
| Containerization | Docker + Docker Compose |

---

## License

Before public deployment, verify licensing terms for LMArena datasets and OpenRouter API. See discussion in [SPEC.md §7](./SPEC.md).