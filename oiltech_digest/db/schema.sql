-- OilTech Digest — схема БД (PostgreSQL).
-- Структура и связи — по docs/architecture.md §9. Типы адаптированы под Postgres.
-- Идемпотентно: повторный запуск безопасен (CREATE TABLE IF NOT EXISTS).
-- На Issue #1 наполняются только sources и articles; остальные таблицы создаются «впрок».

-- =========================================================================
-- Источники
-- =========================================================================
CREATE TABLE IF NOT EXISTS sources (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name           TEXT NOT NULL,
  source_type    TEXT NOT NULL,                 -- из Excel «Тип» (Journal/News/Company/Telegram/...)
  url            TEXT,                           -- Excel «Ссылка» (главный сайт)
  rss_url        TEXT,                           -- проставляется discover-rss
  enabled        BOOLEAN DEFAULT TRUE,
  parse_strategy TEXT,                           -- rss / request / telegram / playwright / none
  listing_url    TEXT,                           -- страница со списком новостей для request-источников
  listing_strategy TEXT,                         -- auto / links / cards (пока auto)
  listing_selector TEXT,                         -- CSS/XPath-подсказка для карточек листинга
  article_link_selector TEXT,                    -- CSS/XPath-подсказка для ссылок на статью
  article_date_selector TEXT,                    -- CSS/XPath-подсказка для даты публикации
  category       TEXT,
  update_frequency TEXT,                         -- Excel «Частота мониторинга»
  priority       NUMERIC DEFAULT 1.0,            -- из Excel «Рейтинг источника» (1..3)
  last_parsed_at TIMESTAMPTZ,
  last_seen_article_url TEXT,
  last_seen_published_at TIMESTAMPTZ,
  last_listing_hash TEXT,
  network_region TEXT NOT NULL DEFAULT 'auto',   -- auto / ru / external
  network_profile TEXT NOT NULL DEFAULT 'direct',-- direct / proxy / browser
  last_ru_probe_status TEXT,
  last_external_probe_status TEXT,
  external_required_reason TEXT,
  external_cooldown_until TIMESTAMPTZ,
  created_at     TIMESTAMPTZ DEFAULT now(),
  updated_at     TIMESTAMPTZ DEFAULT now()
);
-- Естественный ключ: бренд может иметь несколько каналов (сайт + Telegram) с одним
-- именем, но разным типом — это разные источники. Поэтому уникальность по (name, source_type).
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_name_type ON sources(name, source_type);
CREATE INDEX IF NOT EXISTS idx_sources_enabled_strategy ON sources(enabled, parse_strategy);

ALTER TABLE sources ADD COLUMN IF NOT EXISTS listing_url TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS listing_strategy TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS listing_selector TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS article_link_selector TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS article_date_selector TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_seen_article_url TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_seen_published_at TIMESTAMPTZ;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_listing_hash TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS network_region TEXT NOT NULL DEFAULT 'auto';
ALTER TABLE sources ADD COLUMN IF NOT EXISTS network_profile TEXT NOT NULL DEFAULT 'direct';
ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_ru_probe_status TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_external_probe_status TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS external_required_reason TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS external_cooldown_until TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_sources_last_seen_published_at ON sources(last_seen_published_at DESC);
CREATE INDEX IF NOT EXISTS idx_sources_network_region ON sources(network_region, enabled);

-- Архив источника (требование заказчика 12.09: «выключаем источник — не парсится больше,
-- уходит в архив, его статьи уходят из выборки»). Отдельно от `enabled`, потому что это
-- РАЗНЫЕ вещи: выключенный источник просто не опрашивается, но его накопленные статьи
-- продолжают висеть в ленте у всех (лента джойнит sources без условия на enabled).
-- Архивный — и не опрашивается, и не показывает свои статьи. Обратимо: NULL = активен.
-- Жёсткого DELETE нет намеренно: articles.source_id ссылается на sources БЕЗ ON DELETE,
-- то есть Postgres просто откажет удалить источник, у которого есть хоть одна статья.
ALTER TABLE sources ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_sources_archived_at ON sources(archived_at) WHERE archived_at IS NOT NULL;

-- =========================================================================
-- Статьи (сырые, до обработки)
-- =========================================================================
CREATE TABLE IF NOT EXISTS articles (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id      BIGINT NOT NULL REFERENCES sources(id),
  title          TEXT NOT NULL,
  url            TEXT NOT NULL,
  published_at   TIMESTAMPTZ,
  collected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  raw_text       TEXT,
  text_truncated BOOLEAN DEFAULT FALSE,           -- RSS отдал обрезанный/сокращённый текст
  full_text_fetched_at TIMESTAMPTZ,
  full_text_status TEXT,                          -- ok / failed / too_short / no_gain / mismatch / blocked / paywall
                                                  -- no_gain: текст извлёкся нормально, но не вдвое длиннее
                                                  -- уже сохранённого — перезаписывать нечем, статья цела
  full_text_error TEXT,
  extraction_method TEXT,                         -- rss / lxml / trafilatura / selector
  language       TEXT,
  content_hash   TEXT,
  created_at     TIMESTAMPTZ DEFAULT now(),
  updated_at     TIMESTAMPTZ DEFAULT now()
);
-- body_hash — хэш САМОГО ТЕКСТА (в отличие от content_hash = заголовок+URL). Нужен,
-- чтобы ловить подмену «одно тело — многим статьям» (задача №24): без него на проде
-- накопилось 951 статья с побайтово общим телом, а всего чужое тело оказалось
-- у 10.4% видимой ленты.
ALTER TABLE articles ADD COLUMN IF NOT EXISTS body_hash TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_url ON articles(url);
CREATE INDEX IF NOT EXISTS idx_articles_content_hash ON articles(content_hash);
-- Составной: проверка «такое тело у этого источника уже есть» идёт всегда в паре.
CREATE INDEX IF NOT EXISTS idx_articles_source_body_hash ON articles(source_id, body_hash);
CREATE INDEX IF NOT EXISTS idx_articles_source_id ON articles(source_id);
CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at DESC);


-- =========================================================================
-- Карточки статей (рабочее представление в «Все статьи») — будущее
-- =========================================================================
-- Перепечатки: одна публикация, разошедшаяся по нескольким изданиям. Таблица
-- ГЛОБАЛЬНАЯ, в отличие от user_article_states.status='duplicate': перепечатка —
-- факт о материале, а не мнение конкретного пользователя.
--
-- Почему отдельная таблица, а не флаг в articles: запись обратима и объяснима.
-- Храним, ЧЕГО копия, чем измерено сходство и почему судья решил именно так —
-- без этого разбор жалобы «почему статья пропала» превращается в гадание, а
-- заказчик уже жаловался на исчезновение материалов.
--
-- Идёт ПОСЛЕ articles: обе ссылки ведут туда, и порядок блоков в этом файле
-- значим — блок исполняется сверху вниз одной транзакцией.
CREATE TABLE IF NOT EXISTS article_reprints (
  article_id     BIGINT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  primary_id     BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  similarity     NUMERIC,
  reason         TEXT,
  decided_by     TEXT NOT NULL DEFAULT 'ai',
  model          TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT article_reprints_not_self CHECK (article_id <> primary_id)
);

CREATE INDEX IF NOT EXISTS idx_article_reprints_primary ON article_reprints(primary_id);

