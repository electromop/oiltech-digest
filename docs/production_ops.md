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
