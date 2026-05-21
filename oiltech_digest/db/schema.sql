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
  category       TEXT,
  priority       NUMERIC DEFAULT 1.0,            -- из Excel «Рейтинг источника» (1..3)
  last_parsed_at TIMESTAMPTZ,
  created_at     TIMESTAMPTZ DEFAULT now(),
  updated_at     TIMESTAMPTZ DEFAULT now()
);
-- Естественный ключ: бренд может иметь несколько каналов (сайт + Telegram) с одним
-- именем, но разным типом — это разные источники. Поэтому уникальность по (name, source_type).
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_name_type ON sources(name, source_type);
CREATE INDEX IF NOT EXISTS idx_sources_enabled_strategy ON sources(enabled, parse_strategy);

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
  status              TEXT DEFAULT 'new',         -- new / review / digest / archive
  selected_for_digest BOOLEAN DEFAULT FALSE,
  digest_month        TEXT,
  analyst_comment     TEXT,
  created_at          TIMESTAMPTZ DEFAULT now(),
  updated_at          TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_article_cards_article_id ON article_cards(article_id);

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
  total_score  NUMERIC NOT NULL,
  score_label  TEXT,
  explanation  TEXT,
  created_at   TIMESTAMPTZ DEFAULT now(),
  updated_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_article_scores_article_id ON article_scores(article_id);

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
  description   TEXT,
  keywords_json JSONB,
  enabled       BOOLEAN DEFAULT TRUE,
  sort_order    INTEGER DEFAULT 0,
  created_at    TIMESTAMPTZ DEFAULT now(),
  updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS article_tags (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  article_id  BIGINT NOT NULL REFERENCES articles(id),
  tag_id      BIGINT NOT NULL REFERENCES tags(id),
  confidence  NUMERIC,
  rationale   TEXT,
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_article_tags_article_id ON article_tags(article_id);
CREATE INDEX IF NOT EXISTS idx_article_tags_tag_id ON article_tags(tag_id);

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
