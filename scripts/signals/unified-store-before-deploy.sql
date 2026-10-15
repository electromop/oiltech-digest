-- Единое хранилище сигналов (#95, ADR 0003) — схема ДО выката кода, который в неё пишет.
--
-- Только добавления, повторный прогон безопасен (IF NOT EXISTS). origin со значением по
-- умолчанию: Postgres ставит его существующим строкам без перезаписи таблицы — все прежние
-- карточки становятся «радар», массового UPDATE нет.
-- lock_timeout: ALTER берёт на signals эксклюзивный замок; если его держит долгий запрос, шаг
-- откажет через 5 с, ничего не изменив, — повторить позже. Окно — не во время радара (00:00–02:30 МСК).
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/signals/unified-store-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'radar';  -- radar / feed
ALTER TABLE signals ADD COLUMN IF NOT EXISTS origin_article_id BIGINT REFERENCES articles(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_signals_origin_article ON signals(origin_article_id) WHERE origin_article_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_signals_origin_category ON signals(origin, signal_category);
COMMIT;
