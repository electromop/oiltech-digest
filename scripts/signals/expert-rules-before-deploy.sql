-- Правила эксперта и автопроверка (#190, спека #187, ADR 0007): схема ДО выката кода ядра, который её читает, и первое
-- предложение правила в журнале ОС.
--
-- Только добавления, повторный прогон безопасен (IF NOT EXISTS, предложение одно). Таблицы не переписываются: у новой
-- колонки ai_processing_runs.recipe нет значения по умолчанию. Первое предложение: три подсказки толкования для стадии
-- «Карточки» из правил замен (решение 6 гриля, разбор 53 правил 12.11): его автопроверку поставит первый цикл
-- планировщика после выката ядра.
-- lock_timeout: ALTER берёт на ai_processing_runs эксклюзивный замок; если его держит долгий запрос, шаг откажет через
-- 5 с, ничего не изменив: повторить позже. Окно: не во время радара (00:00-02:30 МСК).
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/signals/expert-rules-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
CREATE TABLE IF NOT EXISTS expert_rules (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agent         TEXT NOT NULL,
  step          TEXT NOT NULL,
  category      TEXT,
  topic         TEXT,
  text          TEXT NOT NULL,
  replaces      BIGINT[] NOT NULL DEFAULT '{}',
  event_ids     BIGINT[] NOT NULL DEFAULT '{}',
  quote         TEXT,
  action_id     BIGINT REFERENCES feedback_actions(id) ON DELETE SET NULL,
  approved_by   TEXT,
  user_id       BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS expert_rule_sets (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agent         TEXT NOT NULL,
  version       INTEGER NOT NULL,
  rule_ids      BIGINT[] NOT NULL DEFAULT '{}',
  previous_id   BIGINT REFERENCES expert_rule_sets(id),
  current       BOOLEAN NOT NULL DEFAULT FALSE,
  action_id     BIGINT REFERENCES feedback_actions(id) ON DELETE SET NULL,
  author        TEXT,
  user_id       BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (agent, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_expert_rule_sets_current ON expert_rule_sets(agent) WHERE current;
CREATE TABLE IF NOT EXISTS rule_checks (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  action_id     BIGINT REFERENCES feedback_actions(id) ON DELETE CASCADE,
  mode          TEXT NOT NULL,
  agent         TEXT NOT NULL,
  status        TEXT NOT NULL,
  job_id        BIGINT REFERENCES background_jobs(id) ON DELETE SET NULL,
  row_count     INTEGER NOT NULL DEFAULT 0,
  toward        INTEGER NOT NULL DEFAULT 0,
  away          INTEGER NOT NULL DEFAULT 0,
  unchanged     INTEGER NOT NULL DEFAULT 0,
  advice        TEXT,
  note          TEXT,
  details       JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_rule_checks_action ON rule_checks(action_id, id DESC);
ALTER TABLE ai_processing_runs ADD COLUMN IF NOT EXISTS recipe JSONB;
-- Первое предложение правила: три подсказки толкования стадии «Карточки» (эксперт, замечания 13.09).
INSERT INTO feedback_actions (kind, status, agents, author, quote, details)
SELECT 'rule_proposal', 'pending', ARRAY['card'], 'Перенос из правил замен',
       'expected to contribute: ожидаемый, не подтверждённый эффект; evaluate feasibility: применимость ещё оценивается;'
       ' PoC: не промышленное внедрение',
       jsonb_build_object(
         'rule', jsonb_build_object(
           'agent', 'card', 'step', 'maturity', 'category', NULL, 'topic', NULL,
           'text', '«expected to contribute» значит ожидаемый, не подтверждённый эффект; «evaluate feasibility» значит '
                   'применимость ещё оценивается; «PoC» не промышленное внедрение.'),
         'rule_kind', 'new', 'replaces', '[]'::jsonb, 'replaced', '[]'::jsonb,
         'seed', 'три подсказки толкования из правил замен эксперта (разбор 53 правил 12.11)')
WHERE NOT EXISTS (SELECT 1 FROM feedback_actions WHERE kind = 'rule_proposal' AND details ? 'seed');
COMMIT;
