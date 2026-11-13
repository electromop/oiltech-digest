-- Контур ОС (#189): журнал ОС, недельный срез, разбор ОС, настройки контура: схема ДО выката кода ядра, который её читает.
--
-- Только добавления, повторный прогон безопасен (IF NOT EXISTS, строка seed одна). Таблицы не переписываются: у новой
-- колонки correction_undone_at нет значения по умолчанию. Строка seed: начало окна первого недельного среза, выкат #188
-- (ядро 13.11.2026 07:55 UTC): первый срез берёт отметки с этого момента, раньше их правки ложились сразу.
-- lock_timeout: ALTER берёт на signal_feedback_events эксклюзивный замок; если его держит долгий запрос, шаг откажет через
-- 5 с, ничего не изменив: повторить позже. Окно: не во время радара (00:00-02:30 МСК).
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/signals/os-journal-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
CREATE TABLE IF NOT EXISTS feedback_slices (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  trigger       TEXT NOT NULL,
  status        TEXT NOT NULL,
  window_from   TIMESTAMPTZ NOT NULL,
  window_to     TIMESTAMPTZ NOT NULL,
  week_start    DATE,
  job_id        BIGINT REFERENCES background_jobs(id) ON DELETE SET NULL,
  signals       INTEGER NOT NULL DEFAULT 0,
  proposals     INTEGER NOT NULL DEFAULT 0,
  note          TEXT,
  author        TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at   TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_slices_week ON feedback_slices(week_start) WHERE trigger = 'schedule';
CREATE INDEX IF NOT EXISTS idx_feedback_slices_created ON feedback_slices(created_at DESC);
CREATE TABLE IF NOT EXISTS feedback_actions (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind          TEXT NOT NULL,
  status        TEXT NOT NULL,
  signal_id     BIGINT REFERENCES signals(id) ON DELETE CASCADE,
  parent_id     BIGINT REFERENCES feedback_actions(id) ON DELETE CASCADE,
  slice_id      BIGINT REFERENCES feedback_slices(id) ON DELETE SET NULL,
  event_ids     BIGINT[] NOT NULL DEFAULT '{}',
  quote         TEXT,
  agents        TEXT[] NOT NULL DEFAULT '{}',
  details       JSONB NOT NULL DEFAULT '{}'::jsonb,
  author        TEXT,
  user_id       BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_feedback_actions_created ON feedback_actions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_actions_signal ON feedback_actions(signal_id, kind, status);
CREATE INDEX IF NOT EXISTS idx_feedback_actions_parent ON feedback_actions(parent_id);
CREATE INDEX IF NOT EXISTS idx_feedback_actions_events ON feedback_actions USING gin(event_ids);
CREATE TABLE IF NOT EXISTS feedback_settings (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  values_json   JSONB NOT NULL,
  author        TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS signal_event_pairs (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  signal_id        BIGINT NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
  other_signal_id  BIGINT NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
  same_event       BOOLEAN NOT NULL,
  source           TEXT NOT NULL,
  action_id        BIGINT REFERENCES feedback_actions(id) ON DELETE SET NULL,
  author           TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_signal_event_pairs_signal ON signal_event_pairs(signal_id);
CREATE INDEX IF NOT EXISTS idx_signal_event_pairs_other ON signal_event_pairs(other_signal_id);
-- Отмена правки заголовка и сути из журнала ОС.
ALTER TABLE signal_feedback_events ADD COLUMN IF NOT EXISTS correction_undone_at TIMESTAMPTZ;
-- Начало окна первого среза: выкат #188.
INSERT INTO feedback_slices (trigger, status, window_from, window_to, note, finished_at)
SELECT 'seed', 'done', '2026-11-13 07:55:00+00', '2026-11-13 07:55:00+00',
       'начало окна первого среза: выкат одной формы ОС (#188)', now()
WHERE NOT EXISTS (SELECT 1 FROM feedback_slices WHERE trigger = 'seed');
COMMIT;
