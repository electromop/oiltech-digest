# Handoff 2026-08-21: Source Discovery Agent

## Контекст

Ветка: `codex/source-discovery-agent`.

Цель ветки: довести MVP поиска дополнительных источников до управляемого agent loop:
агент анализирует дефицит тем, ищет кандидатов источников, проверяет их в песочнице,
делегирует AI-оценку на внешний контур при необходимости, копит память и показывает
оператору понятную ленту решений.

## Что сделано

### Backend: агент и планирование

- Добавлен пакет `oiltech_digest/source_discovery/`:
  - `agent.py` — поиск кандидатов источников, генерация запросов, seed-url режим,
    фильтрация запрещённых доменов, запись кандидатов.
  - `planner.py` — детерминированный планировщик действий по дефициту тем,
    качеству источников, кандидатам и памяти.
  - `loop.py` — итерационный agent loop со стратегиями поиска, памятью стратегий,
    бюджетами и остановками.
  - `sandbox.py` — проверка кандидата на тестовых материалах.
  - `readiness.py` — диагностика готовности агента, внешнего AI, scheduler и бюджета.
  - `prompts.py` — инструкции/схемы для AI-запросов агента.

### Backend: API и очередь

- Добавлены API endpoints:
  - `GET /api/source-candidates`
  - `GET /api/source-candidates/triage`
  - `GET /api/source-candidates/{id}/articles`
  - `PATCH /api/source-candidates/{id}`
  - `POST /api/source-candidates/{id}/evaluate`
  - `POST /api/source-candidates/{id}/approve`
  - `GET /api/source-discovery/plan`
  - `POST /api/source-discovery/plan/enqueue`
  - `POST /api/source-discovery/loop/enqueue`
  - `GET /api/source-discovery/memory`
  - `POST /api/source-discovery/memory`
  - `PATCH /api/source-discovery/memory/{id}`
  - `GET /api/source-discovery/actions`
  - `GET /api/source-discovery/runs`
  - `GET /api/source-discovery/quality`
  - `GET /api/source-discovery/query-memory`
  - `GET /api/source-discovery/readiness`

- Добавлены background jobs:
  - `discover_source_candidates`
  - `source_discovery_plan`
  - `source_discovery_loop`
  - `source_candidate_evaluate`
  - `parse_source_once`

- `approve_source_candidate` теперь после одобрения ставит первый сбор:
  - `request/playwright` → `scrape_source`;
  - `rss` → `parse_source_once`.

### External worker

- Внешний AI-контур умеет обрабатывать `source_candidate_evaluate`.
- После результата external AI core:
  - сохраняет результаты по статьям кандидата;
  - обновляет assessment кандидата;
  - пишет память по теме и домену;
  - пишет событие `source_candidate_learning`;
  - привязывает событие к `agent_run_id`, если задача пришла из loop.

### Память и control plane

- Добавлена `agent_memory`:
  - темы;
  - домены;
  - запросы;
  - стратегии;
  - ручные правила.

- Ручные правила через UI/API:
  - `domain + rejected` — агент не создаёт кандидатов с домена;
  - `topic + rejected` — агент не планирует поиск по теме;
  - `topic/domain + active` — приоритетное правило.

- События агента теперь получают операторские поля:
  - `decision_title`;
  - `decision_summary`;
  - `decision_tone`.

### Scheduler и прод-предохранители

- Добавлен режим scheduler:
  - `SOURCE_DISCOVERY_MODE=loop|plan|topics`.

- Для loop добавлены суточные бюджеты:
  - `SOURCE_DISCOVERY_MAX_DAILY_LOOP_RUNS`
  - `SOURCE_DISCOVERY_MAX_DAILY_CANDIDATES`
  - `SOURCE_DISCOVERY_MAX_DAILY_EVALUATIONS`

- Loop штатно останавливается с причинами:
  - `daily_loop_budget_reached`;
  - `daily_candidate_budget_reached`;
  - `daily_evaluation_budget_reached`;
  - `budget_usage_unavailable`.

- `enqueue-agent-loop` защищён от параллельных loop-задач.

### Frontend

- Добавлены страницы:
  - `SourceAgentPage.tsx`;
  - `SourceCandidatesPage.tsx`.

- В экран агента добавлены:
  - настройки;
  - план действий;
  - learning summary;
  - память;
  - форма ручного правила;
  - качество по темам/доменам;
  - query memory;
  - список запусков;
  - последний loop;
  - память стратегий;
  - лента решений;
  - readiness;
  - суточный бюджет.

