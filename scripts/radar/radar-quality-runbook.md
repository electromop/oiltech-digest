# Качество радара (замечания Виктора 29.09) — выкат

Ветка `fix/radar-quality-viktor`. Что меняется — `tests/test_signal_quality.py` и CLAUDE.md,
раздел «Качество отбора радара».

## Порядок

Окно — не во время ежедневного радара (первый цикл планировщика после 00:00 МСК, до ~20 мин).
Проверка, что радар не идёт: `cli external-queues-status` — у `external-agents` running=0.

1. **Схема до кода** (только добавления, повтор безопасен):

   ```bash
   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
     < scripts/radar/radar-quality-before-deploy.sql
   ```

2. **Ядро:** `sh scripts/deploy-core.sh --no-schema app scheduler`.
   С этого момента экран скрывает бизнес, «другое», смешанные, старые (>180 дней) и архивные
   карточки. У прежних карточек поля пустые — они видны, пока не пройдут новым судьёй.

3. **NL:** `sh scripts/deploy-nl.sh` (владелец). Судья начинает ставить категорию, дату события,
   флаг смешанных событий, тему из списка и баллы по профилю tech_radar.
   До пересборки NL ядро и старый воркер совместимы: новые поля просто не приходят.

4. **Профиль tech_radar должен быть с суммой 100:**

   ```bash
   docker exec oiltech_app python -m oiltech_digest.cli apply-scoring-preset --profile tech_radar --preset viktor
   ```

   Сухой прогон «до / после»; если набор уже Виктора — ничего не меняет. Сумма не 100 — судья
   ставит прежний общий балл (в итоге прогона `radar_criteria` пуст).

5. **Архив ранних карточек** — сначала список:

   ```bash
   docker exec oiltech_app python -m oiltech_digest.cli archive-signals --created-before 2026-09-14
   ```

   Ожидание по замеру 29.09 — около 38 карточек со свободной темой, баллы 20–40. Разобранные
   помечены `[разобрана]` — решает владелец. Записать — тот же вызов с `--apply`.
   Вернуть: `unarchive-signals --reason early-free-theme-2026-09` (или `--ids 1,2,3`).

6. **Новый прогон радара** через очередь:

   ```bash
   docker exec oiltech_app python -m oiltech_digest.cli enqueue-signal-discovery --no-offline
   ```

   (сначала `--dry-run` на одну тему, если нужно). Итог задачи: у кандидатов
   `raw_output.theme_choice.reason = judge`, `raw_output.judge_score` против `profile_score`.

## Режим поиска «ChatGPT» (решение 01.10: в прод идёт он)

Находки темы — один вызов `gpt-5` со встроенным поиском OpenAI на тему (`signal_research.py`),
дальше наш судья, ревью, дедуп. Сравнение 30.09–01.10 на 13 темах: 28 технологических карточек
на радаре (Brave 28.09 — 23, сильных две), почти все — события сентября, первоисточники,
много российских. Файл для Виктора — `radar_chatgpt_mode_2026-10-01.xlsx`.

**Где что задаётся.** Режим решает ядро и кладёт в задачу; модели читает NL.

- Ядро, `/root/oiltech-digest/.env`:

  ```
  SIGNAL_SEARCH_MODE=openai_web
  ```

  затем `sh scripts/deploy-core.sh --no-schema app scheduler` (переменная читается при старте).

- NL, `.env.external-worker` (владелец), затем `sh scripts/deploy-nl.sh`:

  ```
  SIGNAL_JUDGE_MODEL=gpt-5
  SIGNAL_JUDGE_REASONING=medium
  SIGNAL_REVIEW_MODEL=gpt-5
  SIGNAL_REVIEW_REASONING=medium
  # по умолчанию уже так, задавать не нужно:
  # SIGNAL_RESEARCH_MODEL=gpt-5  SIGNAL_RESEARCH_REASONING=low  SIGNAL_RESEARCH_DAYS=30
  ```

  `OPENAI_MODEL` воркера не трогать: им идут суть, теги и скоринг всей ленты.

**Время и деньги (замер локально, 13 тем).** Чистый прогон — около 2 ч, в основном судья на
`gpt-5 medium`; поиск — ~$2,4 за прогон (по нашей ставке gpt-5) плюс плата OpenAI за вызовы
web_search (~290 вызовов); судья на gpt-5 — сверх этого, отдельно не замерян. Ежедневный прогон
в этом режиме — десятки долларов в месяц: если дорого — `SIGNAL_JUDGE_MODEL=gpt-5-mini`,
`SIGNAL_JUDGE_REASONING=low` (быстрее и дешевле, качество не сравнивали) или перевод радара на
еженедельный запуск (в планировщике сейчас его нет — отдельная правка).

**Аренда на NL.** Порог зависания радара — 1200 с без признака продвижения; поиск подаёт его
перед каждой попыткой, судья — на каждый кластер. Таймауты: поиск 480 с, судья 240 с.

**Проверка первым прогоном:** в итоге задачи у тем `web_search.provider = openai_web_search`,
`research.status = ok`, `research.events` > 0, `research.unverified` — единицы. `status = error` с
403 — ключ OpenAI NL не пускает в web_search (проверено локально: наш ключ пускает).

**Откат на Brave:** `SIGNAL_SEARCH_MODE=brave` в `.env` ядра и перезапуск `app scheduler`.

## Проверка на проде (только чтение)

```sql
SELECT signal_category, count(*) FROM signals WHERE created_at > now() - interval '1 day' GROUP BY 1;
SELECT count(*) FILTER (WHERE event_date IS NOT NULL) AS dated, count(*) AS total
FROM signals WHERE created_at > now() - interval '1 day';
SELECT count(*) FROM signals WHERE mixed_events;
SELECT score_profile, round(avg(score)) FROM signals WHERE created_at > now() - interval '1 day' GROUP BY 1;
```

## Откат

Код — прежний коммит ядра и NL. Колонки остаются (NULL — «как до правки»), удалять их не нужно.
Архив — `unarchive-signals --reason early-free-theme-2026-09`.

## Настройки

- `SIGNAL_RADAR_MAX_EVENT_AGE_DAYS` (180; 0 — не скрывать по возрасту) — читается при старте ядра.
- `SIGNAL_RADAR_EXCLUDED_TOPICS` («Рынок, бизнес-модели»; несколько — через `;`) — начало имени
  тематики, которую радар не ищет.
