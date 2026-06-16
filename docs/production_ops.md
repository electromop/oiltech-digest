# Production Ops

Ниже минимальный набор команд для диагностики и обслуживания без сброса данных.

## Readiness и схема

Проверить HTTP-readiness:

```bash
curl -s http://127.0.0.1:8000/api/readiness
```

Проверить обязательные таблицы:

```bash
docker compose run --rm app python -m oiltech_digest.cli schema-check
```

Прогнать read-only benchmark основных запросов:

```bash
docker compose run --rm app python -m oiltech_digest.cli bench-readiness --iterations 5
```

## Фоновые задачи

Вернуть зависшие `running` задачи обратно в `queued`:

```bash
docker compose run --rm app python -m oiltech_digest.cli jobs-requeue-stale
```

Если нужен другой порог:

```bash
docker compose run --rm app python -m oiltech_digest.cli jobs-requeue-stale --stale-minutes 120
```

## Service cleanup

Удалить:
- истекшие `user_sessions`
- старые terminal-записи `background_jobs`
- старые terminal-записи `export_jobs`

По умолчанию retention берется из env:
- `BACKGROUND_JOB_RETENTION_DAYS`
- `EXPORT_JOB_RETENTION_DAYS`

Команда:

```bash
docker compose run --rm app python -m oiltech_digest.cli maintenance-cleanup
```

Явные retention overrides:

```bash
docker compose run --rm app python -m oiltech_digest.cli maintenance-cleanup \
  --background-job-days 30 \
  --export-job-days 14
```

## Автоматический cleanup

В `scheduler` cleanup встроен в цикл.

Управляющие env:

- `RUN_MAINTENANCE_ON_START=1` — прогонять cleanup на первом цикле
- `MAINTENANCE_EVERY_CYCLES=24` — потом повторять каждые N циклов

Пример:

```bash
MAINTENANCE_EVERY_CYCLES=12
```

При `CYCLE_INTERVAL_SECONDS=21600` это будет один cleanup каждые 72 часа.

## Геораспределенный routing

Подготовительный слой для схемы "РФ core + внешний worker" уже есть, но по
умолчанию внешний контур выключен и все задачи остаются в локальных очередях.

Флаги:

- `EXTERNAL_WORKERS_ENABLED=0` — глобально включает/выключает внешний контур
- `AI_EXECUTION_REGION=ru` — `ru` или `external`; при `external` AI-задачи идут в `external-ai`
- `FETCH_EXTERNAL_ENABLED=0` — разрешает отправлять источники с `network_region=external` во внешний fetch/playwright

Пока внешний worker не запущен, не включайте эти флаги на проде, иначе задачи
могут накопиться в `external-*` очередях без исполнителя.

### External Worker API

Внешний worker не подключается к Postgres напрямую. Он забирает задачи через
machine-to-machine API:

- `POST /api/external-worker/claim`
- `POST /api/external-worker/jobs/{id}/progress`
- `POST /api/external-worker/jobs/{id}/heartbeat`
- `POST /api/external-worker/jobs/{id}/complete`
- `POST /api/external-worker/jobs/{id}/fail`

Авторизация: `Authorization: Bearer <token>`.

На core-сервере хранится только SHA-256 hash токена:

```bash
python -c "import hashlib; print(hashlib.sha256(b'your-token-here').hexdigest())"
```

Переменная:

```bash
EXTERNAL_WORKER_TOKEN_HASH=<sha256>
```

Каждая взятая задача получает отдельный `lease_token`. Если worker потерял связь
и lease истек, следующий `claim` вернет задачу обратно в очередь.

### Запуск внешнего worker

На зарубежном сервере нужен только app image и `.env.external-worker`.
Postgres там не поднимается.

Минимальный `.env.external-worker`:

```bash
CORE_API_URL=https://core.example.ru
EXTERNAL_WORKER_TOKEN=<plain-token>
EXTERNAL_WORKER_ID=eu-worker-1
EXTERNAL_WORKER_QUEUES=external-ai,external-fetch,external-playwright
EXTERNAL_WORKER_CAPABILITIES=openai,http_fetch,playwright
OPENAI_API_KEY=sk-...
```

Запуск:

```bash
docker compose -f docker-compose.external-worker.yml up -d --build
```

Для проверки одного прохода без daemon-режима:

```bash
docker compose -f docker-compose.external-worker.yml run --rm external-worker \
  python -m oiltech_digest.cli -v external-worker --once
```

Первый production switch лучше делать постепенно:

1. Включить только `external-ai` и проверить обработку 5-10 статей.
2. Потом вручную перевести один проблемный источник в `network_region=external`.
3. Только после этого включать больше источников и `external-playwright`.

## Логи

CLI и worker используют единый формат:

```text
timestamp level=INFO service=cli logger=... message
```

API дополнительно пишет request-логи с полями:
- `method`
- `path`
- `status`
- `duration_ms`
- `client`

Для управления уровнем логирования:

```bash
LOG_LEVEL=DEBUG
```
