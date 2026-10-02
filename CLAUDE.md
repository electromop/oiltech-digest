# OilTech Digest

Мониторинг нефтесервисных публикаций, месячный дайджест и агентный слой — технологический
радар и агент источников. Заказчик — ООО «Нефтесервисные решения» (Газпром нефть).

## Единый контур (с 28.09.2026, сессия D)

Один код и один стек: этот репозиторий, стек `/root/oiltech-digest` на РФ (база `oiltech_pg`,
приложение `app`, Caddy) и пять воркеров на NL. Агентный стек (`lowbrains/oiltech-agents`,
база `oiltech_agents_pg`) выведен: его данные перенесены `scripts/d/migrate-agents-to-main.sh`,
`agents.oiltech-digest.ru` — временный редирект (302) на основной домен.

- Ядро: `sh scripts/deploy-core.sh [--schema|--no-schema] <сервисы>` в `/root/oiltech-digest`.
  NL: `sh scripts/deploy-nl.sh` — пять воркеров по одному. Никогда `compose up -d --build` без
  имени сервиса: 20.09 так поднялся лишний планировщик (8,5 ч дублей).
- `init-db` на живой базе — только осознанно: в `schema.sql` есть массовые UPDATE (url_key,
  пометка дублей статей). Новые колонки — точечным `ALTER … IF NOT EXISTS` до выката кода.
- Выкат NL — не во время ежедневного радара (первый цикл планировщика после 00:00 МСК, прогон
  до ~20 мин): `external-worker-agents` перезапустится, и прогон начнётся заново — с повторными
  запросами к Brave и судье. Проверка: `cli external-queues-status` (у `external-agents` running=0).
- `Caddyfile` смонтирован одним файлом: после `git reset` контейнер видит старую копию. Грузить
  так: `docker exec -i oiltech_caddy caddy reload --config /dev/stdin --adapter caddyfile < Caddyfile`.
- Внешние очереди (`external-*`) не исполняются в процессе, который ставит задачу
  (`background_jobs.runs_inline`).

## Граница Германа

Радар и агент источников — зона Германа: `oiltech_digest/signal_*.py`,
`oiltech_digest/source_discovery/**`, `frontend/src/features/signals/**`, экраны агента
источников, тесты `test_signal_*` и `test_source_discovery_*`. Механики, которыми радар
пользуется, — общие: парсинг, скоринг, теги, перевод.

## Технологический радар — ядро и NL

Снимок базы на ядре при выдаче задачи (`signal_discovery.build_external_payload`) → прогон без
базы на NL-воркере `external-worker-agents`, полоса `external-agents` (`process_external_payload`)
→ запись на ядре при complete (`apply_external_result`).

- Ежедневный прогон ставит планировщик на первом цикле после 00:00 МСК (`web_only`: статьи
  корпуса он не читает — без поиска радар пуст). Упавшая задача в те же сутки заново не ставится.
- Поиск — Brave: `BRAVE_SEARCH_API_KEY` обязан быть в `.env.external-worker` на NL, иначе поиск
  вернёт ноль и радар молча ничего не найдёт.
- Темы радара — 13 корневых тегов (`SIGNAL_RADAR_TOPIC_SOURCE=tags`). Тема карточки — по её
  содержанию (ключи тематик), а не по поисковому запросу; решение — `raw_output.theme_choice`.
- Проверка — только через очередь: `enqueue-signal-discovery --topic … --no-offline --dry-run`.
  Без `--no-offline` задача встаёт в `default` на РФ, а `discover-signals` на РФ получает 403 от OpenAI.

## Качество отбора радара (замечания Виктора 29.09)

Судья в том же вызове: `signal_category` (technology / business / other), `event_date` (дата
самого события), `mixed_events` (ссылки о разных событиях), `theme` — одна из 13 тематик из
списка (код проверяет; иначе — ключи тематик), `criteria_scores` по профилю tech_radar — итог
`score` считает `normalize_score_payload`, как у статей (ADR 0002, Б3). Общий балл судьи —
`raw_output.judge_score`. Темы и критерии — в снимке (`radar_themes`, `radar_criteria`).

