# OilTech Digest: гид по проекту

Для того, кто впервые открывает проект: что работает, как данные идут по системе, где код и как запустить основные
сценарии. Подробное устройство: [architecture.md](architecture.md), проверка: [testing.md](testing.md).

## 1. Что делает проект

OilTech Digest собирает публикации о нефтесервисе, решает агентами, какие из них брать, и даёт людям разобрать их и
собрать месячный выпуск для заказчика. Части системы:

1. сбор: каталог источников, RSS, листинги сайтов, браузер, открытые каналы Telegram, полный текст статьи;
2. технологический радар: поиск материалов встроенным поиском модели по расписанию;
3. бот Telegram «Материалы коллег»: сотрудники вносят ссылки, файлы и события ([tg_materials_bot.md](tg_materials_bot.md));
4. агенты: «Отбор» (брать или нет), «Суть и перевод», «Карточка» (тема, атрибуты, баллы), «Дедуп»;
5. интерфейс на React и API на FastAPI: бизнес-лента, радар, выпуск, материалы, настройки;
6. выпуск месяца: PDF в формате эксперта заказчика, письмо, DOCX.

## 2. Как движутся данные

```text
источники
  → discover-rss, parse (rss, request, playwright, telegram, linkedin через NL)
  → предфильтр шума, до записи в базу
  → articles
  → fetch-full-text
  → «Отбор»: брать или нет                ← находки радара, материалы коллег
  → суть и русский заголовок
  → сигнал: «Карточка» (тема, атрибуты), затем баллы по профилю категории
  → бизнес-лента и технологический радар на экране
  → отметка «в дайджест» → выпуск месяца
```

Фильтров два, и они разные:

- `ingestion/relevance_filter.py`: дешёвый детерминированный отсев явного мусора ещё до записи статьи;
- агент «Отбор»: смысловое решение «брать или нет» до сути и перевода. Отклонённое дальше не обрабатывается.

## 3. Где код

### `oiltech_digest/`

- `config.py`: переменные окружения и умолчания; `cli.py`: все команды; `api.py`: HTTP API и отдача интерфейса;
- `background_jobs.py`, `external_worker.py`, `lanes.py`, `network_policy.py`, `contract.py`: очередь задач,
  внешний воркер, полосы, маршрут «на месте или наружу», договор ядра и NL;
- `otbor*.py`, `signal_card*.py`, `signal_summary.py`, `signal_scores.py`, `signal_scoring.py`, `card_return.py`:
  агенты «Отбор», «Карточка» и «Суть и перевод» сигналов, их пакеты эксперту и экзамены (`*_pack*.py`, `*_exam*.py`);
- `signal_discovery.py`, `signal_research.py`, `signal_dedup.py`, `signal_search_trial.py`: радар (прогон, поиск,
  «Дедуп», пробный прогон);
- `signal_feedback.py`, `signal_training.py`, `signal_reference.py`: обратная связь, выгрузки, эталон заказчика;
- `colleague_materials.py`, `material_signals.py`, `tg_materials_bot.py`, `tg_materials_notices.py`: материалы
  коллег и бот;
- `auth.py`, `admission.py`: вход и допуск по коду; `brand_fonts.py`: шрифт бренда с сервера; `feed_window.py`:
  окно месяца ленты;
- `db/`: `schema.sql` (вся схема), `connection.py` (`init_db()`), `repository.py` (основные запросы) и хранилища
  этапов (`otbor_store.py`, `signal_card_store.py`, `signal_scores_store.py`, `feed_signals.py`, `marks.py`,
  `issue_edits_store.py` и соседние);
- `ingestion/`: сбор (`rss_discovery.py`, `rss_parser.py`, `request_parser.py`, `playwright_parser.py`,
  `telegram_parser.py`, `external_fetch.py`), полный текст (`article_fetcher.py`, `article_tails.py`), предфильтр
  (`relevance_filter.py`), диагностика источников (`source_diagnostics.py`, `source_probe.py`), страницы находок
  радара (`publisher_page.py`, `core_page.py`), сид каталога (`excel_seed.py`);
- `processing/`: клиент OpenAI и офлайн-заглушка (`openai_client.py`), суть и перевод (`pipeline.py`, `prompts.py`,
  `external_ai.py`), перепечатки (`reprints.py`), выпуск (`digest.py`, `digest_materials.py`, `digest_pdf.py`,
  `digest_email_template.html`), словарь терминов (`domain_glossary.py`), сиды тематик и критериев (`seed.py`);
