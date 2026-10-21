-- Дочерние общества «дочка → мать» (решение владельца 20.10): таблица ДО выката кода, который её читает
-- (фильтр «всё про компанию», экран «Дочерние общества»).
--
-- Только добавление, повторный прогон безопасен (IF NOT EXISTS). CREATE тот же, что в schema.sql; init-db на
-- живой базе не нужен. Выкат только ядра, NL не трогается.
-- lock_timeout: ссылка на users берёт на неё короткий замок; если его держит долгий запрос, шаг откажет
-- через 5 с, ничего не изменив: повторить позже.
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/signals/company-parents-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
CREATE TABLE IF NOT EXISTS company_parents (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  child_name   TEXT NOT NULL,
  parent_name  TEXT NOT NULL,
  note         TEXT,
  created_by   BIGINT REFERENCES users(id) ON DELETE SET NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMIT;
