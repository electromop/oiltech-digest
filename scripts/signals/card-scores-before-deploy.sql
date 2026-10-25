-- «Карточка» в конвейере «Отбора» (#132): тема, баллы вторым вызовом, «Решение платформы», возврат отклонённого —
-- схема ДО выката кода, который в неё пишет.
--
-- Только добавления и одно ослабление, повторный прогон безопасен (IF NOT EXISTS; DROP NOT NULL у уже пустого
-- ограничения ничего не делает), заполнений нет: у прежних сигналов точности даты нет (NULL — день), возвратов 0,
-- баллов второго вызова нет. maturity теряет NOT NULL: у «другого» «Решения платформы» нет; прежний код пустое туда
-- не пишет. lock_timeout: ALTER берёт на таблицу эксклюзивный замок; если его держит долгий запрос, шаг откажет через
-- 5 с, ничего не изменив, — повторить позже. Окно — не во время радара (00:00–02:30 МСК).
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/signals/card-scores-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS event_date_precision TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS otbor_returns INTEGER NOT NULL DEFAULT 0;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS otbor_return_fact TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS score_band TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS scores_recipe JSONB;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS scores_stale_since TIMESTAMPTZ;
ALTER TABLE signals ALTER COLUMN maturity DROP NOT NULL;
COMMIT;
