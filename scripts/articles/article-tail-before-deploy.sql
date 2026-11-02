-- Колонка отрезанного хвоста текста статьи (#140): ДО выката кода, который её пишет и читает.
--
-- Только добавление, повторный прогон безопасен (IF NOT EXISTS), столбец без значения по умолчанию
-- таблицу не переписывает. init-db на живой базе не нужен.
-- lock_timeout: ALTER берёт на articles короткий замок; если его держит долгий запрос, шаг откажет
-- через 5 с, ничего не изменив: повторить позже.
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/articles/article-tail-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE articles ADD COLUMN IF NOT EXISTS text_tail TEXT;
COMMIT;
