-- Сессия G (ADR 0002): копия баллов сентября 2026 перед пересчётом с ИИ
-- (enqueue-rescore --no-dry-run) — ранбук scoring-profiles-runbook.md, шаг 6.
--
-- Пересчёт переписывает article_scores и УДАЛЯЕТ прежние подпункты article_score_items
-- (repository.replace_article_score): без копии прежние баллы не вернуть. Копия — таблицами в той
-- же базе; вернуть — scoring-restore-2026-09.sql. Месяц статьи — как у ленты и выпуска:
-- COALESCE(published_at, collected_at) в поясе сессии базы.
-- Повторный запуск откажет (таблица уже есть): первая копия не перезаписывается.
-- После приёмки пересчёта: DROP TABLE g_backup_article_score_items_2026_09, g_backup_article_scores_2026_09;
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/g/scoring-backup-2026-09.sql
BEGIN;
CREATE TABLE g_backup_article_scores_2026_09 AS
SELECT s.*
FROM article_scores s
JOIN articles a ON a.id = s.article_id
WHERE to_char(COALESCE(a.published_at, a.collected_at), 'YYYY-MM') = '2026-09';
CREATE TABLE g_backup_article_score_items_2026_09 AS
SELECT i.*
FROM article_score_items i
WHERE i.article_score_id IN (SELECT id FROM g_backup_article_scores_2026_09);
COMMIT;
SELECT (SELECT count(*) FROM g_backup_article_scores_2026_09) AS scores,
       (SELECT count(*) FROM g_backup_article_score_items_2026_09) AS items;
