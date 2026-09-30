-- Качество радара (замечания Виктора 29.09) — схема ДО выката кода, который в неё пишет.
--
-- Только добавления, повторный прогон безопасен (IF NOT EXISTS), массовых UPDATE нет:
-- у прежних карточек новые поля — NULL, и видимость читает их как «до правки» (карточка видна).
-- lock_timeout: ALTER берёт на signals эксклюзивный замок; если его держит долгий запрос, шаг
-- откажет через 5 с, ничего не изменив, — повторить позже. Окно — не во время радара (00:00–00:30 МСК).
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/radar/radar-quality-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS signal_category TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS event_date DATE;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS mixed_events BOOLEAN;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS mixed_events_reason TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS score_profile TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS score_items_json JSONB;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS criteria_snapshot JSONB;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS archive_reason TEXT;
COMMIT;
