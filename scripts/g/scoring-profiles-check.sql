-- Сессия G (ADR 0002): проверка после шагов выката — только чтение (READ ONLY, в конце ROLLBACK).
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/g/scoring-profiles-check.sql
-- Имеет смысл после шага 1: до него колонки profile нет, и первый же запрос упадёт.
BEGIN TRANSACTION READ ONLY;
-- 1. Активные критерии по профилям: сумма весов — ровно 100 в каждом. business — прежний набор
--    ленты; tech_radar появится после seed-scoring (шаг 4).
SELECT profile, count(*) AS active, sum(weight) AS weight_sum
FROM scoring_criteria WHERE enabled GROUP BY profile ORDER BY profile;
-- 2. Имена активных по профилям — сверка после пресета: в business ровно пять критериев набора
--    заказчика (ранбук, шаг 5).
SELECT profile, id, name, weight
FROM scoring_criteria WHERE enabled ORDER BY profile, sort_order, id;
-- 2а. Все строки по профилям: выключенные — история, сид их не воскрешает.
SELECT profile, enabled, count(*) AS criteria
FROM scoring_criteria GROUP BY profile, enabled ORDER BY profile, enabled;
-- 3. Индексы имён: после шага 3 остаётся только idx_scoring_criteria_profile_name.
SELECT indexname FROM pg_indexes
WHERE schemaname = current_schema() AND tablename = 'scoring_criteria' ORDER BY indexname;
-- 4. Происхождение баллов: у оценённых после выката — профиль business и снимок критериев.
SELECT COALESCE(profile, '(до профилей)') AS profile, count(*) AS scores,
       count(criteria_snapshot) AS with_snapshot, max(updated_at) AS last_scored
FROM article_scores GROUP BY 1 ORDER BY 1;
-- 5. Стадия scoring идёт: вызовы модели за последние 6 часов, по часам.
SELECT date_trunc('hour', created_at) AS hour, count(*) AS runs
FROM ai_processing_runs
WHERE stage = 'scoring' AND created_at > now() - interval '6 hours'
GROUP BY 1 ORDER BY 1;
ROLLBACK;
