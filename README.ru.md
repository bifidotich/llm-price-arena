# LLM Price Arena

Сервис аналитики соотношения качества и цены LLM-моделей. Агрегирует Elo-рейтинги из **LMArena** и цены инференса из **OpenRouter**, вычисляет параметризуемый **Value Score** и предоставляет результаты через **REST API** и **статический дашборд**.

Полная спецификация: [`SPEC.md`](./SPEC.md)

---

## Обзор

LLM Price Arena — инструмент мониторинга и сравнения, который:

- Загружает **Elo-рейтинги** из датасета HuggingFace `lmarena-ai/leaderboard-dataset` (категории: overall, coding, math, research, agent)
- Получает **поценовую стоимость токенов** из публичного реестра моделей OpenRouter
- Сопоставляет имена моделей между двумя источниками через конфигурируемую таблицу алиасов
- Вычисляет **Value Score** — метрику, балансирующую качество (Elo win probability) и стоимость (средневзвешенная цена за миллион токенов)
- Атомарно кэширует результаты и обновляет их по расписанию через APScheduler
- Отдаёт данные через FastAPI-приложение с фоновым воркером

---

## Быстрый старт

```bash
cp .env.example .env        # опционально: указать HF_TOKEN для авторизации
cd llm-price-arena
docker compose up
```

- **Дашборд:** <http://localhost:8000>
- **API (пример):** <http://localhost:8000/api/models?category=coding&preset=balanced&top=50>
- **Health-check:** <http://localhost:8000/api/health>

---

## Архитектура

```
                     ┌──────── worker (APScheduler) ───────────┐
                     │  каждые refresh_interval:                │
LMArena HF ──────────┤   1. загрузка рейтингов (latest)         │
OpenRouter  ─────────┤   2. загрузка цен                        │──► Cache
                     │   3. матчинг по алиасам → blended price, │    (snapshot.json)
                     │      win probability, value score        │
                     │   4. атомарная запись снапшота           │
                     └──────────────────────────────────────────┘
                                                                       │
   static dashboard ──GET /api/models?category=&preset=&top=──► FastAPI ◄┘
                    ──GET /api/health, /api/meta──────────────►
```

### Ключевые компоненты

| Компонент | Файл | Назначение |
|-----------|------|------------|
| **API & Планировщик** | `app/main.py` | FastAPI-приложение, APScheduler с управлением через lifespan, раздача статики |
| **Worker** | `app/worker.py` | Оркестрация фетчинга, матчинга, вычисления value, записи кэша |
| **Scoring Engine** | `app/scoring.py` | Формула Value Score: win probability (Elo) × blended price с весами β/γ |
| **Слой кэша** | `app/cache.py` | Абстрактный интерфейс `Cache` + `FileCache` с атомарной записью (temp + rename) |
| **Конфиг** | `app/config.py` | YAML-конфигурация с переопределением через переменные окружения (синтаксис `SECTION__KEY`) |
| **Источник LMArena** | `app/sources/lmarena.py` | Загрузчик датасета HuggingFace для Elo-рейтингов |
| **Источник OpenRouter** | `app/sources/openrouter.py` | HTTP-клиент для получения цен моделей |
| **Дашборд** | `static/index.html` | Динамический HTML/CSS/JS дашборд со scatter plot, bar chart и сортируемой таблицей |

---

## Формула Value Score

Ключевая метрика, количественно оценивающая экономическую эффективность модели:

<latex>Value = (WinProb^β / BlendedPrice^γ) × 100</latex>

Где:

- <latex>WinProb = 1 / (1 + 10^{(anchor - rating) / 400})</latex> — логистическая вероятность победы модели над фиксированным якорем (по умолчанию 1400)
- <latex>BlendedPrice = token_share × input_price + (1 - token_share) × output_price</latex> — средневзвешенная стоимость миллиона токенов
- <latex>β</latex>, <latex>γ</latex> — параметры чувствительности, задаваемые в **пресетах**

### Пресеты

