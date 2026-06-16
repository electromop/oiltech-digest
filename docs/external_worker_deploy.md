# External Worker Deploy

Runbook для схемы:

- РФ core: frontend, FastAPI, Postgres, scheduler, локальные workers.
- External worker: зарубежный сервер без Postgres, выполняет `external-*` задачи через HTTP API core.

## 1. Перед обновлением core

Сделать backup БД на РФ-сервере:

```bash
docker compose exec db pg_dump -U oiltech -d oiltech_digest > backup-before-external-worker.sql
```

Обновить код и применить идемпотентную схему:

```bash
git pull
docker compose build app worker playwright-worker scheduler
docker compose run --rm app python -m oiltech_digest.cli init-db
docker compose run --rm app python -m oiltech_digest.cli schema-check
```

Проверить core:

```bash
curl -s http://127.0.0.1:8000/api/readiness
docker compose run --rm app python -m oiltech_digest.cli external-queues-status
```

## 2. Machine token

Сгенерировать token и hash локально или на защищенной машине:

```bash
python -c "import secrets, hashlib; t=secrets.token_urlsafe(32); print('TOKEN=' + t); print('HASH=' + hashlib.sha256(t.encode()).hexdigest())"
```

На РФ core в `.env`:

```bash
EXTERNAL_WORKERS_ENABLED=1
EXTERNAL_WORKER_TOKEN_HASH=<HASH>
AI_EXECUTION_REGION=ru
FETCH_EXTERNAL_ENABLED=0
```

На первом этапе `AI_EXECUTION_REGION=ru`, чтобы включить API без переключения нагрузки.

Перезапустить core:

```bash
docker compose up -d --build app worker playwright-worker scheduler
```

## 3. Зарубежный сервер

Положить код и `.env.external-worker`.

Минимальный `.env.external-worker`:

```bash
CORE_API_URL=https://core.example.ru
EXTERNAL_WORKER_TOKEN=<TOKEN>
EXTERNAL_WORKER_ID=eu-worker-1
EXTERNAL_WORKER_QUEUES=external-ai
EXTERNAL_WORKER_CAPABILITIES=openai
OPENAI_API_KEY=sk-...
```

Запуск одного прохода:

```bash
docker compose -f docker-compose.external-worker.yml run --rm external-worker \
  python -m oiltech_digest.cli -v external-worker --once
```

Daemon:

```bash
docker compose -f docker-compose.external-worker.yml up -d --build
docker compose -f docker-compose.external-worker.yml logs -f external-worker
```

## 4. Переключить OpenAI во внешний контур

На РФ core:

```bash
AI_EXECUTION_REGION=external
EXTERNAL_WORKERS_ENABLED=1
```

Перезапустить API/worker/scheduler:

```bash
docker compose up -d --build app worker scheduler
```

Smoke:

```bash
docker compose run --rm app python -m oiltech_digest.cli external-queues-status
```

Создать AI job через UI или API. Проверить:

- job появляется в `external-ai`;
- external worker забирает job;
- после complete у статьи появились summary/relevance/tag/score;
- при остановленном external worker UI core продолжает открываться.

## 5. Вынести проблемный источник

Сначала один источник вручную:

```bash
curl -X PATCH https://core.example.ru/api/sources/<id> \
  -H 'Content-Type: application/json' \
  -d '{"network_region":"external","network_profile":"direct"}'
```

На РФ core:

```bash
FETCH_EXTERNAL_ENABLED=1
```

Для browser/WAF источника:

```bash
curl -X PATCH https://core.example.ru/api/sources/<id> \
  -H 'Content-Type: application/json' \
  -d '{"network_region":"external","network_profile":"browser"}'
```

На external worker:

```bash
EXTERNAL_WORKER_QUEUES=external-ai,external-fetch,external-playwright
EXTERNAL_WORKER_CAPABILITIES=openai,http_fetch,playwright
```

## 6. Rollback

Отключить external routing на РФ core:

```bash
AI_EXECUTION_REGION=ru
FETCH_EXTERNAL_ENABLED=0
EXTERNAL_WORKERS_ENABLED=0
```

Перезапустить:

```bash
docker compose up -d --build app worker playwright-worker scheduler
```

Если в `external-*` остались задачи, они не удаляются. Их можно оставить до восстановления external worker или переиграть вручную после анализа.

Проверка:

```bash
docker compose run --rm app python -m oiltech_digest.cli external-queues-status
docker compose run --rm app python -m oiltech_digest.cli schema-check
```

## 7. Что мониторить

- hidden page `?screen=maintenance`;
- hidden page `?screen=jobs`;
- `external-queues-status`;
- oldest queued external job;
- failed external jobs;
- expired leases;
- last heartbeat.

