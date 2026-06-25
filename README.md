# LLM Price Arena

Benchmark-to-price ratio analytics service. Aggregates Elo ratings from **LMArena** and inference pricing from **OpenRouter**, computes parametrizable **Value Score** metrics, and exposes results via a **REST API** and a **static dashboard**.

Full specification: [`SPEC.md`](./SPEC.md)

---

## Overview

LLM Price Arena is a monitoring and comparison tool that:

- Fetches **Elo rankings** from the `lmarena-ai/leaderboard-dataset` HuggingFace dataset (categories: overall, coding, math, research, agent)
- Retrieves **per-token pricing** from OpenRouter's public model registry
- Maps model names between the two sources via a configurable alias table
- Computes a **Value Score** metric balancing quality (Elo win probability) against cost (blended per-million-token price)
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
- **API (example):** <http://localhost:8000/api/models?category=coding&preset=balanced&top=50>
- **Health endpoint:** <http://localhost:8000/api/health>

---

## Architecture

```
                     ┌──────── worker (APScheduler) ───────────┐
                     │  every refresh_interval:                 │
LMArena HF ──────────┤   1. fetch ratings (latest)              │
OpenRouter  ─────────┤   2. fetch prices                        │──► Cache
                     │   3. match via aliases → blended price,  │    (snapshot.json)
                     │      win probability, value score        │
                     │   4. atomic write snapshot               │
                     └──────────────────────────────────────────┘
                                                                       │
   static dashboard ──GET /api/models?category=&preset=&top=──► FastAPI ◄┘
                    ──GET /api/health, /api/meta──────────────►
```

### Key Components

| Component | File | Responsibility |
|-----------|------|----------------|
| **API & Scheduler** | `app/main.py` | FastAPI application, lifespan-managed APScheduler, static file mount |
| **Worker** | `app/worker.py` | Orchestrates fetching, matching, value computation, cache writes |
| **Scoring Engine** | `app/scoring.py` | Value Score formula: win probability (Elo) × blended price with β/γ weights |
| **Cache Layer** | `app/cache.py` | Abstract `Cache` interface + `FileCache` with atomic temp+rename writes |
| **Config** | `app/config.py` | YAML configuration with environment variable overrides (`SECTION__KEY` syntax) |
| **LMArena Source** | `app/sources/lmarena.py` | HuggingFace dataset loader for Elo leaderboard |
| **OpenRouter Source** | `app/sources/openrouter.py` | HTTP client for model pricing data |
| **Dashboard** | `static/index.html` | Dynamic HTML/CSS/JS dashboard with scatter plots, bar charts, and sortable tables |

---

## Value Score Formula

The core metric quantifies the economic efficiency of a model:

<latex>Value = (WinProb^β / BlendedPrice^γ) × 100</latex>

Where:

- <latex>WinProb = 1 / (1 + 10^{(anchor - rating) / 400})</latex> — logistic Elo win probability against a fixed anchor (default 1400)
- <latex>BlendedPrice = token_share × input_price + (1 - token_share) × output_price</latex> — weighted average cost per million tokens
- <latex>β</latex>, <latex>γ</latex> — sensitivity parameters defined in **presets**

### Presets

| Preset | β | γ | Interpretation |
|--------|---|---|---------------|
| **quality** | 2.0 | 0.3 | Quality dominates; cost is secondary |
| **balanced** | 1.0 | 0.5 | Default. Diminishing price sensitivity, balanced quality/cost trade-off |
| **budget** | 1.0 | 1.0 | Linear cost sensitivity; each dollar counted equally |

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/models?category=coding&preset=balanced&top=50` | Sorted model list with ratings, prices, and value scores |
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

- **`scoring.presets`** — β/γ weights for each value profile
- **`sources.lmarena.categories`** — mapping from UI tabs to dataset subsets and category filters
- **`model_aliases`** — model name mapping between LMArena and OpenRouter identifiers

> **Note:** The `model_aliases` table is the most fragile component. Models without a matching alias fall into the `unmatched` set visible via `/api/meta` and `/api/health`. Currently 35 aliases are registered; production deployments should verify and extend this mapping.

---

## Production Considerations

1. **Dataset schema verification** — confirm column names and category values before enabling production workflows:
   ```bash
   python -c "from datasets import load_dataset; \
     d = load_dataset('lmarena-ai/leaderboard-dataset', 'text_style_control', split='latest'); \
     print(d.features); print(d[0])"
   ```
   Update constants in `app/sources/lmarena.py` and category filters in `config.yaml` accordingly.

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
| Dataset | HuggingFace datasets 3.x |
| Configuration | PyYAML 6.x |
| ASGI server | Uvicorn (included with FastAPI) |
| Dashboard | Vanilla HTML/CSS/JS, pure JS scatter/barchart rendering |
| Containerization | Docker + Docker Compose |

---

## License

Before public deployment, verify licensing terms for LMArena datasets and OpenRouter API. See discussion in [SPEC.md §7](./SPEC.md).