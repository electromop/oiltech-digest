-- Общие отметки (#111): новые таблицы ДО выката кода — без полного прогона schema.sql на живой
-- базе (в нём массовые UPDATE). То же, что в schema.sql; повтор безопасен.
-- Выкат — scripts/marks/shared-marks-runbook.md.
BEGIN;

CREATE TABLE IF NOT EXISTS article_marks (
  article_id      BIGINT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  status          TEXT NOT NULL DEFAULT 'new',
  analyst_comment TEXT,
  set_by_user_id  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_article_marks_status ON article_marks(status);

CREATE TABLE IF NOT EXISTS signal_marks (
  signal_id       BIGINT PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
  status          TEXT NOT NULL DEFAULT 'watch',
  analyst_comment TEXT,
  set_by_user_id  BIGINT REFERENCES users(id) ON DELETE SET NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_signal_marks_status ON signal_marks(status);

COMMIT;
