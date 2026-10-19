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

COMMIT;