CREATE TABLE IF NOT EXISTS article_cards (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  article_id          BIGINT NOT NULL REFERENCES articles(id),
  summary             TEXT,
  summary_model       TEXT,
  summary_generated_at TIMESTAMPTZ,
  title_ru            TEXT,                       -- русский заголовок (перевод иностранных при суммаризации)
  relevant            BOOLEAN,                    -- AI-фильтр релевантности (Issue: AI-gate)
  relevance_reason    TEXT,
  relevance_model     TEXT,
  status              TEXT DEFAULT 'new',         -- new / digest / archive / noise / duplicate / rejected
                                                  -- ЛЕГАСИ: глобальная колонка, вытеснена user_article_states
  selected_for_digest BOOLEAN DEFAULT FALSE,
  digest_month        TEXT,
  analyst_comment     TEXT,
  created_at          TIMESTAMPTZ DEFAULT now(),
  updated_at          TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_article_cards_article_id ON article_cards(article_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_article_cards_article_unique ON article_cards(article_id);

-- =========================================================================
-- Скоринг — будущее
-- =========================================================================
CREATE TABLE IF NOT EXISTS scoring_criteria (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name             TEXT NOT NULL,
  description      TEXT,
  weight           NUMERIC NOT NULL DEFAULT 0,
  keywords_json    JSONB,
  keywords_en_json JSONB,
  enabled          BOOLEAN DEFAULT TRUE,
  sort_order       INTEGER DEFAULT 0,
  created_at       TIMESTAMPTZ DEFAULT now(),
  updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS article_scores (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  article_id   BIGINT NOT NULL REFERENCES articles(id),
  model        TEXT,
  total_score  NUMERIC NOT NULL,
  score_label  TEXT,
  explanation  TEXT,
  created_at   TIMESTAMPTZ DEFAULT now(),
  updated_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_article_scores_article_id ON article_scores(article_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_article_scores_article_unique ON article_scores(article_id);
-- Происхождение балла (сессия G, ADR 0002): каким профилем и каким набором критериев он
-- посчитан. NULL — балл до профилей, читается как business. Снимок — id, имя, вес и хэш
-- текста каждого критерия на момент оценки: по нему видно, хватит ли пересчёта без ИИ
-- (изменились только веса) или нужен новый вызов модели (тексты или набор другие).
ALTER TABLE article_scores ADD COLUMN IF NOT EXISTS profile TEXT;
ALTER TABLE article_scores ADD COLUMN IF NOT EXISTS criteria_snapshot JSONB;
-- Профили скоринга (сессия G, ADR 0002): «Бизнес-сигналы» (business) и «Технологический
-- радар» (tech_radar). Колонка с умолчанием, без UPDATE: строки, что были до профилей,
-- читаются как business, и лента считается тем же набором, что и раньше. Имя уникально в
-- пределах профиля: «Технологическая новизна» есть и в наборе радара, и в старом сиде.
-- На живой базе эти строки выполняются вручную ДО выката кода (scripts/g/), выкат —
-- --no-schema. Глобальный индекс имён снимается ПОСЛЕ выката: старый код пишет
-- ON CONFLICT (name) и без этого индекса не сохранил бы новый критерий. Блок — после
-- article_scores: порядок таблиц как у записи балла и у шага 1 выката.
ALTER TABLE scoring_criteria ADD COLUMN IF NOT EXISTS profile TEXT NOT NULL DEFAULT 'business';
DO $$
BEGIN
  ALTER TABLE scoring_criteria
    ADD CONSTRAINT scoring_criteria_profile_check CHECK (profile IN ('business', 'tech_radar'));
EXCEPTION
  WHEN duplicate_object THEN NULL;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS idx_scoring_criteria_profile_name ON scoring_criteria(profile, name);
DROP INDEX IF EXISTS idx_scoring_criteria_name;

CREATE TABLE IF NOT EXISTS article_score_items (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  article_score_id BIGINT NOT NULL REFERENCES article_scores(id),
  criterion_id     BIGINT NOT NULL REFERENCES scoring_criteria(id),
  keyword_score    NUMERIC,
  ai_score         NUMERIC,
  final_score      NUMERIC,
  rationale        TEXT,
  created_at       TIMESTAMPTZ DEFAULT now()
);

-- =========================================================================
-- Теги (иерархические) — будущее
-- =========================================================================
CREATE TABLE IF NOT EXISTS tags (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  parent_id     BIGINT REFERENCES tags(id),
  name          TEXT NOT NULL,
  name_en       TEXT,
  description   TEXT,
  keywords_json JSONB,
  keywords_en_json JSONB,
  enabled       BOOLEAN DEFAULT TRUE,
  sort_order    INTEGER DEFAULT 0,
  created_at    TIMESTAMPTZ DEFAULT now(),
  updated_at    TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_name_parent ON tags(name, (COALESCE(parent_id, 0)));
-- Стоп-слова (negative keywords) у родительских тегов: статья со стоп-словом исключается на этапе релевантности.
ALTER TABLE tags ADD COLUMN IF NOT EXISTS negative_keywords_json JSONB;

CREATE TABLE IF NOT EXISTS article_tags (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  article_id  BIGINT NOT NULL REFERENCES articles(id),
  tag_id      BIGINT NOT NULL REFERENCES tags(id),
  model       TEXT,
  confidence  NUMERIC,
  rationale   TEXT,
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_article_tags_article_id ON article_tags(article_id);
CREATE INDEX IF NOT EXISTS idx_article_tags_tag_id ON article_tags(tag_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_article_tags_article_unique ON article_tags(article_id);

-- =========================================================================
-- Месячные дайджесты — будущее
-- =========================================================================
CREATE TABLE IF NOT EXISTS monthly_digests (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id     BIGINT,
  month       TEXT NOT NULL,                  -- YYYY-MM
  title       TEXT,
  status      TEXT DEFAULT 'draft',
  created_at  TIMESTAMPTZ DEFAULT now(),
  updated_at  TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE monthly_digests ADD COLUMN IF NOT EXISTS user_id BIGINT;
DROP INDEX IF EXISTS idx_monthly_digests_month;
CREATE UNIQUE INDEX IF NOT EXISTS idx_monthly_digests_user_month ON monthly_digests(user_id, month);
CREATE UNIQUE INDEX IF NOT EXISTS idx_monthly_digests_shared_month ON monthly_digests(month) WHERE user_id IS NULL;

CREATE TABLE IF NOT EXISTS monthly_digest_items (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  digest_id   BIGINT NOT NULL REFERENCES monthly_digests(id),
  article_id  BIGINT NOT NULL REFERENCES articles(id),
  sort_order  INTEGER DEFAULT 0,
  section     TEXT,
  editor_note TEXT,
  created_at  TIMESTAMPTZ DEFAULT now()
);

-- =========================================================================
-- История выгрузок — будущее
-- =========================================================================
CREATE TABLE IF NOT EXISTS export_jobs (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  export_type   TEXT NOT NULL,                 -- hourly / monthly_digest
  format        TEXT,                           -- pdf / docx / csv
  status        TEXT,
  file_path     TEXT,
  error_message TEXT,
  started_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ
);

-- =========================================================================
-- Пользователи и сессии админки
-- =========================================================================
CREATE TABLE IF NOT EXISTS users (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email          TEXT NOT NULL UNIQUE,
  password_salt  TEXT NOT NULL,
  password_hash  TEXT NOT NULL,
  role           TEXT NOT NULL DEFAULT 'user',  -- 'admin' (всё+пользователи) | 'user' (свой срез, без настроек)
  created_at     TIMESTAMPTZ DEFAULT now(),
  updated_at     TIMESTAMPTZ DEFAULT now()
);

DO $$
BEGIN
  ALTER TABLE monthly_digests
    ADD CONSTRAINT monthly_digests_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION
  WHEN duplicate_object THEN NULL;
END $$;

-- До #111 — личное состояние пользователя: свои статусы статей и свой дайджест. С #111 —
-- архив только для чтения: отметки общие (article_marks ниже), перенос — migrate-shared-marks.
CREATE TABLE IF NOT EXISTS user_article_states (
  user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  article_id      BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  status          TEXT NOT NULL DEFAULT 'new',  -- new / digest / archive / noise / duplicate
                                                -- archive скрывает из ленты (как noise/duplicate),
                                                -- но БЕЗ штрафа баллу качества источника
  analyst_comment TEXT,
  updated_at      TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (user_id, article_id)
);
CREATE INDEX IF NOT EXISTS idx_user_article_states_user_status ON user_article_states(user_id, status);

-- 12.09.2026: статус `review` («На проверке») убран из набора (решение заказчика).
-- Существующие строки переводим в `new`, а НЕ в `archive`, хотя новый переход
-- «снял из дайджеста» ведёт именно в archive. Причина: `review` использовался как
-- «посмотрите, тут косяк» — 22.08 заказчика прямо просили ставить этот статус, чтобы
-- системно отловить дефекты. `archive` теперь СКРЫВАЕТ статью из ленты, и миграция в
-- него спрятала бы ровно те статьи, ради которых пометка ставилась. `new` возвращает
-- их в общий поток — ничего не теряется.
-- Идемпотентно: повторный запуск не находит строк и ничего не делает.
UPDATE user_article_states SET status = 'new' WHERE status = 'review';
-- Та же чистка в легаси-колонке article_cards.status (до #111 её читал разовый перенос
-- migrate_global_status_to_user), чтобы комментарии схемы не расходились с данными.
UPDATE article_cards SET status = 'new' WHERE status = 'review';

-- Общие отметки статей (#111, ADR 0004): одна строка на статью, кто бы её ни поставил.
-- Нет строки — «новая». Последний поставивший — set_by_user_id (учётку удалили — пусто,
-- отметка остаётся). user_article_states выше — архив личных отметок только для чтения:
-- приложение его не читает и не пишет (перенос — команда migrate-shared-marks).
CREATE TABLE IF NOT EXISTS article_marks (
  article_id      BIGINT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  status          TEXT NOT NULL DEFAULT 'new',  -- new / digest / archive / noise / duplicate
  analyst_comment TEXT,
  set_by_user_id  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_article_marks_status ON article_marks(status);

CREATE TABLE IF NOT EXISTS user_sessions (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id        BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  session_token  TEXT NOT NULL UNIQUE,
  expires_at     TIMESTAMPTZ NOT NULL,
  created_at     TIMESTAMPTZ DEFAULT now(),
  last_seen_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_token ON user_sessions(session_token);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id ON user_sessions(user_id);

-- Допуск по коду (#113, admission.py). Время допуска пусто — учётка ждёт допуска; новые учётки
-- создаются без него. Учётки, которые были до колонки, допускаются один раз — при её
-- добавлении: повторный init-db не должен вернуть допуск тем, с кого его сняли. На живой базе
-- то же делает scripts/admission/admission-before-deploy.sql до выката кода.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema() AND table_name = 'users' AND column_name = 'admitted_at') THEN
    ALTER TABLE users ADD COLUMN admitted_at TIMESTAMPTZ;
    UPDATE users SET admitted_at = now();
  END IF;
END $$;

-- Код допуска: у учётки один действующий, новый заменяет прежний. Сам код не хранится — только
-- отпечаток (PBKDF2, как пароль). notify_job_id — задача отправки в Telegram (external-notify).
CREATE TABLE IF NOT EXISTS admission_codes (
  user_id        BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  code_salt      TEXT NOT NULL,
  code_hash      TEXT NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at     TIMESTAMPTZ NOT NULL,
  used_at        TIMESTAMPTZ,
  attempts       INTEGER NOT NULL DEFAULT 0,
  notify_job_id  BIGINT,
  sent_at        TIMESTAMPTZ,
  send_error     TEXT
);

-- =========================================================================
-- Дочерние общества: список «дочка → мать» для фильтра «всё про компанию»
-- =========================================================================
-- Решение владельца 20.10: клик по компании в фильтре показывает и её дочерние общества по цепочке
-- (Газпром → Газпром нефть → Газпромнефть-Заполярье). Список ведут на экране «Дочерние общества»
-- (company_parents.py, db/company_parents_repo.py): правка действует сразу, без выката. Не в справочнике
-- карточки (signal_card_dictionaries.json): его версия входит в рецепт карточки, правка списка там
-- означала бы повторную оплату прогона по истории. Имена как написаны: одна ли это компания (форма
-- собственности, регистр, падеж) решает правило при сверке, поэтому уникального ключа нет; одну мать у
-- дочки и отсутствие циклов держит проверка API под блокировкой таблицы. Учётку автора удалили — пусто.
-- Блок после users (ссылка на неё). На живой базе — этим же CREATE до выката кода, без init-db.
CREATE TABLE IF NOT EXISTS company_parents (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  child_name   TEXT NOT NULL,
  parent_name  TEXT NOT NULL,
  note         TEXT,
  created_by   BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =========================================================================
-- Метрики AI-обработки (для Issue #10)
-- =========================================================================
CREATE TABLE IF NOT EXISTS ai_processing_runs (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id          BIGINT,                        -- фоновая задача-источник (идемпотентность биллинга, баг H1/T2)
  article_id      BIGINT REFERENCES articles(id),
  stage           TEXT NOT NULL,                 -- summary / tagging / scoring / digest
  provider        TEXT NOT NULL DEFAULT 'openai',
  model           TEXT,
  language        TEXT,
  input_tokens    INTEGER DEFAULT 0,
  output_tokens   INTEGER DEFAULT 0,
  total_tokens    INTEGER DEFAULT 0,
  cost_usd        NUMERIC DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'ok',
  error_message   TEXT,
  created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ai_runs_stage_language ON ai_processing_runs(stage, language);
CREATE INDEX IF NOT EXISTS idx_ai_runs_article_id ON ai_processing_runs(article_id);

-- =========================================================================
-- Фоновые задачи API: тяжелые операции не должны блокировать web-request
-- =========================================================================
CREATE TABLE IF NOT EXISTS background_jobs (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id       BIGINT REFERENCES users(id) ON DELETE SET NULL,
  kind          TEXT NOT NULL,                  -- digest_export / process_articles / scrape_source / diagnose_source
  queue_name    TEXT NOT NULL DEFAULT 'default',
  status        TEXT NOT NULL DEFAULT 'queued', -- queued / running / ok / failed
  progress      NUMERIC NOT NULL DEFAULT 0,
  attempts      INTEGER NOT NULL DEFAULT 0,
  max_attempts  INTEGER NOT NULL DEFAULT 3,
  run_after     TIMESTAMPTZ NOT NULL DEFAULT now(),
  payload_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
  result_json   JSONB,
  error_message TEXT,
  execution_region TEXT NOT NULL DEFAULT 'ru',
  capability    TEXT,
  claimed_by    TEXT,
  lease_token_hash TEXT,
  lease_expires_at TIMESTAMPTZ,
  last_heartbeat_at TIMESTAMPTZ,
  ai_started_at TIMESTAMPTZ,               -- локальная AI-обработка сделала первый вызов модели
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_background_jobs_status_created ON background_jobs(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_background_jobs_kind_created ON background_jobs(kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_background_jobs_queue_ready ON background_jobs(queue_name, status, run_after, created_at);
-- ВНИМАНИЕ: индекс по user_id создаётся НИЖЕ, в идемпотентной секции — ПОСЛЕ
-- `ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS user_id`. Здесь (в CREATE-секции)
-- его держать нельзя: на существующей БД `CREATE TABLE IF NOT EXISTS` — no-op, колонки
-- user_id ещё нет → init-db падает `column "user_id" does not exist`.

-- =========================================================================
-- Разведка источников и обратная связь
-- =========================================================================
CREATE TABLE IF NOT EXISTS signal_feedback_events (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  article_id  BIGINT REFERENCES articles(id) ON DELETE CASCADE,
  signal_id   BIGINT,
  signal_evidence_id BIGINT,
  source_url  TEXT,
  signal_title TEXT,
  user_id     BIGINT REFERENCES users(id) ON DELETE SET NULL,
  event_type  TEXT NOT NULL,                  -- added_to_digest / marked_noise / marked_duplicate / tag_changed / score_changed / status_changed / comment_added
  old_value   TEXT,
  new_value   TEXT,
  comment     TEXT,
  verdict     TEXT,
  reason      TEXT,
  corrected_title TEXT,
  corrected_thesis TEXT,
  duplicate_of_signal_id BIGINT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_signal_feedback_article_created ON signal_feedback_events(article_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_feedback_user_created ON signal_feedback_events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_feedback_event_created ON signal_feedback_events(event_type, created_at DESC);

-- =========================================================================
-- Обратная связь человека: оценки + комментарий (требование владельца 12.09)
-- =========================================================================
-- Зачем отдельная таблица, а не signal_feedback_events: та — append-only ЖУРНАЛ
-- («статус сменился с X на Y»), а это ДОКУМЕНТ, который автор правит. Разные формы
-- жизни. Журнал отвечает «что произошло», эта таблица — «что человек об этом думает».
--
-- Поля выбраны не из головы: ровно так заказчик уже пишет ОС руками (чат 10.09) —
-- «1. Корректировка названия… 2. Статья интересная и актуальная. 3. Источник отличный.
-- 4. Перевод: walking island rig → шагающая буровая…». Отсюда три оценки и текст.
--
-- article_id и source_id оба необязательны, но хотя бы один обязан быть: ОС бывает
-- и про конкретный сигнал, и про источник целиком («канал никто не ведёт»).
-- Оценка по источнику проставляется и при ОС о сигнале — тогда source_id берётся
-- из статьи, и накопленное можно свернуть по источнику без джойнов по всей ленте.
CREATE TABLE IF NOT EXISTS feedback_entries (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id        BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  article_id     BIGINT REFERENCES articles(id) ON DELETE CASCADE,
  source_id      BIGINT REFERENCES sources(id) ON DELETE CASCADE,
  reason         TEXT,        -- быстрая причина в один клик (см. FEEDBACK_REASONS)
  usefulness     SMALLINT,    -- 1..5 «полезен ли сигнал»
  translation    SMALLINT,    -- 1..5 «качество перевода и заголовка»
  source_quality SMALLINT,    -- 1..5 «стоит ли держать этот источник»
  comment        TEXT,        -- свободный текст: правки терминов, предостережения
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT feedback_entries_target_not_empty
    CHECK (article_id IS NOT NULL OR source_id IS NOT NULL),
  CONSTRAINT feedback_entries_scores_in_range
    CHECK (
      (usefulness     IS NULL OR usefulness     BETWEEN 1 AND 5) AND
      (translation    IS NULL OR translation    BETWEEN 1 AND 5) AND
      (source_quality IS NULL OR source_quality BETWEEN 1 AND 5)
    )
);
-- Одна карточка ОС на пару «человек × сигнал»: повторное сохранение правит её,
-- а не плодит дубли. Для ОС об источнике без статьи — своя пара.
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_user_article
  ON feedback_entries(user_id, article_id) WHERE article_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_user_source_only
  ON feedback_entries(user_id, source_id) WHERE article_id IS NULL AND source_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_feedback_source_created ON feedback_entries(source_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_reason_created ON feedback_entries(reason, created_at DESC);

CREATE TABLE IF NOT EXISTS source_quality_snapshots (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id           BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  period_from         TIMESTAMPTZ NOT NULL,
  period_to           TIMESTAMPTZ NOT NULL,
  articles_found      INTEGER NOT NULL DEFAULT 0,
  articles_processed  INTEGER NOT NULL DEFAULT 0,
  relevant_count      INTEGER NOT NULL DEFAULT 0,
  rejected_count      INTEGER NOT NULL DEFAULT 0,
  avg_score           NUMERIC,
  digest_count        INTEGER NOT NULL DEFAULT 0,
  duplicate_count     INTEGER NOT NULL DEFAULT 0,
  noise_count         INTEGER NOT NULL DEFAULT 0,
  processing_cost_usd NUMERIC NOT NULL DEFAULT 0,
  quality_score       NUMERIC NOT NULL DEFAULT 0,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_source_quality_source_period ON source_quality_snapshots(source_id, period_to DESC);
CREATE INDEX IF NOT EXISTS idx_source_quality_score ON source_quality_snapshots(quality_score DESC, period_to DESC);

CREATE TABLE IF NOT EXISTS source_candidates (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  url                 TEXT NOT NULL,
  normalized_domain   TEXT NOT NULL,
  name                TEXT,
  candidate_type      TEXT,                    -- newsroom / media / company / rss / blog / unknown
  status              TEXT NOT NULL DEFAULT 'new',
  discovered_by       TEXT NOT NULL DEFAULT 'manual',
  discovery_reason    TEXT,
  topic               TEXT,
  expected_tags_json  JSONB,
  confidence          NUMERIC,
  tested_articles     INTEGER NOT NULL DEFAULT 0,
  relevant_articles   INTEGER NOT NULL DEFAULT 0,
  avg_score           NUMERIC,
  duplicate_count     INTEGER NOT NULL DEFAULT 0,
  noise_count         INTEGER NOT NULL DEFAULT 0,
  recommended_action  TEXT,
  review_comment      TEXT,
  approved_source_id  BIGINT REFERENCES sources(id) ON DELETE SET NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_candidates_url ON source_candidates(url);
CREATE INDEX IF NOT EXISTS idx_source_candidates_status_created ON source_candidates(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_candidates_domain ON source_candidates(normalized_domain);

CREATE TABLE IF NOT EXISTS source_candidate_articles (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  candidate_id        BIGINT NOT NULL REFERENCES source_candidates(id) ON DELETE CASCADE,
  title               TEXT NOT NULL,
  url                 TEXT NOT NULL,
  published_at        TIMESTAMPTZ,
  raw_text            TEXT,
  language            TEXT,
  text_chars          INTEGER NOT NULL DEFAULT 0,
  prefilter_keep      BOOLEAN,
  prefilter_reason    TEXT,
  relevant            BOOLEAN,
  relevance_reason    TEXT,
  relevance_model     TEXT,
  summary             TEXT,
  summary_model       TEXT,
  title_ru            TEXT,
  tag_id              BIGINT REFERENCES tags(id) ON DELETE SET NULL,
  tag_confidence      NUMERIC,
  tag_rationale       TEXT,
  tag_model           TEXT,
  total_score         NUMERIC,
  score_label         TEXT,
  score_explanation   TEXT,
  score_items_json    JSONB,
  score_model         TEXT,
  processing_status   TEXT NOT NULL DEFAULT 'new', -- new / ok / rejected / error
  error_message       TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_candidate_articles_url ON source_candidate_articles(candidate_id, url);
CREATE INDEX IF NOT EXISTS idx_source_candidate_articles_candidate ON source_candidate_articles(candidate_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_candidate_articles_status ON source_candidate_articles(candidate_id, processing_status);

CREATE TABLE IF NOT EXISTS agent_tasks (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind          TEXT NOT NULL,                  -- discover_sources / test_source_candidate / recheck_source_quality / recommend_schedule_changes
  status        TEXT NOT NULL DEFAULT 'planned',
  topic         TEXT,
  payload_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
  result_json   JSONB,
  budget_json   JSONB,
  error_message TEXT,
  started_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_status_created ON agent_tasks(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_kind_created ON agent_tasks(kind, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_runs (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind          TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'running',
  trigger       TEXT,
  payload_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
  result_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_message TEXT,
  started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at   TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status_created ON agent_runs(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_kind_created ON agent_runs(kind, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_actions (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id      BIGINT REFERENCES agent_runs(id) ON DELETE SET NULL,
  task_id     BIGINT REFERENCES agent_tasks(id) ON DELETE CASCADE,
  action_type TEXT NOT NULL,
  input_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
  output_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  cost_usd    NUMERIC NOT NULL DEFAULT 0,
  duration_ms INTEGER,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_actions_run_created ON agent_actions(run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_actions_task_created ON agent_actions(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_actions_type_created ON agent_actions(action_type, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_memory (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  memory_key      TEXT NOT NULL UNIQUE,
  memory_type     TEXT NOT NULL,
  subject         TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'active',
  score           NUMERIC NOT NULL DEFAULT 0,
  facts_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_memory_type_score ON agent_memory(memory_type, score DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_memory_subject ON agent_memory(subject);

-- =========================================================================
-- Радар технологических сигналов
-- =========================================================================
CREATE TABLE IF NOT EXISTS signal_radar_topics (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name              TEXT NOT NULL UNIQUE,
  description       TEXT,
  query_seeds_json  JSONB NOT NULL DEFAULT '[]'::jsonb,
  industry_scope_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  enabled           BOOLEAN NOT NULL DEFAULT TRUE,
  sort_order        INTEGER NOT NULL DEFAULT 0,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS signals (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  signal_key        TEXT NOT NULL UNIQUE,
  title             TEXT NOT NULL,
  title_ru          TEXT,
  theme             TEXT NOT NULL,
  summary           TEXT,
  thesis            TEXT,
  transferability   TEXT,
  maturity          TEXT NOT NULL DEFAULT 'watch', -- reject / watch / shortlist / proven
  confidence        NUMERIC NOT NULL DEFAULT 0,
  score             NUMERIC NOT NULL DEFAULT 0,
  why_now           TEXT,
  why_not_noise     TEXT,
  companies_json    JSONB NOT NULL DEFAULT '[]'::jsonb,
  industries_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
  evidence_count    INTEGER NOT NULL DEFAULT 0,
  first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_signals_maturity_score ON signals(maturity, score DESC, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_theme_seen ON signals(theme, last_seen_at DESC);

-- Материалы коллег из Telegram (бот tg_materials_bot.py на NL): ссылка, файл или событие
-- своими словами и оценка автора по шкале Виктора (те же вердикты, что у карточки радара,
-- signal_feedback._normalize_verdict). Не статья: у материала нет источника в каталоге, и
-- в articles его взяли бы дозагрузка, гейт релевантности и дедуп (тот же довод, что у
-- documents). Карточка сигнала из материала — следующий шаг (signal_id), пока — хранение.
-- Повтор отправки после сбоя связи NL↔ядро не двоит материал: ключ — сообщение бота с
-- карточкой подтверждения (tg_chat_id, tg_message_id).
CREATE TABLE IF NOT EXISTS colleague_materials (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind             TEXT NOT NULL,             -- link / file / event
  title            TEXT NOT NULL,
  description      TEXT NOT NULL,
  url              TEXT,                      -- у link
  companies_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
  verdict          TEXT NOT NULL,
  verdict_comment  TEXT,
  file_name        TEXT,                      -- исходное имя, ТОЛЬКО для показа
  file_path        TEXT,                      -- на томе документов, имя генерируется
  file_size        BIGINT,
  tg_chat_id       BIGINT NOT NULL,
  tg_message_id    BIGINT NOT NULL,
  tg_user_id       BIGINT NOT NULL,
  tg_username      TEXT,
  tg_author        TEXT NOT NULL,             -- имя, как его видно в Telegram
  signal_id        BIGINT REFERENCES signals(id) ON DELETE SET NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_colleague_materials_tg_message
  ON colleague_materials(tg_chat_id, tg_message_id);
CREATE INDEX IF NOT EXISTS idx_colleague_materials_created ON colleague_materials(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_colleague_materials_tg_user ON colleague_materials(tg_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS signal_evidence (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  signal_id         BIGINT REFERENCES signals(id) ON DELETE CASCADE,
  article_id        BIGINT REFERENCES articles(id) ON DELETE SET NULL,
  source_url        TEXT NOT NULL,
  title             TEXT NOT NULL,
  title_ru          TEXT,
  publisher         TEXT,
  published_at      TIMESTAMPTZ,
  evidence_type     TEXT NOT NULL DEFAULT 'article',
  extracted_fact    TEXT,
  summary_ru        TEXT,
  strength          NUMERIC NOT NULL DEFAULT 0,
  raw_payload_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_evidence_url ON signal_evidence(source_url);
CREATE INDEX IF NOT EXISTS idx_signal_evidence_signal ON signal_evidence(signal_id, strength DESC);
CREATE INDEX IF NOT EXISTS idx_signal_evidence_article ON signal_evidence(article_id);

CREATE TABLE IF NOT EXISTS signal_generation_runs (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  background_job_id BIGINT REFERENCES background_jobs(id) ON DELETE SET NULL,
  trigger           TEXT,
  status            TEXT NOT NULL DEFAULT 'running',
  config_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
  result_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_message     TEXT,
  started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at       TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_signal_generation_runs_status_created ON signal_generation_runs(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_generation_runs_job ON signal_generation_runs(background_job_id);

CREATE TABLE IF NOT EXISTS signal_training_examples (
  id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  generation_run_id      BIGINT REFERENCES signal_generation_runs(id) ON DELETE SET NULL,
  signal_id              BIGINT REFERENCES signals(id) ON DELETE SET NULL,
  feedback_event_id      BIGINT REFERENCES signal_feedback_events(id) ON DELETE SET NULL,
  topic                  TEXT NOT NULL,
  signal_key             TEXT,
  pipeline_verdict       TEXT NOT NULL,
  input_json             JSONB NOT NULL DEFAULT '{}'::jsonb,
  raw_output_json        JSONB NOT NULL DEFAULT '{}'::jsonb,
  normalized_output_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_signal_training_examples_run ON signal_training_examples(generation_run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_training_examples_signal ON signal_training_examples(signal_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_training_examples_verdict ON signal_training_examples(pipeline_verdict, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_training_examples_topic ON signal_training_examples(topic);

CREATE TABLE IF NOT EXISTS user_signal_states (
  user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  signal_id       BIGINT NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
  status          TEXT NOT NULL DEFAULT 'watch',  -- watch / digest / archive / noise / duplicate
  analyst_comment TEXT,
  updated_at      TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (user_id, signal_id)
);
CREATE INDEX IF NOT EXISTS idx_user_signal_states_user_status ON user_signal_states(user_id, status);

-- Общие отметки карточек радара (#111): как article_marks; нет строки — «наблюдать».
-- user_signal_states выше — архив личных отметок только для чтения.
CREATE TABLE IF NOT EXISTS signal_marks (
  signal_id       BIGINT PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
  status          TEXT NOT NULL DEFAULT 'watch',  -- watch / digest / archive / noise / duplicate
  analyst_comment TEXT,
  set_by_user_id  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_signal_marks_status ON signal_marks(status);

-- Перенос личных отметок в общие (#111) — запись «сделан», одна строка: повторный запуск ничего
-- не меняет, сверка только чтением берёт отсюда основу и первый открытый месяц на момент переноса.
CREATE TABLE IF NOT EXISTS shared_marks_migration (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  base_user_id   BIGINT NOT NULL,
  open_from      TEXT NOT NULL,  -- «ГГГГ-ММ»: месяцы раньше — закрытые на момент переноса
  report_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
  done_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_shared_marks_migration_once ON shared_marks_migration ((TRUE));

CREATE TABLE IF NOT EXISTS signal_agent_memory (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  memory_key      TEXT NOT NULL UNIQUE,
  memory_type     TEXT NOT NULL,
  subject         TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'active',
  score           NUMERIC NOT NULL DEFAULT 0,
  facts_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_signal_agent_memory_type_score ON signal_agent_memory(memory_type, score DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_agent_memory_subject_hash ON signal_agent_memory((md5(subject)));

-- Idempotent upgrades for databases initialized before these columns existed.
ALTER TABLE article_cards ADD COLUMN IF NOT EXISTS summary_model TEXT;
ALTER TABLE article_cards ADD COLUMN IF NOT EXISTS summary_generated_at TIMESTAMPTZ;
ALTER TABLE article_cards ADD COLUMN IF NOT EXISTS title_ru TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS title_ru TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS summary TEXT;
-- Дедуп радара (18.09): дубль не удаляем, а скрываем со ссылкой на главную карточку.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS merged_into_signal_id BIGINT REFERENCES signals(id) ON DELETE SET NULL;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS merge_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_signals_merged_into ON signals(merged_into_signal_id) WHERE merged_into_signal_id IS NOT NULL;
-- Ревью пачки (21.09): насколько сигнал интересен НА ФОНЕ своей пачки и почему.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS interest_score NUMERIC;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS why_interesting TEXT;
-- Качество радара (замечания Виктора 29.09): поля судьи и архив. Скрытие — в видимости карточки
-- (repository._RADAR_VISIBLE_SQL), карточка при этом хранится. NULL — карточка до правки.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS signal_category TEXT;     -- technology / business / other
ALTER TABLE signals ADD COLUMN IF NOT EXISTS event_date DATE;          -- дата самого события
ALTER TABLE signals ADD COLUMN IF NOT EXISTS mixed_events BOOLEAN;     -- ссылки о разных событиях
ALTER TABLE signals ADD COLUMN IF NOT EXISTS mixed_events_reason TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS score_profile TEXT;       -- чем посчитан score (tech_radar)
ALTER TABLE signals ADD COLUMN IF NOT EXISTS score_items_json JSONB;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS criteria_snapshot JSONB;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS archive_reason TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS oilfield_relevance TEXT;  -- direct / transferable / none
ALTER TABLE signals ADD COLUMN IF NOT EXISTS oilfield_application TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS filter_stage TEXT;   -- search / judge / review / judge_error
ALTER TABLE signals ADD COLUMN IF NOT EXISTS filter_reason TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS verdict_reason TEXT;     -- почему такое решение (судья)
ALTER TABLE signals ADD COLUMN IF NOT EXISTS decision_log JSONB;      -- ход решения: [{stage, text}]
-- Единое хранилище сигналов (#95, ADR 0003): сигнал ленты — принятая отбором статья
-- (origin_article_id — её главная публикация), находка поиска — радар. Прежние — радар.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'radar';  -- radar / feed
ALTER TABLE signals ADD COLUMN IF NOT EXISTS origin_article_id BIGINT REFERENCES articles(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_signals_origin_article ON signals(origin_article_id) WHERE origin_article_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_signals_origin_category ON signals(origin, signal_category);
-- Карточка сигнала (#96, ADR 0003): атрибуты агента «Карточка сигнала». Зрелость технологии —
-- technology_maturity: maturity (watch / shortlist / proven) — подтверждённость радара, не зрелость.
-- Дата события, связь с нефтесервисом, применение и «почему важно» — прежние поля судьи радара.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS event_type TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS region TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS technology_class TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS technology_maturity TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS participants_json JSONB;  -- [{name, quote, verified}]
ALTER TABLE signals ADD COLUMN IF NOT EXISTS event_date_source TEXT;   -- из текста / по дате публикации / не определено
ALTER TABLE signals ADD COLUMN IF NOT EXISTS card_set_by JSONB;        -- {поле: agent / radar_judge / human}
ALTER TABLE signals ADD COLUMN IF NOT EXISTS card_recipe JSONB;        -- инструкция, модель, справочники, время
-- Кто поставил значения судьи у находок радара до #96 (новые пишет upsert_signal). Повторный прогон
-- строк не находит: после него card_set_by у находок радара не пуст.
UPDATE signals SET card_set_by = jsonb_strip_nulls(jsonb_build_object(
    'event_date', CASE WHEN event_date IS NOT NULL THEN 'radar_judge' END,
    'oilfield_relevance', CASE WHEN oilfield_relevance IS NOT NULL THEN 'radar_judge' END,
    'oilfield_application', CASE WHEN COALESCE(oilfield_application, '') <> '' THEN 'radar_judge' END,
    'why_interesting', CASE WHEN COALESCE(why_interesting, '') <> '' THEN 'radar_judge' END))
WHERE origin = 'radar' AND card_set_by IS NULL;
-- Агент «Отбор» (#130, ADR 0005): единственный решает «брать или нет». Решение — у сигнала: отрасль, связь и
-- применение (прежние поля связи, источник «otbor» в card_set_by), категория, код причины отказа; у отклонённой
-- maturity = 'reject' («Решение платформы»). Нет решения — сбой: технический (шаг повторит) или ответ не по
-- правилам (без повтора). Отклонённое и сбой — разделы для админа (db/rejected_sections.py).
ALTER TABLE signals ADD COLUMN IF NOT EXISTS industry TEXT;              -- oilfield_services / oil_gas / other_industry
ALTER TABLE signals ADD COLUMN IF NOT EXISTS otbor_decision TEXT;        -- accept / reject; NULL — «Отбор» не решал
ALTER TABLE signals ADD COLUMN IF NOT EXISTS reject_reason TEXT;         -- код причины отказа (otbor_dictionaries.json)
ALTER TABLE signals ADD COLUMN IF NOT EXISTS otbor_disputed TEXT;        -- пометка «спорное»: почему
ALTER TABLE signals ADD COLUMN IF NOT EXISTS otbor_failure TEXT;         -- technical / invalid_answer, пока решения нет
ALTER TABLE signals ADD COLUMN IF NOT EXISTS otbor_failure_reason TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS otbor_recipe JSONB;         -- инструкция, модель, справочники, время, задача
-- Статью, вставленную при включённом «Отборе» (OTBOR_ENABLED), решает он, а не гейт ленты: пока решения нет,
-- её не видно (feed_window.visible_sql) и обработка ленты её не берёт.
ALTER TABLE articles ADD COLUMN IF NOT EXISTS selector TEXT;             -- otbor; NULL — гейт ленты
CREATE INDEX IF NOT EXISTS idx_articles_otbor ON articles(collected_at) WHERE selector = 'otbor';
-- Радар и материалы коллег через «Отбор» (#131): находку поиска и материал коллеги, заведённые при включённом
-- флаге, решает он (signals.selector = 'otbor'); пока решения нет, сигнал не виден нигде. Материал коллеги —
-- сигнал происхождения material; какой итог «Отбора» автору уже сообщил бот — notified_outcome; оценка автора —
-- блок ОС с его именем из Telegram (author).
ALTER TABLE signals ADD COLUMN IF NOT EXISTS selector TEXT;              -- otbor; NULL — прежние правила
CREATE INDEX IF NOT EXISTS idx_signals_otbor_waiting ON signals(created_at)
  WHERE selector = 'otbor' AND otbor_decision IS NULL;
ALTER TABLE colleague_materials ADD COLUMN IF NOT EXISTS notified_outcome TEXT;
ALTER TABLE colleague_materials ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
ALTER TABLE signal_feedback_events ADD COLUMN IF NOT EXISTS author TEXT; -- кто оставил блок, если не учётка
-- «Карточка» в конвейере «Отбора» (#132): дата события с точностью (у месяца и года — первый день периода, как
-- период её читает только код с точностью); возврат в «Отбор», если «Карточка» нашла в тексте дату события старше
-- 180 дней (один раз; что нашла — во входе «Отбора»); баллы второго вызова по профилю категории: ступень, рецепт с
-- итогами в двух вариантах подмешивания ключевых слов; «не тот блок» — баллы пересчитываются. У «другого»
-- «Решения платформы» нет: maturity может быть пусто.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS event_date_precision TEXT;  -- day / month / year; NULL — день (до #132)
ALTER TABLE signals ADD COLUMN IF NOT EXISTS otbor_returns INTEGER NOT NULL DEFAULT 0;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS otbor_return_fact TEXT;    -- что нашла «Карточка»: во вход «Отбора»
ALTER TABLE signals ADD COLUMN IF NOT EXISTS score_band TEXT;           -- strong / useful / watch / weak
ALTER TABLE signals ADD COLUMN IF NOT EXISTS scores_recipe JSONB;       -- второй вызов: инструкция, модель, итоги
ALTER TABLE signals ADD COLUMN IF NOT EXISTS scores_stale_since TIMESTAMPTZ;  -- «не тот блок»: пересчитываются
-- Когда публикация пошла дальше «Отбора» (его решение «брать» или возврат человеком): от этого считают окна шаги
-- конвейера (суть, оба вызова «Карточки»); NULL — решённое до «Отбора», окна от заведения сигнала.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ;
ALTER TABLE signals ALTER COLUMN maturity DROP NOT NULL;
ALTER TABLE signal_evidence ADD COLUMN IF NOT EXISTS title_ru TEXT;
ALTER TABLE signal_evidence ADD COLUMN IF NOT EXISTS summary_ru TEXT;
ALTER TABLE signal_feedback_events ALTER COLUMN article_id DROP NOT NULL;
ALTER TABLE signal_feedback_events ADD COLUMN IF NOT EXISTS signal_id BIGINT REFERENCES signals(id) ON DELETE CASCADE;
ALTER TABLE signal_feedback_events ADD COLUMN IF NOT EXISTS signal_evidence_id BIGINT REFERENCES signal_evidence(id) ON DELETE SET NULL;
ALTER TABLE signal_feedback_events ADD COLUMN IF NOT EXISTS source_url TEXT;
ALTER TABLE signal_feedback_events ADD COLUMN IF NOT EXISTS signal_title TEXT;
ALTER TABLE signal_feedback_events ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE signal_feedback_events ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE signal_feedback_events ADD COLUMN IF NOT EXISTS corrected_title TEXT;
ALTER TABLE signal_feedback_events ADD COLUMN IF NOT EXISTS corrected_thesis TEXT;
ALTER TABLE signal_feedback_events ADD COLUMN IF NOT EXISTS duplicate_of_signal_id BIGINT REFERENCES signals(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_signal_feedback_signal_created ON signal_feedback_events(signal_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_feedback_url_created ON signal_feedback_events(source_url, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signal_feedback_verdict_created ON signal_feedback_events(verdict, created_at DESC);
INSERT INTO signal_agent_memory
  (memory_key, memory_type, subject, status, score, facts_json, last_seen_at, created_at, updated_at)
SELECT memory_key, memory_type, subject, status, score, facts_json, last_seen_at, created_at, updated_at
FROM agent_memory
WHERE memory_type LIKE 'signal\_%' ESCAPE '\'
ON CONFLICT (memory_key) DO UPDATE SET
  memory_type = EXCLUDED.memory_type,
  subject = EXCLUDED.subject,
  status = EXCLUDED.status,
  score = EXCLUDED.score,
  facts_json = EXCLUDED.facts_json,
  last_seen_at = EXCLUDED.last_seen_at,
  updated_at = now();
DELETE FROM agent_memory WHERE memory_type LIKE 'signal\_%' ESCAPE '\';
ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user';
ALTER TABLE article_scores ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE tags ADD COLUMN IF NOT EXISTS name_en TEXT;
ALTER TABLE tags ADD COLUMN IF NOT EXISTS keywords_en_json JSONB;
ALTER TABLE article_tags ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS update_frequency TEXT;
ALTER TABLE article_cards ADD COLUMN IF NOT EXISTS relevant BOOLEAN;
ALTER TABLE article_cards ADD COLUMN IF NOT EXISTS relevance_reason TEXT;
ALTER TABLE article_cards ADD COLUMN IF NOT EXISTS relevance_model TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS text_truncated BOOLEAN DEFAULT FALSE;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS full_text_fetched_at TIMESTAMPTZ;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS full_text_status TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS full_text_error TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS extraction_method TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS image_url TEXT;
-- Мягкое удаление: recheck в режиме --mark помечает нерелевантные сюда (не удаляя
-- физически), затем разовый recheck-purge удаляет помеченные (или recheck-unmark вернёт).
ALTER TABLE articles ADD COLUMN IF NOT EXISTS pending_deletion BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS deletion_reason TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS marked_for_deletion_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_articles_pending_deletion ON articles(pending_deletion) WHERE pending_deletion;

-- =========================================================================
-- url_key: тождество статьи по адресу (13.09)
-- =========================================================================
-- Уникальность держалась на СЫРОМ url, и один материал заводился по нескольку раз:
-- замер прода 13.09 — за 90 дней 940 лишних статей, три причины поимённо:
--   ?from=main_lines_11 против ?from=newsfeed (РБК)   — query-хвосты
--   http:// против https://            (Ростех)        — схема
--   /topics/x против /topics/x/        (Wood Mackenzie)— хвостовой слэш
-- Каждая копия проходила полный ИИ-конвейер заново и занимала отдельную карточку —
-- ровно то, на что жаловался заказчик 08.09 («все 4 новости об одном»).
-- Ключ = host+path без схемы, www и слэша + значимый query (normalize.url_key).
-- 25.09: query сначала срезался целиком и склеил все статьи сайтов, где номер статьи
-- живёт в query (Минэнерго ?news-item=, EIA ?id=, Лукойл ?rid=, Новатэк ?id_4=).
-- Теперь срезаются только трекинговые параметры — тот же список, что в Python.
ALTER TABLE articles ADD COLUMN IF NOT EXISTS url_key TEXT;

-- Бэкфилл: считаем тем же правилом, что и Python-функция (тест сверяет до символа).
UPDATE articles
SET url_key = rtrim(regexp_replace(regexp_replace(lower(regexp_replace(url, '^\s+|\s+$', '', 'g')), '^https?://(www\.)?', ''), '[?#].*$', ''), '/')
    || COALESCE('?' || (
         SELECT string_agg(p, '&' ORDER BY p COLLATE "C")
         FROM regexp_split_to_table(
                substring(split_part(lower(regexp_replace(url, '^\s+|\s+$', '', 'g')), '#', 1) from '\?(.*)$'), '&') AS p
         WHERE p <> ''
           AND split_part(p, '=', 1) NOT IN ('from', 'ysclid', 'yclid', 'ymclid', 'fbclid', 'gclid',
                                             'igshid', '_openstat', 'mc_cid', 'mc_eid')
           AND split_part(p, '=', 1) NOT LIKE 'utm\_%'
           AND split_part(p, '=', 1) NOT LIKE 'gaa\_%'
       ), '')
WHERE url_key IS NULL;

-- Схлопывание УЖЕ накопленных дублей: оставляем самую полную копию (длиннее тело,
-- при равенстве — раньше пришедшую), остальные прячем через существующий мягкий
-- механизм pending_deletion. НЕ удаляем: вместе со статьёй ушла бы история ИИ-затрат.
UPDATE articles a
SET pending_deletion = TRUE
FROM (
  SELECT id FROM (
    SELECT id, row_number() OVER (
             PARTITION BY url_key
             ORDER BY length(COALESCE(raw_text,'')) DESC, id
           ) AS rn
    FROM articles WHERE url_key IS NOT NULL AND NOT pending_deletion
  ) t WHERE rn > 1
) dup
WHERE a.id = dup.id AND NOT a.pending_deletion;

-- Уникальность ЧАСТИЧНАЯ: скрытые копии не мешают, а новая вставка с тем же ключом
-- отбивается. ON CONFLICT в insert_article целится ровно в этот индекс.
CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_url_key
  ON articles(url_key) WHERE url_key IS NOT NULL AND NOT pending_deletion;
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS queue_name TEXT NOT NULL DEFAULT 'default';
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS user_id BIGINT;
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS run_after TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS execution_region TEXT NOT NULL DEFAULT 'ru';
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS capability TEXT;
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS claimed_by TEXT;
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS lease_token_hash TEXT;
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ;
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS ai_started_at TIMESTAMPTZ;
ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS agent_run_id BIGINT;
ALTER TABLE agent_actions ADD COLUMN IF NOT EXISTS run_id BIGINT;
DO $$
BEGIN
  ALTER TABLE background_jobs
    ADD CONSTRAINT background_jobs_agent_run_id_fkey
    FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE SET NULL;
EXCEPTION
  WHEN duplicate_object THEN NULL;
END $$;
DO $$
BEGIN
  ALTER TABLE agent_actions
    ADD CONSTRAINT agent_actions_run_id_fkey
    FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE SET NULL;
EXCEPTION
  WHEN duplicate_object THEN NULL;
END $$;
CREATE INDEX IF NOT EXISTS idx_background_jobs_agent_run_created ON background_jobs(agent_run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_actions_run_created ON agent_actions(run_id, created_at DESC);
UPDATE background_jobs bj
SET user_id = u.id
FROM users u
WHERE bj.user_id IS NULL
  AND bj.payload_json ? 'user_id'
  AND (bj.payload_json->>'user_id') ~ '^[0-9]+$'
  AND u.id = (bj.payload_json->>'user_id')::BIGINT;
UPDATE background_jobs bj
SET user_id = NULL
WHERE bj.user_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = bj.user_id);
DO $$
BEGIN
  ALTER TABLE background_jobs
    ADD CONSTRAINT background_jobs_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;
EXCEPTION
  WHEN duplicate_object THEN NULL;
END $$;
CREATE INDEX IF NOT EXISTS idx_background_jobs_external_ready ON background_jobs(execution_region, queue_name, status, run_after, created_at);
CREATE INDEX IF NOT EXISTS idx_background_jobs_lease_expires ON background_jobs(status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_background_jobs_user_created ON background_jobs(user_id, created_at DESC);
-- Потребители внешних очередей — контейнеры NL (23.09, контракт версий, contract.py):
-- какую сборку и номер контракта каждый сообщил при последнем claim. 18.09 и 21.09
-- «пересобран ли NL» выясняли по косвенным полям; теперь расхождение — тревога сторожа.
CREATE TABLE IF NOT EXISTS external_worker_consumers (
  consumer       TEXT PRIMARY KEY,           -- EXTERNAL_WORKER_ID без #потока
  queues         TEXT[] NOT NULL DEFAULT '{}',
  build          TEXT,                       -- git SHA образа; NULL — сборка до контракта
  contract       INTEGER,                    -- NULL — воркер номер не прислал
  first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Идемпотентность биллинга AI (баг H1/T2): один (job_id, article_id, stage) — одна строка.
-- Повторное применение результата задачи (ретрай/переотдача воркера) НЕ двоит ai_processing_runs
-- → нет двойного счёта OpenAI. NULL job_id (локальный путь) и NULL article_id (дайджест) не
-- дедуплицируются (NULL-ы различны в UNIQUE) — вставляются как раньше.
ALTER TABLE ai_processing_runs ADD COLUMN IF NOT EXISTS job_id BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_runs_job_article_stage ON ai_processing_runs(job_id, article_id, stage);

-- =========================================================================
-- Загруженные документы (фича «приём файлов»)
-- =========================================================================
-- ДОКУМЕНТ — НЕ СТАТЬЯ, и это главное решение здесь. У документа нет источника,
-- нет URL и нет даты публикации в ленте. Положить его в articles через синтетический
-- источник значило бы отдать его 44 запросам репозитория по articles: дозагрузка
-- полного текста пошла бы качать несуществующий адрес, гейт релевантности мог бы
-- тихо скрыть документ пользователя, дедуп и массовые скрытия захватили бы его заодно.
CREATE TABLE IF NOT EXISTS documents (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_user_id     BIGINT NOT NULL REFERENCES users(id),
  filename          TEXT NOT NULL,            -- исходное имя, ТОЛЬКО для показа
  storage_path      TEXT NOT NULL,            -- путь на общем томе, имя генерируется
  kind              TEXT NOT NULL,            -- pdf / pptx / docx, определён по сигнатуре
  size_bytes        BIGINT NOT NULL,
  content_sha256    TEXT NOT NULL,            -- дедуп повторной загрузки в пределах владельца
  anchor_unit       TEXT,                     -- страница / слайд / блок
  anchor_count      INTEGER,
  text_chars        INTEGER,
  status            TEXT NOT NULL DEFAULT 'uploaded',  -- uploaded / parsed / processing / ready / failed
  error_message     TEXT,
  -- Аттестация: ответственность за класс материала на загрузившем. Штампуется НА КАЖДУЮ
  -- загрузку, а не разово в профиле: флаг в профиле, поставленный в марте, к ноябрьской
  -- загрузке отношения не имеет и ничего не доказывает.
  attested_at       TIMESTAMPTZ NOT NULL,
  attestation_text  TEXT NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_documents_owner_created ON documents(owner_user_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_owner_sha ON documents(owner_user_id, content_sha256);

-- Текст по якорям. Отдельной таблицей, а не JSON в documents: по ней ищет проверяльщик
-- фактов — «есть ли это число на той странице, на которую сослалась модель».
CREATE TABLE IF NOT EXISTS document_anchors (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id   BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  number        INTEGER NOT NULL,
  text          TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_document_anchors_doc_number ON document_anchors(document_id, number);

-- Карточка документа. Зеркало пары articles / article_cards.
CREATE TABLE IF NOT EXISTS document_cards (
  document_id     BIGINT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  doc_type        TEXT,      -- отчёт / презентация / статья / КП / иное
  publisher       TEXT,
  doc_date        TEXT,      -- как в документе, строкой: датой бывает «II квартал 2025»
  date_source     TEXT,      -- документ / имя файла / нет — чему доверять (тикет #50)
  language        TEXT,
  essence         TEXT,      -- СУТЬ: что это за документ и зачем
  summary_json    JSONB,     -- СВОДКА: пункты по разделам
  claims_json     JSONB,     -- заявления документа, не подтверждённые фактами
  model           TEXT,
  generated_at    TIMESTAMPTZ
);

-- Извлечённые числа. verified проставляет КОД, сверяя значение с текстом якоря,
-- а не модель о себе. Неподтверждённый факт хранится и показывается с пометкой.
-- Дата документа часто есть только в НАЗВАНИИ файла: на проде 13.09 два документа
-- из четырёх получили «дата: не указано» при дате в имени. Пометка обязательна —
-- название мог поменять кто угодно, и это менее надёжно, чем дата из текста.
ALTER TABLE document_cards ADD COLUMN IF NOT EXISTS date_source TEXT;

CREATE TABLE IF NOT EXISTS document_facts (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id   BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  value         TEXT NOT NULL,
  unit          TEXT,
  context       TEXT,        -- на языке оригинала: иначе сверка с текстом якоря не сойдётся
  anchor        INTEGER,
  verified      BOOLEAN NOT NULL DEFAULT FALSE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_document_facts_doc ON document_facts(document_id);

-- Учёт расходов на разбор документов. ai_processing_runs привязана к статье внешним
-- ключом, и дедуп биллинга держится на UNIQUE (job_id, article_id, stage). Для документа
-- article_id пуст, а NULL-ы в UNIQUE не конфликтуют — значит существующий индекс защиты
-- НЕ даёт, и повторное применение результата удвоило бы счёт (баг H1/T2 заново).
-- Поэтому отдельная колонка и СВОЙ ЧАСТИЧНЫЙ уникальный индекс для строк по документам.
-- Существующий индекс по статьям НЕ трогаем: он корректен для своих строк, а лишний
-- DROP на проде ночью — риск без выигрыша.
ALTER TABLE ai_processing_runs ADD COLUMN IF NOT EXISTS document_id BIGINT REFERENCES documents(id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_runs_job_document_stage
  ON ai_processing_runs(job_id, document_id, stage) WHERE document_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ai_runs_document_id ON ai_processing_runs(document_id);

-- Учёт расходов агента «Карточка сигнала» (#96): строка на (задача, сигнал, стадия). Статьи у
-- вызова нет — как у документов, свой частичный уникальный индекс: повторное применение итога
-- той же задачи не удваивает счёт.
ALTER TABLE ai_processing_runs ADD COLUMN IF NOT EXISTS signal_id BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_runs_job_signal_stage
  ON ai_processing_runs(job_id, signal_id, stage) WHERE signal_id IS NOT NULL;
