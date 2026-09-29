-- Сессия G (ADR 0002): снять ещё не выданные задачи пересчёта балла (enqueue-rescore) — перед
-- возвратом баллов из копии (ранбук scoring-profiles-runbook.md, откат «в»). Задачи в работе
-- дорабатывают: дождаться running=0 у external-ai-bulk (cli external-queues-status) и только
-- потом scoring-restore-2026-09.sql. Повтор безопасен.
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/g/scoring-rescore-cancel.sql
BEGIN;
UPDATE background_jobs
SET status = 'failed',
    error_message = 'снято владельцем: откат пересчёта (ADR 0002)',
    finished_at = now()
WHERE kind = 'process_articles'
  AND status = 'queued'
  AND payload_json->'only' = '["scoring"]'::jsonb;
COMMIT;
