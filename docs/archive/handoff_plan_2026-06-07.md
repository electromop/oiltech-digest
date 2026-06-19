# OilTech Digest — handoff на 2026-06-07

Документ фиксирует состояние проекта после блока работ по production reliability,
фоновой очереди задач, Playwright ingestion, тестам и benchmark-инструментам.

---

## 1. Текущее состояние

Проект находится в рабочем состоянии:

- React-админка собрана и отдается FastAPI из `frontend/dist`;
- backend API работает поверх PostgreSQL;
- тяжелые операции вынесены в DB-backed background jobs;
- Docker Compose разделяет `app`, `worker`, `playwright-worker`, `scheduler`, `db`;
- PDF/HTML/DOCX экспорт дайджеста запускается через фоновые задачи;
- Playwright-источники вынесены в отдельную очередь;
- добавлен безопасный read-only benchmark для проверки продовой базы;
- рабочее дерево перед созданием этого handoff было чистым.

Последние важные коммиты:

```text
c00d8bf Add production readiness benchmark CLI
2f5f8a7 Add hidden jobs page and async digest export
f8a5aa1 Add test infrastructure and frontend smoke test
37020c8 Add production background jobs worker
01d522b Add Playwright ingestion strategy
5da64bc Polish digest export layout
63fd3c6 Add updated project handoff for 2026-06-06
0b243b7 Build React admin and polish digest workflows
```

---

## 2. Что сделано с прошлого handoff

### Playwright ingestion

Добавлена стратегия парсинга JS-страниц через Playwright:

- отдельный модуль Playwright-парсера;
- диагностика источников учитывает эту стратегию;
- Playwright-задачи отправляются в отдельную очередь `playwright`;
- в Docker Compose добавлен отдельный `playwright-worker`.

Ключевые файлы:

- `oiltech_digest/ingestion/playwright_parser.py`
- `oiltech_digest/ingestion/source_diagnostics.py`
- `oiltech_digest/background_jobs.py`
- `docker-compose.yml`

### Production background jobs

Добавлена DB-backed очередь `background_jobs`:

- задачи создаются через API;
- worker забирает задачи через `FOR UPDATE SKIP LOCKED`;
- есть retry с exponential backoff;
- stale `running` задачи возвращаются в `queued`;
- очереди разделены на `default`, `ai`, `playwright`;
- результаты и ошибки сохраняются в БД;
- готовые файлы скачиваются через `/api/jobs/{id}/download`.

Поддержанные задачи:

- `digest_export`;
- `process_articles`;
- `scrape_source`;
- `diagnose_source`.

Документация:

- `docs/production_jobs.md`

### Hidden Jobs page

Добавлена скрытая страница мониторинга фоновых задач:

```text
/?screen=jobs
```

Она не показывается в основном меню, но доступна напрямую. Нужна для диагностики
очередей, ошибок и статусов экспортов/обработки.

Ключевые файлы:

- `frontend/src/features/jobs/JobsPage.tsx`
- `frontend/src/api/jobs.ts`
- `frontend/src/app/App.tsx`

### Async digest export

Экспорт дайджеста больше не держит браузер в долгом HTTP-запросе:

- frontend ставит задачу через `/api/jobs/digest-export`;
- UI показывает статус;
- после завершения скачивает файл через `/api/jobs/{id}/download`;
- пустая вкладка при формировании больше не нужна.

Ключевые файлы:

- `frontend/src/features/digest/DigestPage.tsx`
- `frontend/src/api/digest.ts`
- `oiltech_digest/api.py`
- `oiltech_digest/background_jobs.py`

### Тестовая инфраструктура

Добавлены и расширены тесты:

- backend tests через Docker service `test`;
- frontend smoke test через Vitest;
- тесты background jobs;
- тесты async digest export;
- тесты Playwright/request/rss diagnostics;
- тесты benchmark-модуля.

Последний полный backend-прогон:

```text
99 passed, 2 warnings
```

Команды:

```bash
docker compose run --rm test
cd frontend && npm test
cd frontend && npm run build
```

### Production readiness benchmark

Добавлена read-only CLI-команда для проверки скорости ключевых prod-запросов:

```bash
docker compose run --rm app python -m oiltech_digest.cli bench-readiness
```

JSON-вывод:

```bash
docker compose run --rm app python -m oiltech_digest.cli bench-readiness --json
```

Что проверяет:

- `table_counts`;
- `dashboard_stats`;
- `articles_list`;
- `source_health`;
- `digest_candidates`;
- `jobs_list`;
- `queue_summary`.

Важно: команда ничего не пишет в БД, не запускает парсинг и не вызывает AI.

Ключевые файлы:

- `oiltech_digest/benchmarks.py`
- `oiltech_digest/cli.py`
- `tests/test_benchmarks.py`

---

## 3. Текущая архитектура запуска

### Docker Compose services

- `db` — PostgreSQL 16, данные в named volume `pgdata`;
- `bootstrap` — применяет schema и seed-данные;
- `app` — FastAPI + React static;
- `worker` — очереди `default,ai`;
- `playwright-worker` — очередь `playwright`;
- `scheduler` — периодический ingestion/pipeline цикл;
- `test` — тестовый контейнер с mounted source tree;
- `caddy` — HTTPS reverse proxy.

