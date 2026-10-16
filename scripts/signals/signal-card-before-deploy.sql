-- Карточка сигнала (#96, ADR 0003) — схема ДО выката кода, который в неё пишет.
--
-- Только добавления, повторный прогон безопасен (IF NOT EXISTS). Одно заполнение новой колонки:
-- у находок радара — кто поставил значения судьи (дата события, связь с нефтесервисом, применение,
-- «почему интересно»); строк ≈ 330, повторный прогон их не находит. У сигналов ленты карточки нет —
-- её заполнит агент после экзамена (#98).
-- lock_timeout: ALTER берёт на signals эксклюзивный замок; если его держит долгий запрос, шаг
-- откажет через 5 с, ничего не изменив, — повторить позже. Окно — не во время радара (00:00–02:30 МСК).
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/signals/signal-card-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS event_type TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS region TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS technology_class TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS technology_maturity TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS participants_json JSONB;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS event_date_source TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS card_set_by JSONB;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS card_recipe JSONB;
UPDATE signals SET card_set_by = jsonb_strip_nulls(jsonb_build_object(
    'event_date', CASE WHEN event_date IS NOT NULL THEN 'radar_judge' END,
    'oilfield_relevance', CASE WHEN oilfield_relevance IS NOT NULL THEN 'radar_judge' END,
    'oilfield_application', CASE WHEN COALESCE(oilfield_application, '') <> '' THEN 'radar_judge' END,
    'why_interesting', CASE WHEN COALESCE(why_interesting, '') <> '' THEN 'radar_judge' END))
WHERE origin = 'radar' AND card_set_by IS NULL;
ALTER TABLE ai_processing_runs ADD COLUMN IF NOT EXISTS signal_id BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_runs_job_signal_stage
  ON ai_processing_runs(job_id, signal_id, stage) WHERE signal_id IS NOT NULL;
COMMIT;
