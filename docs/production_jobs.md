# Production Jobs

Тяжелые операции должны запускаться через фоновые задачи, чтобы HTTP API быстро
отвечал и не держал браузер/прокси/AI/PDF внутри одного web-request.

## Текущий слой

Сейчас используется DB-backed очередь:

- состояние хранится в `background_jobs`;
- API сразу возвращает `job.id`;
- результат и ошибка сохраняются в базе;
- готовый файл скачивается отдельным запросом.
- задачи разделены по очередям: `default`, `ai`, `playwright`;
- ошибки ретраятся с exponential backoff до `max_attempts`.

В Docker app работает в режиме `BACKGROUND_JOB_INLINE=0`: он только создает
задачи. Отдельный сервис `worker` запускает:

```bash
python -m oiltech_digest.cli -v jobs-worker
```

и забирает queued-задачи из БД через `FOR UPDATE SKIP LOCKED`.

В `docker-compose.yml` сейчас два worker-контура:

- `worker`: очереди `default,ai`;
- `playwright-worker`: очередь `playwright`.

PDF-экспорт и Playwright scrape попадают в `playwright`; AI processing попадает
в `ai`; прочие легкие задачи остаются в `default`.

Для простой локальной разработки без отдельного worker можно оставить
`BACKGROUND_JOB_INLINE=1` (по умолчанию): тогда API сам выполнит задачу в
локальном thread pool.

Поддержанные задачи:

- `digest_export`;
- `process_articles`;
- `scrape_source`;
- `diagnose_source`.

## API

Поставить экспорт дайджеста в фон:

```bash
curl -X POST http://127.0.0.1:8000/api/jobs/digest-export \
  -H 'Content-Type: application/json' \
  -d '{"month":"2026-06","export_format":"pdf","limit":100,"min_score":0}'
```

Поставить AI-обработку в фон:

```bash
curl -X POST http://127.0.0.1:8000/api/jobs/process \
  -H 'Content-Type: application/json' \
  -d '{"limit":50,"offline":false}'
```

Поставить scrape одного источника в фон:

```bash
curl -X POST 'http://127.0.0.1:8000/api/sources/12/scrape?background=true'
```

Проверить статус:

```bash
curl http://127.0.0.1:8000/api/jobs/123
```

Скачать готовый файл:

```bash
curl -OJ http://127.0.0.1:8000/api/jobs/123/download
```

## Ограничение текущего решения

DB-backed worker уже переживает рестарт API. Если worker погиб посередине
задачи, она может остаться `running`; при старте worker перекидывает старые
`running` задачи обратно в `queued` после `BACKGROUND_JOB_STALE_MINUTES`
(по умолчанию 60 минут).

Если задача упала, она возвращается в `queued` с `run_after` и backoff. После
исчерпания `max_attempts` задача становится `failed`.

Следующий шаг для более жесткого прода:

- Redis + RQ/Celery/Dramatiq;
- per-kind retry policy;
- dead-letter view для failed-задач;
- метрики очередей и алерты по росту `queued`/`failed`.