### Важная особенность Docker

`app`, `worker`, `scheduler`, `bootstrap` работают из собранного Docker image.
Исходники туда копируются на этапе build.

После изменения кода нужно пересобрать image:

```bash
docker compose build app worker scheduler bootstrap playwright-worker
docker compose up -d
```

`test` service монтирует `.:/app`, поэтому видит локальные изменения сразу.

---

## 4. Как безопасно обновить прод без потери базы

Данные PostgreSQL лежат в named volume `pgdata`, поэтому обычный rebuild
контейнеров базу не сбрасывает.

Рекомендуемый порядок:

```bash
git pull
docker compose exec -T db pg_dump -U oiltech -d oiltech_digest > backup-$(date +%F-%H%M).sql
docker compose build app worker scheduler bootstrap playwright-worker
docker compose up -d
docker compose ps
```

Проверки после обновления:

```bash
curl http://127.0.0.1:8000/api/health
docker compose logs --tail=100 app
docker compose logs --tail=100 worker
docker compose logs --tail=100 playwright-worker
docker compose run --rm app python -m oiltech_digest.cli bench-readiness --iterations 3
```

Чего не делать без явного намерения удалить данные:

```bash
docker compose down -v
```

---

## 5. Основные рабочие команды

### Инициализация

```bash
docker compose run --rm app python -m oiltech_digest.cli init-db
docker compose run --rm app python -m oiltech_digest.cli seed-sources
docker compose run --rm app python -m oiltech_digest.cli seed-tags
docker compose run --rm app python -m oiltech_digest.cli seed-scoring
```

### Парсинг и обработка

```bash
docker compose run --rm app python -m oiltech_digest.cli parse --workers 10
docker compose run --rm app python -m oiltech_digest.cli fetch-full-text --limit 100
docker compose run --rm app python -m oiltech_digest.cli process-full --limit 20
```

### Фоновый worker вручную

```bash
docker compose run --rm app python -m oiltech_digest.cli -v jobs-worker --once
docker compose run --rm app python -m oiltech_digest.cli -v jobs-worker --queue ai
docker compose run --rm app python -m oiltech_digest.cli -v jobs-worker --queue playwright
```

### Проверка производительности prod-запросов

```bash
docker compose run --rm app python -m oiltech_digest.cli bench-readiness --iterations 5
```

---

## 6. Что осталось в бэклоге

Актуальные активные задачи из `BACKLOG.md`:

1. `P2` — Источники: надежность парсинга
   - проблемные источники;
   - JS-WAF;
   - telegram-дубли;
   - RU-селекторы;
   - stale/no-articles.

2. `P3` — Дайджест: довести экспортный шаблон по референсу
   - первая страница `Главное за период`;
   - далее примерно по 3 статьи на страницу;
   - добить разрывы карточек и выравнивание.

3. `P3` — Теги: переработать редактор
   - идея запаркована;
   - вероятное направление: дерево / master-detail вместо плоской inline-формы.

---

## 7. Рекомендуемый следующий технический шаг

С учетом текущего состояния и объема продовой базы следующий шаг лучше делать
не в UI, а в надежности эксплуатации:

1. Прогнать на проде:

```bash
docker compose run --rm app python -m oiltech_digest.cli bench-readiness --iterations 5 --articles-limit 1000
```

2. По результату добавить индексы под реальные медленные запросы:

- список сигналов;
- digest candidates;
- filters по tag/status/source;
- jobs list;
- queue claim.

3. Добавить отдельный `/api/readiness`, который проверяет:

- доступность БД;
- наличие ключевых таблиц/колонок;
- размер очередей;
- stale running jobs;
- возможность выполнить lightweight query.

4. Добавить retention для jobs/export файлов:

- удаление старых `ok/failed` jobs;
- удаление старых PDF/DOCX/HTML exports;
- отдельная CLI-команда и/или scheduler step.

---

## 8. Риски и замечания

- `app` image нужно пересобирать после изменения кода, иначе контейнер будет
  показывать старый набор CLI-команд и старую статику.
- Background jobs сейчас DB-backed. Для текущего масштаба это нормально, но при
  росте нагрузки можно переходить на Redis/RQ/Celery/Dramatiq.
- Playwright тяжелый по памяти. На слабом VPS лучше держать отдельный
  `playwright-worker` и ограничивать конкуренцию.
- `bench-readiness` read-only, но на очень большой базе все равно создает
  нагрузку SELECT-запросами. Для первого запуска достаточно `--iterations 3`.
- База не сбрасывается при `docker compose up -d --build`, но сбрасывается при
  удалении volume через `docker compose down -v`.

---

## 9. Быстрый smoke после деплоя

```bash
docker compose ps
curl http://127.0.0.1:8000/api/health
docker compose run --rm app python -m oiltech_digest.cli stats
docker compose run --rm app python -m oiltech_digest.cli bench-readiness --iterations 3
```

В браузере:

- `/` — основная админка;
- `/?screen=jobs` — скрытая страница фоновых задач.

