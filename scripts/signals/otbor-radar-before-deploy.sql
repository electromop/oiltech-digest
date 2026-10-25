-- Радар и материалы коллег через «Отбор» (#131, ADR 0005) — схема ДО выката кода, который в неё пишет.
--
-- Только добавления, повторный прогон безопасен (IF NOT EXISTS), заполнений нет: у прежних сигналов метки пути нет
-- (NULL — живут по прежним правилам), материалам ещё ничего не сообщалось, у прежних блоков ОС автор — учётка.
-- lock_timeout: ALTER берёт на таблицы эксклюзивный замок; если его держит долгий запрос, шаг откажет через 5 с,
-- ничего не изменив, — повторить позже. Окно — не во время радара (00:00–02:30 МСК).
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/signals/otbor-radar-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS selector TEXT;
CREATE INDEX IF NOT EXISTS idx_signals_otbor_waiting ON signals(created_at)
  WHERE selector = 'otbor' AND otbor_decision IS NULL;
ALTER TABLE colleague_materials ADD COLUMN IF NOT EXISTS notified_outcome TEXT;
ALTER TABLE colleague_materials ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
ALTER TABLE signal_feedback_events ADD COLUMN IF NOT EXISTS author TEXT;
COMMIT;
