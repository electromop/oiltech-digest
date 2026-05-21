# OilTech Digest

Система сбора нефтесервисных новостей с последующей AI-обработкой (суть → скоринг → тегирование) и формированием месячного дайджеста. Архитектура и схема БД — в [`docs/architecture.md`](docs/architecture.md).
Порядок ручной проверки — в [`docs/testing.md`](docs/testing.md).

**Статус:** Issue #1 — сбор RSS + базовая БД (PostgreSQL). Начат AI processing слой:
суть статьи, тегирование, скоринг, метрики токенов/стоимости и черновик digest content.

## Требования
- Docker (движок; на macOS — например, через `colima`)
- Python 3.11+

## Быстрый старт

```bash
# 1. Поднять PostgreSQL
cp .env.example .env          # при необходимости поправить пароль
docker-compose up -d db       # либо: docker compose up -d db

# 2. Python-окружение
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Пайплайн сбора
python -m oiltech_digest.cli init-db          # создать схему БД
python -m oiltech_digest.cli seed-sources      # загрузить источники из Excel
python -m oiltech_digest.cli discover-rss       # найти RSS-ленты (отчёт применимости)
python -m oiltech_digest.cli parse              # спарсить ленты в articles
python -m oiltech_digest.cli stats              # диагностика

# 4. AI-обработка (OpenAI / ChatGPT API)
python -m oiltech_digest.cli seed-tags          # D01-D18 → tags (+ name_en/keywords_en_json)
python -m oiltech_digest.cli seed-scoring       # базовый профиль весов, сумма = 100
python -m oiltech_digest.cli process --limit 20 # summary → tagging → scoring
python -m oiltech_digest.cli ai-cost-report     # токены/стоимость по этапам и языкам
python -m oiltech_digest.cli ai-article-cost-report # стоимость полного прогона 1 статьи
```

## Admin UI

После запуска PostgreSQL и seed/parse команд:

```bash
uvicorn oiltech_digest.api:app --reload --host 127.0.0.1 --port 8000
```

Откройте [`http://127.0.0.1:8000`](http://127.0.0.1:8000). Интерфейс работает с реальной БД:
статьи, дайджест, источники, теги, критерии скоринга и отчёты по AI-стоимости.

Добавьте `-v` для подробного лога: `python -m oiltech_digest.cli -v discover-rss`.

Для локальной проверки без API-ключа используйте `--offline`:

```bash
python -m oiltech_digest.cli process --offline --limit 5
```

## Команды

| Команда | Назначение |
|---|---|
| `init-db` | создать схему БД (идемпотентно) |
| `seed-sources` | загрузить 120 источников из `1_Список_источников_для_дайджеста.xlsx` |
| `discover-rss` | автообнаружение RSS по сайту источника; `--force`, `--dry-run`, `--source-id`, `--workers`, `--limit`, `--timeout` |
| `parse` | спарсить ленты в `articles`; `--max-age-days`, `--source-id`, `--workers` |
| `stats` | количество источников/статей, распределение по стратегиям, кросс-дубли |
| `seed-tags` | загрузить D01-D18 из `2_Направления_и_ключевые_слова.xlsx` в `tags`; EN-поля сохраняются только в БД |
| `seed-scoring` | создать базовые критерии скоринга с суммой весов 100 |
| `summarize` | сформировать краткую AI-суть в `article_cards.summary`; `--limit`, `--offline` |
| `tag` | присвоить статьям один тег в `article_tags`; `--limit`, `--offline` |
| `score` | рассчитать `article_scores` и детализацию `article_score_items`; `--limit`, `--offline` |
| `process` | выполнить `summarize → tag → score` одной командой |
| `ai-cost-report` | агрегировать токены/стоимость по этапам и языкам для Issue #10 |
| `ai-article-cost-report` | посчитать стоимость полного AI-прогона одной статьи: summary + tagging + scoring |
| `sources` | вывести источники; `--search`, `--limit` |
| `source-enable` | включить/выключить источник: `source-enable 12 --no-enabled` |
| `source-add-rss` | вручную добавить RSS-источник; поддерживает `--frequency` |
| `digest-content` | собрать JSON-черновик дайджеста по месяцу и score |

## OpenAI API

AI-функции используют OpenAI Responses API через переменные окружения:

```bash
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5-nano
OPENAI_INPUT_USD_PER_MTOK=0.05
OPENAI_OUTPUT_USD_PER_MTOK=0.40
```

Модель и цены вынесены в `.env`, чтобы их можно было актуализировать без правки кода.
По умолчанию выбран `gpt-5-nano` как недорогая модель для классификации,
извлечения и ранжирования.

## Структура

```
oiltech_digest/
  config.py          # пути, DATABASE_URL, константы парсера
  db/                # schema.sql, connection, repository
  ingestion/         # excel_seed, rss_discovery, http_client, rss_parser, normalize
  processing/        # OpenAI client, prompts, seed, summary/tagging/scoring, digest
  api.py             # FastAPI backend + static admin UI
  cli.py
docker-compose.yml   # PostgreSQL 16
```
