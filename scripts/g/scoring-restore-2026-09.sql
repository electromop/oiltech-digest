-- Сессия G (ADR 0002): вернуть баллы сентября 2026 из копии scoring-backup-2026-09.sql —
-- откат пересчёта с ИИ (ранбук scoring-profiles-runbook.md, откат «в»).
--
-- Балл статьи при пересчёте сохраняет свой id (запись по article_id), поэтому итог, профиль и
-- снимок возвращаются по id, а подпункты — заменой: текущие удаляются, копия вставляется.
-- Баллы, которых в копии нет (статьи, впервые оценённые после неё), не трогаются. Повтор безопасен.
-- Задачи пересчёта, если ещё идут, сначала снять — иначе они перезапишут вернувшиеся баллы.
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/g/scoring-restore-2026-09.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
DELETE FROM article_score_items
WHERE article_score_id IN (SELECT id FROM g_backup_article_scores_2026_09);
UPDATE article_scores s
SET model = b.model,
    total_score = b.total_score,
    score_label = b.score_label,
    explanation = b.explanation,
    profile = b.profile,
    criteria_snapshot = b.criteria_snapshot,
    updated_at = b.updated_at
FROM g_backup_article_scores_2026_09 b
WHERE s.id = b.id;
INSERT INTO article_score_items
  (article_score_id, criterion_id, keyword_score, ai_score, final_score, rationale, created_at)
SELECT article_score_id, criterion_id, keyword_score, ai_score, final_score, rationale, created_at
FROM g_backup_article_score_items_2026_09
WHERE article_score_id IN (SELECT id FROM article_scores)
ORDER BY id;
COMMIT;