- В экран кандидатов добавлены:
  - список кандидатов;
  - triage queue;
  - раскрытие тестовых материалов;
  - проверка кандидата;
  - approve/reject/pause;
  - approve теперь показывает job первого сбора.

### Документация

- `docs/source_discovery_agent_spec.md` — подробное ТЗ/спецификация агента.
- `docs/selection_pipeline_roadmap.html` — визуальная схема текущего и будущего pipeline.

## Как включить agent loop на сервере

Минимальные env для scheduler:

```env
SOURCE_DISCOVERY_ENABLED=1
SOURCE_DISCOVERY_MODE=loop
SOURCE_DISCOVERY_EVERY_CYCLES=24
SOURCE_DISCOVERY_TOPIC_LIMIT=5
SOURCE_DISCOVERY_LIMIT=10
SOURCE_DISCOVERY_MAX_ACTIONS=5
SOURCE_DISCOVERY_MAX_ITERATIONS=3
SOURCE_DISCOVERY_ARTICLE_LIMIT=5
SOURCE_DISCOVERY_MAX_DAILY_LOOP_RUNS=4
SOURCE_DISCOVERY_MAX_DAILY_CANDIDATES=100
SOURCE_DISCOVERY_MAX_DAILY_EVALUATIONS=100
```

Если AI выполняется на зарубежном воркере:

```env
EXTERNAL_WORKERS_ENABLED=1
AI_EXECUTION_REGION=external
SOURCE_DISCOVERY_OFFLINE=0
```

Также нужен внешний worker с capability `openai` и очередью `external-ai`.

## Проверки, которые прошли локально

Проходили после последних итераций:

```bash
python3 -m py_compile ...
npm --prefix frontend run build
npm --prefix frontend test -- --run
sh -n scripts/docker-scheduler.sh
git diff --check
```

Frontend tests: `14/14` passed.

Docker backend pytest локально не прошёл полностью: Docker поднялся, но в app image
нет `pytest`:

```text
/usr/local/bin/python: No module named pytest
```

Ранее, когда Docker daemon был недоступен, ошибка была:

```text
failed to connect to the docker API
```

## Что проверить после деплоя

1. Миграции схемы:

```bash
docker compose run --rm app python -m oiltech_digest.cli init-db
```

2. Readiness:

```bash
docker compose run --rm app python -m oiltech_digest.cli agent-readiness
```

3. Ручной loop:

```bash
docker compose run --rm app python -m oiltech_digest.cli enqueue-agent-loop \
  --no-offline \
  --max-iterations 1 \
  --max-actions 2 \
  --candidate-limit 5
```

4. Worker:

```bash
docker compose run --rm app python -m oiltech_digest.cli jobs-worker --once
```

5. Проверить в UI:
  - экран агента;
  - ленту решений;
  - память;
  - кандидатов;
  - approve кандидата и появление `initial_job`.

## Что осталось

## Что осталось до полного агента

Текущая версия — уже рабочий управляемый agent loop, но это ещё не полностью
самостоятельный агент принятия решений. Ниже список того, что нужно доделать, чтобы
считать его полноценным production-агентом.

### 1. End-to-end production loop

Нужно один раз пройти полный боевой сценарий на Docker/серверном контуре:

```text
scheduler
-> source_discovery_loop
-> discover_source_candidates
-> source_candidate_evaluate в external-ai
-> внешний worker
-> apply_source_candidate_result
-> source_candidate_learning
-> approve_source_candidate
-> parse_source_once / scrape_source
-> новые сигналы в базе
```

Критерий готовности: по одному запуску видно в UI, какие решения агент принял,
каких кандидатов нашёл, что ушло во внешний AI, чему агент научился и какой первый
сбор стартовал после approve.

### 2. Decision layer вместо одного основного действия

Сейчас loop в основном делает `discover_sources`. Нужно добавить слой выбора
следующего действия:

- искать новые источники по дефицитной теме;
- глубже тестировать уже найденного кандидата;
- перепроверять слабый источник;
- предлагать изменить частоту источника;
- временно остановить шумную тему;
- не делать ничего, если бюджет/качество не позволяют.

Критерий готовности: в одном loop могут появляться разные типы действий, а не только
поиск новых источников.

### 3. Обучение на пользовательской обратной связи по сигналам

Нужно связать события feedback по сигналам с памятью агента:

- пользователь пометил сигнал полезным;
- пользователь отправил сигнал в шум;
- пользователь добавил/убрал из дайджеста;
- пользователь поменял статус.

Агент должен агрегировать это по теме, источнику, домену, тегу и использовать при
планировании.

Критерий готовности: пользовательская реакция на сигналы меняет приоритеты поиска
источников и тем.

### 4. Метрики эффективности агента

Нужен отдельный отчёт:

- кандидатов найдено за loop;
- кандидатов дошло до approve;
- новых сигналов после approve;
- релевантных сигналов после approve;
- стоимость AI на кандидата;
- стоимость AI на полезный источник;
- полезность тем/доменов/стратегий.

Критерий готовности: можно понять, окупается ли агент и какие стратегии дают результат.

### 5. Более сильные стратегии поиска

Сейчас стратегии базовые. Нужно расширить источники поиска:

- пресс-релизы компаний;
- корпоративные блоги;
- научные публикации;
- конференции;
- патенты;
- регуляторные сайты;
- отраслевые медиа;
- vendor docs / product release notes.

Критерий готовности: агент умеет искать не только “newsroom”-источники, но и
технологические, научные и продуктовые источники.

### 6. Расширенный control plane

Сейчас есть ручные правила домен/тема. Нужно добавить:

- bulk import/export правил;
- белый список доменов;
- приоритетные компании;
- запрет источников по паттернам URL;
- временные правила с expiry;
- комментарий оператора к правилу.

Критерий готовности: оператор может управлять агентом без SQL и без правок кода.

### 7. Надёжность очередей и тестовый Docker target

Нужно добавить нормальный test target/image:

- `pytest` должен быть доступен в Docker;
- backend tests должны запускаться одной командой;
- отдельный тестовый профиль не должен тащить production runtime мусор.

Критерий готовности:

```bash
docker compose run --rm app python -m pytest
```

или отдельный:

```bash
docker compose run --rm test
```

проходит в чистом окружении.

### 8. Safety-политики автономных действий

Нужно явно описать и закрепить в коде, какие действия агент может делать сам:

- можно: искать кандидатов;
- можно: тестировать кандидатов;
- можно: писать память;
- можно: ставить первичный сбор после approve;
- нельзя: включать источник без человека;
- нельзя: удалять источник;
- нельзя: менять частоту без подтверждения;
- нельзя: игнорировать rejected правила.

Критерий готовности: safety-policy проверяется тестами, а не только документацией.

### 9. Нормальный run report

Нужно сделать итоговый отчёт одного запуска:

- цель;
- старт/финиш;
- бюджет до/после;
- действия;
- найденные кандидаты;
- поставленные external jobs;
- чему научился;
- что ждёт человека;
- ошибки.

Критерий готовности: по одному отчёту можно принять решение, запускать ли агента
регулярно.

### 10. Production rollout

План rollout:

1. Включить `SOURCE_DISCOVERY_MODE=plan` без loop.
2. Проверить планы и UI.
3. Включить `SOURCE_DISCOVERY_MODE=loop` с маленькими бюджетами.
4. Проверить external AI.
5. Проверить approve + first parse/scrape.
6. Поднять бюджеты.
7. Включить регулярный scheduler.

Критерий готовности: агент работает несколько циклов без ручного вмешательства,
не превышает бюджеты и даёт кандидатов, которых оператор реально одобряет.

### P0 перед production autopilot

- Установить `pytest` в dev/test image или добавить отдельный test target, чтобы backend tests гонялись в Docker.
- Сделать полный end-to-end прогон:
  `scheduler -> source_discovery_loop -> source_candidate_evaluate external-ai -> external worker -> learning -> approve -> first parse/scrape`.
- Проверить, что внешний worker реально забирает `source_candidate_evaluate`.

### P1

- Улучшить decision layer: сейчас loop в основном делает `discover_sources`.
  Следующий шаг — выбирать между:
  - поиск новых источников;
  - глубокая проверка кандидата;
  - перепроверка слабого источника;
  - рекомендация изменения частоты;
  - stop по шумной теме.

- Добавить метрики эффективности:
  - найдено кандидатов за loop;
  - доля одобренных;
  - новые сигналы после approve;
  - стоимость external AI на кандидата;
  - полезность доменов и тем.

### P2

- Сделать отдельный полноценный экран журнала решений, если текущей встроенной ленты
  в `SourceAgentPage` станет мало.
- Добавить bulk operations для memory rules.
- Добавить import/export правил агента.