Экран не показывает (карточка хранится; `_RADAR_VISIBLE_SQL`): архив, бизнес и «другое»,
смешанные, событие старше `SIGNAL_RADAR_MAX_EVENT_AGE_DAYS` (180). NULL — карточка до правки,
видна. Тему рынка радар не ищет (`SIGNAL_RADAR_EXCLUDED_TOPICS`). Архив — `archive-signals`
(сухой прогон по умолчанию) / `unarchive-signals`. Выкат — `scripts/radar/radar-quality-runbook.md`.

## Поиск радара — «режим ChatGPT» (с 01.10 — основной)

`SIGNAL_SEARCH_MODE` в `.env` ядра: `openai_web` — на тему один вызов `gpt-5` со встроенным поиском
OpenAI (`signal_research.py`): события периода с первоисточниками → докачка страницы → наш судья,
ревью, дедуп; `brave` — прежний поиск; `both` — оба. Ссылка подтверждена, если страница
открылась или адрес — среди источников, которые вернул поиск (`web_search_call.action.sources`);
иначе отбрасывается. Модели радара — свои (`SIGNAL_JUDGE_*`, `SIGNAL_REVIEW_*`, `SIGNAL_DEDUP_*`,
`SIGNAL_RESEARCH_*`), `OPENAI_MODEL` ленты не трогать. Настройки и цена —
`scripts/radar/radar-quality-runbook.md`.

## Релевантность и эталон заказчика (02.10)

Судья ставит `oilfield_relevance` (direct / transferable / none) и `oilfield_application`; none
(«не про нефтесервис») экран не показывает. Перенос из горнодобычи, производства, транспорта —
transferable: заказчик такие берёт в свой ТОП. Эталон — его xlsx ТОП-сигналов:
`import-reference-signals` (в память как «сильный сигнал»), `radar-recall` (полнота).

Отсеянное не теряется: брак судьи и ревью, отсеянное поиском, сбой судьи — карточки с
`filter_stage` / `filter_reason`. На радар не выходят, видны всем в «Отсеянные и скрытые».
Отсеянная карточка не держит ссылки, не идёт в дедуп и не затирает принятую с тем же ключом.

## Дедуп радара

Одно событие — одна карточка (`signal_dedup.py`): правило пар (основы заголовка от 0,25, общая
компания или ссылка) → судья «одно ли событие» на NL → звезда, а не цепочка. Дубль не удаляется:
`signals.merged_into_signal_id` + `merge_reason`, его ссылки видны в главной карточке. Разобранную
карточку не прячет никогда. Снять пометку:
`UPDATE signals SET merged_into_signal_id = NULL, merge_reason = NULL WHERE id = …`.

Ссылки не уезжают между видимыми карточками (28.09, сигналы 97 и 71): `upsert_signal_evidence`
переносит ссылку только из скрытого дубля в главную; дубль ревью пачки принимается, только если у
пары общая ссылка или похожий заголовок. Устаревшие счётчики — `refresh-signal-evidence-counts`.

## Агент источников — известные дефекты

Описание для заказчика — `docs/agent_istochnikov_dlya_zakazchika.md`. Включается флагом
`SOURCE_DISCOVERY_ENABLED` в `.env` (решение владельца 28.09 — включён); планировщик ставит его
раз в 24 цикла, в том числе на первом цикле после перезапуска.

1. ИИ-часть плана и цикла исполняется на ядре: при вынесенном ИИ поиск и песочница идут по
   правилам (`agent.core_ai_offline`, в итоге — `ai_forced_offline`); оценка кандидата с ИИ
   уходит на NL. Перенос ИИ-части плана и цикла на NL — открыт.
2. Одобренный кандидат сразу попадает в основную ленту: база одна (до D — только в базу агентов).

## Agent skills

### Issue tracker

Задачи и спеки живут в GitHub Issues репозитория `electromop/oiltech-digest`,
операции — через `gh`. См. `docs/agents/issue-tracker.md`.

### Triage labels

Пять канонических ролей, строка метки равна имени роли. См. `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` в корне + `docs/adr/`. См. `docs/agents/domain.md`.
