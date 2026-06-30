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
  full_text_status TEXT,                          -- ok / failed / too_short / blocked / paywall
  full_text_error TEXT,
  extraction_method TEXT,                         -- rss / lxml / trafilatura / selector
  language       TEXT,
  content_hash   TEXT,
  created_at     TIMESTAMPTZ DEFAULT now(),
  updated_at     TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_url ON articles(url);
CREATE INDEX IF NOT EXISTS idx_articles_content_hash ON articles(content_hash);
CREATE INDEX IF NOT EXISTS idx_articles_source_id ON articles(source_id);
CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at DESC);

-- =========================================================================
-- Карточки статей (рабочее представление в «Все статьи») — будущее
-- =========================================================================
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
  status              TEXT DEFAULT 'new',         -- new / review / digest / archive / rejected
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_scoring_criteria_name ON scoring_criteria(name);

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

-- Личное состояние пользователя: свои статусы статей и свой дайджест (срез на юзера).
-- Сами статьи/AI/теги/скоринг/источники — общие; пер-юзерный только рабочий статус.
CREATE TABLE IF NOT EXISTS user_article_states (
  user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  article_id      BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  status          TEXT NOT NULL DEFAULT 'new',  -- new / review / digest / archive
  analyst_comment TEXT,
  updated_at      TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (user_id, article_id)
);
CREATE INDEX IF NOT EXISTS idx_user_article_states_user_status ON user_article_states(user_id, status);

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

-- =========================================================================
-- Метрики AI-обработки (для Issue #10)
-- =========================================================================
CREATE TABLE IF NOT EXISTS ai_processing_runs (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at    TIMESTAMPTZ,
  finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_background_jobs_status_created ON background_jobs(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_background_jobs_kind_created ON background_jobs(kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_background_jobs_queue_ready ON background_jobs(queue_name, status, run_after, created_at);
CREATE INDEX IF NOT EXISTS idx_background_jobs_user_created ON background_jobs(user_id, created_at DESC);

-- Idempotent upgrades for databases initialized before these columns existed.
ALTER TABLE article_cards ADD COLUMN IF NOT EXISTS summary_model TEXT;
ALTER TABLE article_cards ADD COLUMN IF NOT EXISTS summary_generated_at TIMESTAMPTZ;
ALTER TABLE article_cards ADD COLUMN IF NOT EXISTS title_ru TEXT;
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