- `documents/`: разбор загруженных документов.

### `frontend/`

React и TypeScript, сборка Vite в `frontend/dist`; её отдаёт `app` на `/`. Экраны в `frontend/src/features/`,
запросы к API в `frontend/src/api/`, стили и роли цветов в `frontend/src/styles/` (`tokens.css`).

### `scripts/`

- `docker-scheduler.sh`: цикл планировщика;
- `deploy-core.sh`, `deploy-nl.sh`: выкат ядра и воркеров NL ([architecture.md](architecture.md), раздел 10);
- `run-local.sh`: местный запуск API на порту 8011 с включённым внешним контуром (проверка приёма файлов);
- `manual-add-article.sh`: добавить статью по ссылке и поставить её в обработку.

### Остальное

- `tests/`: тесты pytest; `data/seed/`: каталог источников и направления с ключевыми словами (Excel), 13 тематик
  заказчика; `docs/`: документация и ADR.

## 4. Запуск

### Docker целиком

```bash
cp .env.example .env    # заполнить: пароль базы, ключ OpenAI, модели агентов
docker compose up -d --build app                                  # db, bootstrap (схема и сиды), app
docker compose up -d --build worker playwright-worker scheduler   # конвейер, профиль pipeline
```

Интерфейс открывается на `http://localhost:8000`. Вход по http требует `AUTH_COOKIE_SECURE=0` в `.env`. Первая
учётка и её первый вход:

```bash
docker compose exec app python -m oiltech_digest.cli create-user --email <почта> --password <пароль> --admin
docker compose exec app python -m oiltech_digest.cli admission-code --email <почта>   # код для экрана входа
```

Без внешнего контура (`EXTERNAL_WORKERS_ENABLED=0`, так по умолчанию) задачи ИИ встают в местную очередь `ai`, их
исполняет `worker`. Этапу без своей модели (`OTBOR_MODEL`, `SIGNAL_CARD_MODEL`, `SIGNAL_SCORES_MODEL`,
`SIGNAL_RESEARCH_MODEL`) шаг планировщика отказывает с причиной в логе. Сервис `caddy` нужен только на сервере.

### Локально по шагам

```bash
cp .env.example .env
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
docker compose up -d db
python -m oiltech_digest.cli init-db
python -m oiltech_digest.cli seed-sources
python -m oiltech_digest.cli seed-tags
python -m oiltech_digest.cli seed-scoring
(cd frontend && npm ci && npx vite build)
uvicorn oiltech_digest.api:app --reload --host localhost --port 8000
```

`DATABASE_URL` из `.env` должен сходиться с логином и паролем базы в compose (в `.env.example` они согласованы).
Без собранного фронта `/` отвечает 404 «Интерфейс не собран».

## 5. Что делает планировщик

[`scripts/docker-scheduler.sh`](../scripts/docker-scheduler.sh), подробно в [architecture.md](architecture.md),
раздел 7. Без `SKIP_BOOTSTRAP=1` при старте: `init-db`, `seed-sources`, `seed-tags`, `seed-scoring`,
`seed-signal-topics`, `apply-source-overrides` (в compose это делает `bootstrap`). Дальше в цикле: обслуживание,
поиск RSS, сбор, полный текст, суть и перевод, шаги агентов («Отбор», «Суть и перевод» сигналов, «Карточка», баллы),
сбор через NL, перепечатки, радар по расписанию, сторож полос `check-lanes`, `stats`. Затем пауза
`CYCLE_INTERVAL_SECONDS`.

Главные переменные: `CYCLE_INTERVAL_SECONDS`, `DISCOVER_EVERY_CYCLES`, `PARSE_WORKERS`, `FULL_TEXT_LIMIT`,
`AI_PROCESS_LIMIT`, `AI_OFFLINE`, `FETCH_EXTERNAL_ENABLED`, `REPRINTS_INTERVAL_HOURS`, `SIGNAL_CARD_ENABLED`,
`SIGNAL_DISCOVERY_DAILY_ENABLED`, `STEP_TIMEOUT_SECONDS`.

## 6. База простыми словами

