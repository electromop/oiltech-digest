# Команды для консоли — поднять прод и выкатить приём файлов

Ветка `feat/upload-docs` запушена в `origin`, коммит `1c6ef81`.
Выкатываем **ветку**, а не main: `main` остаётся точкой отката.

---

## Шаг 0. Выключить VPN

Без этого SSH не идёт ни до СПб, ни до Амстердама. Проверка:

```bash
ssh -o ConnectTimeout=10 root@109.68.213.12 'hostname && uptime'
```

Ответил — идём дальше. Молчит — VPN ещё активен либо маршрут не восстановился.

---

## Шаг 1. Понять, почему прод не слушает 443

Со стороны видно `ECONNREFUSED` на 443: хост жив, веб-стек не работает. Смотрим, что с контейнерами:

```bash
ssh root@109.68.213.12 'docker ps -a --format "{{.Names}}\t{{.Status}}" && echo "--- ПАМЯТЬ ---" && free -m | head -2 && echo "--- ДИСК ---" && df -h / | tail -1 && echo "--- ПОСЛЕДНЯЯ ЗАГРУЗКА ---" && uptime -s'
```

Что искать в выводе:
- контейнеров нет вовсе → docker не поднялся после перезагрузки;
- `oiltech_caddy` в `Exited` → упал только вход, остальное живо;
- `Exited (137)` у любого → убит по памяти (OOM), это ожидаемый риск при 1,9 ГБ;
- диск под 100 % → всё встало из-за места.

Логи входа и приложения:

```bash
ssh root@109.68.213.12 'cd /root/oiltech-digest && docker compose logs --tail=40 caddy app 2>&1 | tail -60'
```

---

## Шаг 2. Поднять то, что упало

Если контейнеры просто не запущены:

```bash
ssh root@109.68.213.12 'cd /root/oiltech-digest && docker compose up -d && sleep 20 && docker ps --format "{{.Names}}\t{{.Status}}"'
```

Проверка снаружи:

```bash
curl -s -o /dev/null -w "health: %{http_code}\n" https://oiltech-digest.ru/api/health
```

**Дальше не идти, пока не будет 200.** Выкатывать на сломанный хост нельзя: не отличишь новую поломку от старой.

---

## Шаг 3. Дамп базы до выката

Бэкапов по расписанию нет — это единственная страховка.

```bash
ssh root@109.68.213.12 'cd /root/oiltech-digest && docker compose exec -T oiltech_pg pg_dump -U oiltech oiltech_digest | gzip > /root/before-upload-docs-$(date +%F-%H%M).sql.gz && ls -lh /root/before-upload-docs-*.sql.gz | tail -1'
```

---

## Шаг 4. Убедиться, что нет активных ИИ-задач

Рестарт рвёт lease внешнего воркера — 24.07 на этом сожгли ~$11/час в петле.

```bash
ssh root@109.68.213.12 'cd /root/oiltech-digest && docker compose exec -T oiltech_pg psql -U oiltech -d oiltech_digest -c "SELECT id, kind, status FROM background_jobs WHERE status IN (\047running\047,\047finalizing\047);"'
```

Пусто — идём дальше. Есть строки — подождать, пока завершатся.

---

## Шаг 5. Выкат РФ-ядра

```bash
ssh root@109.68.213.12 'cd /root/oiltech-digest && git fetch origin && git reset --hard origin/feat/upload-docs && git log --oneline -1'
```

Сборка по одному сервису — на 1,9 ГБ всё сразу рискует уйти в OOM:

```bash
ssh root@109.68.213.12 'cd /root/oiltech-digest && docker compose build app'
```

```bash
ssh root@109.68.213.12 'cd /root/oiltech-digest && docker compose build worker scheduler tasks'
```

Схема — новые таблицы документов создаются идемпотентно:

```bash
ssh root@109.68.213.12 'cd /root/oiltech-digest && docker compose run --rm app python -m oiltech_digest.cli init-db && docker compose run --rm app python -m oiltech_digest.cli schema-check'
```

Поднять:

```bash
ssh root@109.68.213.12 'cd /root/oiltech-digest && docker compose up -d app worker scheduler tasks caddy && sleep 20 && curl -s -o /dev/null -w "health: %{http_code}\n" http://127.0.0.1/api/health'
```

---

## Шаг 6. Внешний воркер в Амстердаме

Сначала переменная модели — **без неё стадия разбора падает с явной ошибкой, это сделано нарочно**:

```bash
ssh root@85.234.107.233 'cd /root/oiltech-digest && grep -q "^OPENAI_DOC_MODEL=" .env.external-worker || printf "OPENAI_DOC_MODEL=gpt-5.4-mini\nOPENAI_DOC_REASONING=medium\n" >> .env.external-worker; grep -E "^OPENAI_DOC" .env.external-worker'
```

Код и пересборка:

```bash
ssh root@85.234.107.233 'cd /root/oiltech-digest && git fetch origin && git reset --hard origin/feat/upload-docs && docker compose -f docker-compose.external-worker.yml up -d --build && sleep 10 && docker compose -f docker-compose.external-worker.yml logs --tail=20 external-worker'
```

---

## Шаг 7. Дымовая проверка

1. Открыть https://oiltech-digest.ru, войти админом.
2. Экран **«Материалы»** в меню слева.
3. Загрузить документ (PDF, PPTX или DOCX до 25 МБ), поставить галочку подтверждения.
4. Ждать: 13 слайдов — около 15 секунд, 100 страниц — до трёх минут.
5. Кликнуть по имени файла — карточка: паспорт, суть, сводка, таблица чисел со ссылкой на страницу и колонкой сверки.

Что задача взялась и завершилась:

```bash
ssh root@109.68.213.12 'cd /root/oiltech-digest && docker compose exec -T oiltech_pg psql -U oiltech -d oiltech_digest -c "SELECT id, kind, status, progress FROM background_jobs WHERE kind = \047process_document\047 ORDER BY id DESC LIMIT 5;"'
```

Сколько стоил разбор:

```bash
ssh root@109.68.213.12 'cd /root/oiltech-digest && docker compose exec -T oiltech_pg psql -U oiltech -d oiltech_digest -c "SELECT document_id, stage, model, total_tokens, cost_usd FROM ai_processing_runs WHERE document_id IS NOT NULL ORDER BY id DESC LIMIT 10;"'
```

---

## Откат

Приём файлов гасится **без выката кода**:

```bash
ssh root@109.68.213.12 'cd /root/oiltech-digest && grep -q "^UPLOAD_DOCS_ENABLED=" .env && sed -i "s/^UPLOAD_DOCS_ENABLED=.*/UPLOAD_DOCS_ENABLED=false/" .env || echo "UPLOAD_DOCS_ENABLED=false" >> .env; docker compose up -d app && echo "приём файлов выключен"'
```

Полный откат кода на прежнее состояние:

```bash
ssh root@109.68.213.12 'cd /root/oiltech-digest && git reset --hard origin/main && docker compose build app worker scheduler tasks && docker compose up -d && curl -s -o /dev/null -w "health: %{http_code}\n" http://127.0.0.1/api/health'
```

Таблицы документов и том при откате остаются — они ничему не мешают.

---

## Если что-то пошло не так — что прислать

```bash
ssh root@109.68.213.12 'cd /root/oiltech-digest && docker ps -a --format "{{.Names}}\t{{.Status}}" && free -m | head -2 && docker compose logs --tail=40 app 2>&1 | tail -50'
```
