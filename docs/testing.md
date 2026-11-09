# Тестирование OilTech Digest

Как проверять проект: автоматические тесты бэкенда и фронта, затем проверки на собранной системе. Устройство
системы: [architecture.md](architecture.md), запуск: [project_guide.md](project_guide.md).

## 1. Тесты бэкенда (pytest)

Нужны Python 3.13 (как в образе), PostgreSQL 16 и зависимости разработки:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium   # для тестов печати PDF и браузерного сбора
```

Без Chromium тесты, которым он нужен, пропускаются, остальные идут.

**База.** Тесты берут строку подключения из `DATABASE_URL` (окружение или `.env` в корне); без неё `config.py`
смотрит на локальный Postgres, базу `oiltech_digest` на порту 5432. Проще всего база из compose:

```bash
cp .env.example .env      # пароль базы и DATABASE_URL в нём согласованы
docker compose up -d db   # слушает localhost:5432
```

Запуск:

```bash
python -m pytest -q                        # весь набор
python -m pytest -q tests/test_lanes.py    # один файл
python -m pytest -q -k feed_window         # по имени теста
```

**Изоляция.** Фикстура `isolated_db` ([`tests/conftest.py`](../tests/conftest.py)) заводит каждому тесту свою схему
`test_<uuid>` в той же базе, прогоняет в ней `init_db()` и подменяет `get_connection` у модулей `connection`,
`repository`, `api`, `source_overrides` и `documents_repo`; после теста схема удаляется. Рабочие таблицы базы тесты не
трогают, но учётке базы нужно право создавать схемы. Новый модуль, который берёт `get_connection` прямым импортом,
нужно добавить в список подмен, иначе его тест пойдёт в настоящую схему.

**Сторож часов базы.** Фикстура `db_clock_steady` один раз на прогон, перед первым тестом с базой, около секунды
сравнивает часы базы с часами машины ([`tests/db_clock_support.py`](../tests/db_clock_support.py)). Уходят часы базы
больше чем на 5 мс в секунду или прыгают назад: прогон останавливается с объяснением. Причина строгости: выдача задач
сравнивает `run_after` с `now()` базы, и при прыгающих часах тесты плавают (`claim` возвращает `None`). Обычный
виновник: база в виртуальной машине, где спорят две службы синхронизации времени. Запустить всё равно:
`OILTECH_TEST_ALLOW_CLOCK_DRIFT=1`.

**Настройки тестов.** `conftest.py` задаёт свои значения, чтобы прогон не зависел от `.env` разработчика:
`AUTH_COOKIE_SECURE=False` (TestClient ходит по http), `FEED_ROLLOVER_DAY=5`, подставные модели поиска радара и
«Дедупа», радар раз в сутки. Ключ OpenAI тестам не нужен: модель заменяет офлайн-клиент или подставной ответ.

Тот же набор в Docker: `docker compose run --rm test` (профиль `test`: образ с dev-зависимостями, рабочая копия
смонтирована в `/app`, база из сервиса `db`).

## 2. Фронт

```bash
cd frontend
npm ci
npx tsc -b       # проверка типов; в сборке образа её нет
npx vitest run   # тесты экранов в jsdom; npm test запускает то же без параллельных файлов
npx vite build   # сборка в frontend/dist, её отдаёт app на /
```

`npm run build` делает `tsc -b` и `vite build` подряд.

## 3. Быстрые проверки

```bash
python -m compileall -q oiltech_digest
git diff --check
docker compose config -q
bash -n scripts/*.sh
```

## 4. Проверка на собранной системе

Команды ниже идут из корня репозитория; в Docker перед ними `docker compose exec app`.

### База и сиды, без OpenAI

```bash
python -m oiltech_digest.cli init-db
python -m oiltech_digest.cli seed-sources
python -m oiltech_digest.cli seed-tags
python -m oiltech_digest.cli seed-scoring
python -m oiltech_digest.cli schema-check
python -m oiltech_digest.cli stats
curl http://localhost:8000/api/health
```

Ожидаемо: схема создаётся без ошибок, `schema-check` пишет `ok`, `stats` показывает источники.

### Сбор

```bash
python -m oiltech_digest.cli discover-rss --workers 10 --timeout 4
python -m oiltech_digest.cli parse --workers 10
python -m oiltech_digest.cli fetch-full-text --limit 50 --min-chars 800
python -m oiltech_digest.cli stats
```

Проверить: у части источников появился `rss_url`; статьи ложатся в `articles`, а повторный `parse` даёт в основном
дубли; в выводе `parse` есть «отсеяно как шум». `fetch-full-text` раскладывает статьи по статусам: `ok`, `too_short`,
`no_gain` (извлечённый текст не вдвое длиннее сохранённого: перезапись не нужна, это не сбой) и `mismatch` (страж
принадлежности: прежний текст не затёрт).

### ИИ

Агентам («Отбор», «Карточка», её баллы, «Суть и перевод» сигналов) нужна настоящая модель: офлайн-режима у них нет.
Новая статья ждёт решения «Отбора», и до него суть и перевод её не берут. Без затрат проверяется только обработка уже
принятых статей:

```bash
python -m oiltech_digest.cli process --offline --limit 5
python -m oiltech_digest.cli ai-cost-report
python -m oiltech_digest.cli ai-article-cost-report
```

С ключом и моделями агентов (`OTBOR_MODEL`, `SIGNAL_CARD_MODEL`, `SIGNAL_SCORES_MODEL`) шаги ставятся командами
`enqueue-otbor`, `enqueue-signal-summary`, `enqueue-signal-cards`, `enqueue-signal-scores`, `enqueue-process`. Без
внешнего контура задачи встают в местную очередь `ai`; их исполняет `jobs-worker` (сервис `worker`), а вне compose
при `BACKGROUND_JOB_INLINE=1` (умолчание `config.py`) сам процесс команды. Проверить вручную:
суть по-русски и без выдумок, русский заголовок не длиннее 90 знаков, без тире и названия издания, у сигнала тема из
13 тематик и баллы, а в `ai_processing_runs` строка на каждый вызов. Отклонённые «Отбором» и ждущие его статьи
`process` не берёт вовсе.

Радар проверяется только через очередь и внешний контур: с адреса РФ OpenAI отвечает 403.

```bash
python -m oiltech_digest.cli enqueue-signal-discovery --topic <тема> --no-offline --dry-run
python -m oiltech_digest.cli external-queues-status
python -m oiltech_digest.cli check-lanes
```

С `--dry-run` поиск оплачивается, но ничего не пишется. Пробный прогон с оценкой цены до оплаты:
`radar-search-trial` (без `--run` сухой).

### Терминология

```bash
python -m oiltech_digest.cli validate-terminology
python -m oiltech_digest.cli audit-terminology --limit 1000 --show 50
python -m oiltech_digest.cli repair-terminology --dry-run --limit 1000 --show 50
```

Применить исправления после просмотра сухого прогона: `repair-terminology --no-dry-run --limit 1000`.

### Выпуск

```bash
python -m oiltech_digest.cli digest-content 2026-11 --output digest.json --html-output digest.html
```

Проверить: в JSON есть `issue`, `news`, `sections`, `cover`, `document`, `footer`; HTML открывается как письмо; у
карточек заголовок, источник, суть и ссылка. PDF и выгрузку удобнее проверить на экране «Месячный дайджест»:
предпросмотр PDF, «Обновить PDF», «Скачать PDF», DOCX и HTML. Для PDF нужен Chromium.

### Интерфейс

После `npx vite build` и запуска `app` (локально нужен `AUTH_COOKIE_SECURE=0`) пройти руками:

- вход: пароль, затем код допуска из `admission-code --email <почта>`;
- «Бизнес-лента» и «Технологический радар»: карточки, фильтры, отметка «в дайджест», обратная связь;
- «Месячный дайджест»: выборка месяца, предпросмотр PDF, правка материала, выгрузка;
- «Материалы»: материалы бота и загрузка документа (загрузка требует внешнего контура: `scripts/run-local.sh`);
- «Источники», «Скоринг» (сумма весов профиля 100, сохранение), «Теги», «Дочерние общества», «Описание Компании»;
- под администратором «Пользователи» и «Статистика».

## 5. Минимальный чек готовности

- pytest зелёный, `npx tsc -b`, `npx vitest run` и `npx vite build` проходят;
- `docker compose config -q` без ошибок, образ собирается;
- `init-db`, сиды, `discover-rss`, `parse`, `fetch-full-text` работают;
- на тестовом наборе статей «Отбор», суть и перевод, «Карточка» и баллы проходят без системных ошибок;
- интерфейс открывается и читает реальные данные, `digest-content` собирается, PDF выгружается;
- `check-lanes` без тревог.