- `sources`: каталог источников (стратегия сбора, лента, селекторы, сетевой регион, дата начала сбора);
- `articles`: статьи с текстом; `article_cards`: суть и русский заголовок статьи;
- `signals`: карточка события, будь то статья ленты, находка радара или материал коллеги; на ней решение «Отбора»,
  тема, атрибуты и баллы; `signal_evidence`: её публикации;
- `article_marks`, `signal_marks`: общие отметки («в дайджест», шум, дубль, архив);
- `monthly_digests`, `monthly_digest_items`: общий черновик выпуска месяца;
- `tags`, `scoring_criteria`: тематики и критерии скоринга по профилям;
- `ai_processing_runs`: токены, стоимость и статус каждого вызова модели; `background_jobs`: очередь задач.

Теги и баллы статей (`article_tags`, `article_scores`) хранят историю до 08.11: с тех пор тему и баллы ставит
«Карточка» у сигнала. Полный перечень таблиц: [architecture.md](architecture.md), раздел 5.

## 7. Команды CLI по смыслу

Запуск: `python -m oiltech_digest.cli <команда>`, справка по команде: `<команда> --help`. Многие команды, которые
меняют данные задним числом, по умолчанию идут сухим прогоном, но не все (`retire-tags` и `recheck-unmark` пишут
сразу): перед запуском смотреть справку.

- База и учётки: `init-db`, `schema-check`, `seed-sources`, `seed-tags`, `seed-scoring`,
  `apply-scoring-preset`, `seed-signal-topics`, `create-user`, `set-role`, `list-users`, `admission-code`.
- Сбор: `discover-rss`, `parse`, `fetch-full-text`, `parse-process`, `backfill-images`,
  `enqueue-external-scrape`, `enqueue-external-refetch`, `source-retry`, `stats`.
- Источники: `sources`, `source-health`, `source-quality`, `source-enable`, `source-add-rss`,
  `set-source-region`, `add-linkedin-source`, `apply-source-overrides`, `source-audit`, `source-diagnose`, `source-probe`,
  `source-dump-listing`.
- Суть и перевод ленты: `process`, `process-full`, `process-articles`, `enqueue-process`, `summarize`,
  `translate-titles`, `enqueue-translate`, `enqueue-resummarize`, `enqueue-title-edit`.
- Агенты: `enqueue-otbor`, `enqueue-signal-summary`, `enqueue-signal-cards`, `enqueue-signal-scores`,
  `enqueue-signal-card-history`, `otbor-materials`, `sync-feed-signals`.
- Радар: `enqueue-daily-signal-discovery`, `enqueue-signal-discovery`, `radar-search-trial`, `signals`,
  `radar-weekly-report`, `radar-recall`, `import-reference-signals`, `archive-signals`, `unarchive-signals`,
  `unmerge-signal`, `refresh-signal-evidence-counts`.
- «Дедуп» ленты: `find-reprints`, `reprints`.
- Экзамены агентов: `otbor-expert-pack`, `otbor-markup-check`, `otbor-exam`, `otbor-exam-control`,
  `card-scores-exam`, `card-scores-exam-control`, `signal-card-expert-pack`, `signal-card-exam`,
  `signal-card-exam-judge`.
- Обратная связь и выгрузки: `signal-feedback`, `import-signal-feedback`, `article-signal-feedback`,
  `export-signal-training-jsonl`, `export-signal-context`, `import-signal-context`.
- Выпуск: `digest-content`, `digest-save`.
- Терминология: `validate-terminology`, `audit-terminology`, `repair-terminology`, `eval-terminology`.
- Ремонт данных: `repair-article-bodies`, `repair-article-dates`, `repair-url-keys`, `repair-telegram-titles`,
  `cleanup-future-dates`, `recheck-marked`, `recheck-purge`, `recheck-unmark`, `retire-tags`,
  `migrate-shared-marks`, `article-candidates`.
- Переключение на «Отбор» (история 04.11): `otbor-shadow`, `otbor-shadow-report`, `otbor-switch`.
- Процессы и обслуживание: `jobs-worker`, `external-worker`, `tg-materials-bot`, `jobs-requeue-stale`,
  `external-queues-status`, `check-lanes`, `live-ai-leases`, `worker-versions`, `scheduler-lock`,
  `maintenance-cleanup`, `bench-readiness`, `ai-cost-report`, `ai-article-cost-report`.

## 8. API

