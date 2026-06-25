# LLM Value Tracker

Сервис собирает Elo-рейтинги моделей (LMArena) и цены (OpenRouter), считает
**Value Score** и отдаёт через REST API + статичный дашборд. Данные обновляются
по расписанию и кэшируются.

Полное описание — [`SPEC.md`](./SPEC.md).

## Быстрый старт

```bash
cp .env.example .env        # при необходимости вписать HF_TOKEN
docker compose up
```

- UI:   <http://localhost:8000>
- API:  <http://localhost:8000/api/models?category=coding&preset=balanced&top=50>
- Health: <http://localhost:8000/api/health>

## Перед боевым запуском

1. **Сверить схему датасета** — имена колонок и значения `category`:
   ```bash
   python -c "from datasets import load_dataset; \
     d = load_dataset('lmarena-ai/leaderboard-dataset','text_style_control',split='latest'); \
     print(d.features); print(d[0])"
   ```
   Поправить константы в `app/sources/lmarena.py` и `categories` в `config.yaml`.
2. **Заполнить `model_aliases`** в `config.yaml` — главный источник «дыр» в данных.
   Несматченные модели видны в `/api/health` (`unmatched_count`) и `/api/meta`.

## Структура

```
app/
  config.py          конфиг (yaml + env override)
  scoring.py         формула Value Score
  cache.py           абстракция кэша + FileCache
  worker.py          фетч + матчинг + сборка снапшота
  main.py            FastAPI: API, планировщик, статика
  sources/
    openrouter.py    цены
    lmarena.py       рейтинги (HF dataset)
static/index.html    дашборд (ходит в /api/models)
config.yaml          веса формулы, пресеты, источники, алиасы
```

## Дашборд

Положить текущий HTML дашборда в `static/index.html` и заменить инлайн-`data`
на `fetch('/api/models?...')`. Формат ответа `/api/models` совпадает с тем, что
ожидает рендер (rating, input/output/blended price, value по пресетам).
