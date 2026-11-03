-- Пробный прогон широкого поиска (#138): решения «Отбора» по находкам — в таблице проверки #134
-- (otbor_shadow_decisions), ДО выката кода, который их туда пишет. У строки статья или находка (signal_id).
--
-- Только добавление, повторный прогон безопасен; строк таблица держит немного, замок на миг.
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/signals/search-trial-shadow-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE otbor_shadow_decisions ALTER COLUMN article_id DROP NOT NULL;
ALTER TABLE otbor_shadow_decisions ADD COLUMN IF NOT EXISTS signal_id BIGINT REFERENCES signals(id) ON DELETE CASCADE;
CREATE UNIQUE INDEX IF NOT EXISTS idx_otbor_shadow_signal ON otbor_shadow_decisions(signal_id) WHERE signal_id IS NOT NULL;
DO $$ BEGIN
  ALTER TABLE otbor_shadow_decisions ADD CONSTRAINT otbor_shadow_one_publication
    CHECK ((article_id IS NULL) <> (signal_id IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
COMMIT;
