-- Проверка «Отбора» на реальных данных (#134): таблица решений рядом, ДО выката кода, который в неё пишет.
--
-- Только добавление, повторный прогон безопасен (IF NOT EXISTS), заполнений нет. Замков на живых таблицах нет:
-- новая таблица ссылается на articles и signals, CREATE TABLE берёт на них только SHARE ROW EXCLUSIVE на миг.
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/signals/otbor-shadow-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
CREATE TABLE IF NOT EXISTS otbor_shadow_decisions (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  article_id     BIGINT REFERENCES articles(id) ON DELETE CASCADE,
  signal_id      BIGINT REFERENCES signals(id) ON DELETE CASCADE,
  trigger        TEXT NOT NULL,
  decision       TEXT,
  values_json    JSONB,
  disputed       TEXT,
  failure        TEXT,
  failure_reason TEXT,
  decision_log   JSONB NOT NULL DEFAULT '[]'::jsonb,
  recipe         JSONB NOT NULL,
  gate_relevant  BOOLEAN,
  switched_at    TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT otbor_shadow_one_publication CHECK ((article_id IS NULL) <> (signal_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_otbor_shadow_article
  ON otbor_shadow_decisions(article_id) WHERE article_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_otbor_shadow_signal
  ON otbor_shadow_decisions(signal_id) WHERE signal_id IS NOT NULL;
COMMIT;