| Пресет | β | γ | Интерпретация |
|--------|---|---|---------------|
| **quality** | 2.0 | 0.3 | Качество доминирует; цена второстепенна |
| **balanced** | 1.0 | 0.5 | По умолчанию. Убывающая чувствительность к цене, сбалансированный компромисс |
| **budget** | 1.0 | 1.0 | Линейная чувствительность к цене; каждый доллар учтён одинаково |

---

## API-эндпоинты

| Метод | Endpoint | Описание |
|--------|----------|---------|
| `GET` | `/api/models?category=coding&preset=balanced&top=50` | Отсортированный список моделей с рейтингами, ценами и value score |
| `GET` | `/api/health` | Статус сервиса, время последнего обновления, состояние источников, количество несматченных моделей |
| `GET` | `/api/meta` | Доступные категории, пресеты, пресет по умолчанию, метаданные снапшота |

### Формат ответа

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

## Конфигурация

Все параметры задаются в [`config.yaml`](./config.yaml) и могут быть переопределены через переменные окружения с двойным подчёркиванием для вложенности (например, `SERVER__PORT=8080`).

### Критические секции конфигурации

- **`scoring.presets`** — веса β/γ для каждого профиля value
- **`sources.lmarena.categories`** — маппинг вкладок UI на subset'ы датасета и фильтры категорий
- **`model_aliases`** — маппинг имён моделей между LMArena и OpenRouter

> **Важно:** Таблица `model_aliases` — самый хрупкий компонент. Модели без алиаса попадают в множество `unmatched`, видимое через `/api/meta` и `/api/health`. В настоящее время зарегистрировано 35 алиасов; для боевого развёртывания необходимо верифицировать и расширить этот маппинг.

---

## Рекомендации для боевого запуска

1. **Верификация схемы датасета** — проверить имена колонок и значения категорий перед включением в боевой контур:
   ```bash
   python -c "from datasets import load_dataset; \
     d = load_dataset('lmarena-ai/leaderboard-dataset', 'text_style_control', split='latest'); \
     print(d.features); print(d[0])"
   ```
   При необходимости обновить константы в `app/sources/lmarena.py` и фильтры категорий в `config.yaml`.

2. **Покрытие алиасов** — неполные алиасы приводят к появлению несматченных моделей (отображаются в health-check). Расширять `config.yaml:model_aliases` по мере необходимости.

3. **Персистентность кэша** — файловый кэш (`data/snapshot.json`) сохраняется при перезапуске контейнера, если смонтирован как Docker volume (настроено в `docker-compose.yml`).

4. **Лицензионная чистота** — перед публичным развёртыванием проверить условия использования датасетов LMArena и API OpenRouter.

---

## Структура проекта

```
├── app/
│   ├── main.py              # Точка входа FastAPI-приложения
│   ├── worker.py             # Фоновый воркер обновления данных
│   ├── scoring.py            # Вычисление Value Score
│   ├── cache.py              # Абстракция кэша и файловая реализация
│   ├── config.py             # Загрузчик YAML + переменные окружения
│   └── sources/
│       ├── __init__.py
│       ├── openrouter.py     # HTTP-клиент OpenRouter
│       └── lmarena.py        # Загрузчик датасета HuggingFace
├── static/
│   └── index.html            # Динамический дашборд (на API)
├── config.yaml               # Конфигурация сервиса
├── SPEC.md                   # Полный документ спецификации
├── README.md                 # Английская версия
├── README.ru.md              # Этот файл
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## Стек технологий

| Компонент | Технология |
|-----------|-----------|
| Среда выполнения | Python 3.12 |
| Веб-фреймворк | FastAPI 0.115 |
| Планировщик | APScheduler 3.10 |
| HTTP-клиент | httpx 0.27 |
| Датасет | HuggingFace datasets 3.x |
| Конфигурация | PyYAML 6.x |
| ASGI-сервер | Uvicorn (в составе FastAPI) |
| Дашборд | Vanilla HTML/CSS/JS, pure JS scatter/barchart rendering |
| Контейнеризация | Docker + Docker Compose |

---

## Лицензия

Перед публичным развёртыванием необходимо проверить лицензионные условия датасетов LMArena и API OpenRouter. Обсуждение — в [SPEC.md §7](./SPEC.md).