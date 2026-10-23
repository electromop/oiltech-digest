-- Агент «Отбор» для ленты (#130, ADR 0005) — схема ДО выката кода, который в неё пишет.
--
-- Только добавления, повторный прогон безопасен (IF NOT EXISTS), заполнений нет: у прежних сигналов решения
-- «Отбора» нет (NULL — живут по прежним правилам), у прежних статей метки пути нет (NULL — их решает гейт).
-- lock_timeout: ALTER берёт на signals и articles эксклюзивный замок; если его держит долгий запрос, шаг
-- откажет через 5 с, ничего не изменив, — повторить позже. Окно — не во время радара (00:00–02:30 МСК).
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/signals/otbor-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS industry TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS otbor_decision TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS reject_reason TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS otbor_disputed TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS otbor_failure TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS otbor_failure_reason TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS otbor_recipe JSONB;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS selector TEXT;
CREATE INDEX IF NOT EXISTS idx_articles_otbor ON articles(collected_at) WHERE selector = 'otbor';
COMMIT;
