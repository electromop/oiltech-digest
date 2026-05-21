# OilTech Digest

Система сбора нефтесервисных новостей с последующей AI-обработкой (суть → скоринг → тегирование) и формированием месячного дайджеста. Архитектура и схема БД — в [`docs/architecture.md`](docs/architecture.md).

**Статус:** Issue #1 — сбор RSS + базовая БД (PostgreSQL).

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
```

Добавьте `-v` для подробного лога: `python -m oiltech_digest.cli -v discover-rss`.

## Команды

| Команда | Назначение |
|---|---|
| `init-db` | создать схему БД (идемпотентно) |
| `seed-sources` | загрузить 120 источников из `1_Список_источников_для_дайджеста.xlsx` |
| `discover-rss` | автообнаружение RSS по сайту источника; `--force`, `--dry-run`, `--source-id`, `--workers` |
| `parse` | спарсить ленты в `articles`; `--max-age-days`, `--source-id`, `--workers` |
| `stats` | количество источников/статей, распределение по стратегиям, кросс-дубли |

## Структура

```
oiltech_digest/
  config.py          # пути, DATABASE_URL, константы парсера
  db/                # schema.sql, connection, repository
  ingestion/         # excel_seed, rss_discovery, http_client, rss_parser, normalize
  cli.py
docker-compose.yml   # PostgreSQL 16
```
