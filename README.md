# OilTech Digest

Система сбора нефтесервисных новостей с последующей AI-обработкой и формированием
месячного дайджеста.

Текущий рабочий pipeline:

```text
sources -> discover-rss -> parse -> fetch-full-text -> summary -> relevance -> tag -> score -> digest
```

## Документация

- [`docs/project_guide.md`](docs/project_guide.md) — главный гид по проекту: что работает, как все связано и с чего читать код;
- [`docs/architecture.md`](docs/architecture.md) — техническая архитектура и поток данных;
- [`docs/testing.md`](docs/testing.md) — сценарии проверки и smoke-тесты;
- [`docs/work_report.md`](docs/work_report.md) — сводка по уже выполненной работе;
- [`docs/cost_dashboard.html`](docs/cost_dashboard.html) — визуальный отчет по стоимости AI.

## Требования
- Docker (движок; на macOS — например, через `colima`)
- Python 3.11+

## Быстрый старт через Docker

Этот режим поднимает всё сразу:

- `db` — PostgreSQL;
- `app` — FastAPI + Admin UI на порту `8000`;
- `bootstrap` — одноразовая инициализация БД и seed данных;
- `scheduler` — автоматический цикл: `discover-rss → parse → fetch-full-text → process`.

```bash
cp .env.example .env
```

Откройте `.env` и задайте минимум:

```bash
POSTGRES_PASSWORD=сложный_пароль
OPENAI_API_KEY=sk-...
```

Если хотите поднять весь стек без OpenAI API, можно сразу поставить:

```bash
AI_OFFLINE=1
```

Запуск:

```bash
docker compose up -d --build
```

Что произойдет после запуска:

1. `db` поднимет PostgreSQL;
2. `bootstrap` создаст схему и прогонит `seed-sources`, `seed-tags`, `seed-scoring`;
3. `app` поднимет админку;
4. `scheduler` начнет циклический сбор и обработку статей.

Открыть интерфейс:

```text
http://127.0.0.1:8000
```

Проверить состояние сервисов:

```bash
docker compose ps
```

Нормальная картина:

- `db`, `app`, `scheduler` — `running`
- `bootstrap` — `exited (0)`

Посмотреть логи автопарсинга:

```bash
docker compose logs -f scheduler
```

Посмотреть bootstrap:

```bash
docker compose logs -f bootstrap
```

Посмотреть логи интерфейса:

```bash
docker compose logs -f app
```

Остановить:

```bash
docker compose down
```

Сбросить базу полностью:

```bash
docker compose down -v
```

### Настройка расписания

По умолчанию scheduler запускает цикл каждые 6 часов:

```env
CYCLE_INTERVAL_SECONDS=21600
```

Полезные параметры `.env`:

```env
RUN_DISCOVER_ON_START=1       # искать RSS при первом запуске контейнера
DISCOVER_EVERY_CYCLES=4       # повторять discover каждый 4-й цикл
FULL_TEXT_LIMIT=200           # сколько статей за цикл дозагружать полным текстом
AI_PROCESS_LIMIT=100          # сколько статей за цикл отправлять в AI pipeline
AI_OFFLINE=0                  # 1 = тестовый режим без OpenAI API
```

Если `OPENAI_API_KEY` пустой, scheduler всё равно будет собирать статьи и полный текст,
но AI-обработку пропустит.

PostgreSQL проброшен только на `127.0.0.1:5432`, наружу сервером не открыт.

## Ручной локальный старт

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
python -m oiltech_digest.cli parse              # спарсить ленты в articles + отсечь очевидный шум
python -m oiltech_digest.cli fetch-full-text    # дозагрузить полный текст по URL, если RSS дал тизер
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
| `parse` | спарсить ленты в `articles`, перед вставкой отсечь очевидный спорт/культуру/бытовые новости без доменного сигнала; `--max-age-days`, `--source-id`, `--workers` |
| `fetch-full-text` | скачать страницы статей по URL и заменить RSS-анонс на полный текст; `--limit`, `--min-chars` |
| `stats` | количество источников/статей, распределение по стратегиям, кросс-дубли |
| `seed-tags` | загрузить D01-D18 из `2_Направления_и_ключевые_слова.xlsx` в `tags`; EN-поля сохраняются только в БД |
| `seed-scoring` | создать базовые критерии скоринга с суммой весов 100 |
| `summarize` | сформировать краткую AI-суть в `article_cards.summary`; `--limit`, `--offline` |
| `relevance` | AI-фильтр релевантности: отделить нефтесервисные статьи от офтопа; `--limit`, `--offline` |
| `tag` | присвоить статьям один тег в `article_tags`; `--limit`, `--offline` |
| `score` | рассчитать `article_scores` и детализацию `article_score_items`; `--limit`, `--offline` |
| `process` | выполнить `summarize → relevance → tag → score` одной командой |
| `process-articles` | прогнать `summary → relevance → tag → score` по выбранным `article_id` |
| `ai-cost-report` | агрегировать токены/стоимость по этапам и языкам для Issue #10 |
| `ai-article-cost-report` | посчитать стоимость полного AI-прогона одной статьи: summary + relevance + tagging + scoring |
| `sources` | вывести источники; `--search`, `--limit` |
| `article-candidates` | быстро найти статьи-кандидаты по ключевым словам |
| `source-enable` | включить/выключить источник: `source-enable 12 --no-enabled` |
| `source-add-rss` | вручную добавить RSS-источник; поддерживает `--frequency` |
| `digest-content` | собрать JSON-черновик дайджеста по месяцу и score; `--html-output` дополнительно пишет email-ready HTML по шаблону |

## Источники и дайджест в UI/API

В разделе **Источники** доступны список источников, тип, периодичность, RSS URL,
включение/выключение, ручное добавление нового RSS-источника и настройка
`listing_url`/селекторов для non-RSS request-источников. Для таких источников
мониторинг идет по странице-списку публикаций и сохраняет только новые URL статей.

Также админка теперь требует вход по `email + password`.

В разделе **Дайджест** можно:

- открыть HTML-версию email-шаблона;
- скопировать JSON;
- скачать тестовый экспорт в `HTML`, `DOC` или `JSON`.

Ручки:

```text
GET /api/digest-content?month=2026-05&limit=20&min_score=60
GET /api/digest-email?month=2026-05&limit=20&min_score=60
GET /api/digest-export?month=2026-05&limit=20&min_score=60&export_format=html
```

CLI-пример:

```bash
python -m oiltech_digest.cli digest-content 2026-05 \
  --output digest-2026-05.json \
  --html-output digest-2026-05.html
```

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
  processing/        # OpenAI client, prompts, seed, summary/relevance/tagging/scoring, digest
  api.py             # FastAPI backend + static admin UI
  cli.py
docker-compose.yml   # PostgreSQL 16
```
