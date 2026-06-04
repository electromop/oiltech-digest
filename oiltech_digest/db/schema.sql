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
  parse_strategy TEXT,                           -- rss / request / telegram / none
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
CREATE INDEX IF NOT EXISTS idx_sources_last_seen_published_at ON sources(last_seen_published_at DESC);

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
  month       TEXT NOT NULL,                  -- YYYY-MM
  title       TEXT,
  status      TEXT DEFAULT 'draft',
  created_at  TIMESTAMPTZ DEFAULT now(),
  updated_at  TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_monthly_digests_month ON monthly_digests(month);

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
  created_at     TIMESTAMPTZ DEFAULT now(),
  updated_at     TIMESTAMPTZ DEFAULT now()
);

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

-- Idempotent upgrades for databases initialized before these columns existed.
ALTER TABLE article_cards ADD COLUMN IF NOT EXISTS summary_model TEXT;
ALTER TABLE article_cards ADD COLUMN IF NOT EXISTS summary_generated_at TIMESTAMPTZ;
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
