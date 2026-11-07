-- Дата начала сбора у источника (решение владельца 06.11): колонка до выката кода, который её читает. Тот же ALTER,
-- что в schema.sql; init-db на живой базе не нужен. Прежние источники получают NULL (без границы), значение по умолчанию
-- (две недели до заведения) ставится только новым строкам. Повторный прогон безопасен.
-- lock_timeout: ALTER берёт на sources короткий замок; если его держит долгий запрос, шаг откажет через 5 с, ничего не
-- изменив: повторить позже. Выкат: этот SQL, затем ядро; NL по своему графику (ядро и без неё не вставит архив).
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/sources/collect-since-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE sources ADD COLUMN IF NOT EXISTS collect_since TIMESTAMPTZ;
ALTER TABLE sources ALTER COLUMN collect_since SET DEFAULT now() - interval '14 days';
COMMIT;