Все адреса в [`api.py`](../oiltech_digest/api.py); роли в [architecture.md](architecture.md), раздел 6.

- Вход: `POST /api/auth/login`, `POST /api/auth/admit` (код допуска), `POST /api/auth/logout`,
  `GET /api/auth/me`; учётки: `/api/users`.
- Служебное: `GET /api/health`, `GET /api/readiness`, `/api/jobs`, `/api/maintenance/*`.
- Лента и сигналы: `GET /api/articles`, `PATCH /api/articles/{id}`, `GET /api/feed-window`,
  `GET /api/signals` (витрины бизнес-ленты и радара, фильтры), `GET /api/signals/summary`,
  `GET /api/signals/dictionaries`, `PATCH /api/signals/{id}` (отметки), `POST /api/signals/feedback`,
  `POST /api/signals/{id}/return` (вернуть отклонённое, админ), `/api/feedback`.
- Выпуск: `GET /api/digest-content`, `GET /api/digest-preview.pdf`, `GET /api/digest-email`,
  `POST /api/jobs/digest-export`, `/api/monthly-digests`, `PUT /api/issue-highlights/{month}`,
  `PATCH /api/signals/{id}/issue-material`, `/api/digest-branding`.
- Материалы и документы: `/api/materials`, `/api/documents`.
- Настройки: `/api/sources` (с `scrape`, `diagnose`, `archive`), `GET /api/source-health`, `/api/tags`,
  `/api/scoring-criteria`, `/api/company-parents`, `/api/company-description`.
- Отчёты: `/api/reports/ai-cost`, `/api/reports/ai-article-cost`, `/api/stats/monthly`, `/api/analytics/monthly`.
- Внешние воркеры и бот: `/api/external-worker/*`, только с токеном воркера.

## 9. Что видит пользователь

Меню в три группы (`frontend/src/app/App.tsx`):

- Работа: «Бизнес-лента» (бизнес-сигналы карточками событий, публикации снизу, атрибуты, фильтры и «всё про
  компанию»), «Технологический радар», «Месячный дайджест» (сборка выпуска, предпросмотр PDF, правка материалов,
  выгрузка), «Материалы» (материалы из бота и загруженные документы);
- Настройки: «Источники», «Скоринг» (вкладки профилей), «Теги», «Дочерние общества», «Описание Компании»;
- Администрирование: «Пользователи», «Статистика».

Служебные экраны задач и обслуживания открываются по ссылке `?screen=jobs` и `?screen=maintenance`.

## 10. Частые вопросы

**Почему у статьи пустой или короткий текст?** RSS часто даёт только анонс. `fetch-full-text` пытается заменить
его полным текстом страницы; если страница не прошла страж принадлежности, статус `mismatch` и прежний текст
остаётся.

**Почему статья не попала в базу?** Предфильтр счёл её шумом, она старше даты начала сбора источника
(`sources.collect_since`) или её адрес уже есть.

**Почему статья не дошла до темы и баллов?** Обычно одно из трёх: «Отбор» её ещё не решил или отклонил; шаг
«Карточки» выключен (`SIGNAL_CARD_ENABLED=0`) или у агента не задана модель; статья ждёт следующего цикла из-за
лимита шага.

**Почему статьи нет на экране, хотя в базе она есть?** Лента показывает окно месяца (текущий месяц, а по 10-е число
включительно и прошлый), перепечатка уходит из ленты к главной копии, отклонённое «Отбором» видно только
администратору в «Отклонённых».

**Откуда берётся стоимость?** Из `ai_processing_runs`: после каждого вызова модели там токены, стоимость, модель и
статус. Отчёты: `ai-cost-report`, `ai-article-cost-report`, экран «Статистика».

## 11. С чего читать код

1. [README.md](../README.md)
2. этот гид и [architecture.md](architecture.md)
3. [cli.py](../oiltech_digest/cli.py) (функция `build_parser`) и [api.py](../oiltech_digest/api.py)
4. [schema.sql](../oiltech_digest/db/schema.sql) и [repository.py](../oiltech_digest/db/repository.py)
5. агенты по порядку конвейера: [otbor.py](../oiltech_digest/otbor.py),
   [signal_card.py](../oiltech_digest/signal_card.py), [signal_scores.py](../oiltech_digest/signal_scores.py)
6. `ingestion/` и `processing/`
