-- Правки человека в выпуске и описание Компании (#162, спека #160, ADR 0006): колонки сигнала и две таблицы ДО выката
-- кода, который их читает (сборщик выпуска, экран «Дайджест», карточка сигнала, экран «Описание Компании»).
--
-- Только добавление, повторный прогон безопасен (IF NOT EXISTS). Те же ALTER и CREATE, что в schema.sql; init-db на
-- живой базе не нужен. Выкат только ядра, NL не трогается.
-- lock_timeout: ALTER берёт на signals короткий замок; если его держит долгий запрос (прогон радара пишет находки), шаг
-- откажет через 5 с, ничего не изменив: повторить позже.
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/digest/issue-edits-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS issue_label TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS hypothesis TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS hypothesis_set_at TIMESTAMPTZ;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS hypothesis_author TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS issue_picture TEXT;
CREATE TABLE IF NOT EXISTS issue_highlights (
  month        TEXT NOT NULL,
  section      TEXT NOT NULL,
  text         TEXT NOT NULL,
  updated_by   BIGINT REFERENCES users(id) ON DELETE SET NULL,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (month, section)
);
CREATE TABLE IF NOT EXISTS company_descriptions (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  text         TEXT NOT NULL,
  created_by   BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMIT;
