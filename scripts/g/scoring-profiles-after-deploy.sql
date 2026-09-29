-- Сессия G (ADR 0002), шаг 3 из 3: снять глобальный индекс имён — ПОСЛЕ выката кода.
--
-- Раньше нельзя: старый код пишет ON CONFLICT (name) (сид и «Сохранить» нового критерия),
-- без этого индекса такой запрос падает. Новый код пишет ON CONFLICT (profile, name) — индекс
-- idx_scoring_criteria_profile_name создан шагом 1. Пока глобальный индекс на месте, в профиль
-- tech_radar нельзя завести «Технологическую новизну»: это имя занято строкой старого сида.
-- Повторный прогон безопасен (IF EXISTS).
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/g/scoring-profiles-after-deploy.sql
-- Затем: docker exec oiltech_app python -m oiltech_digest.cli seed-scoring — заведёт профиль
-- tech_radar (он пуст); профиль business сид не трогает (в нём уже есть критерии).
BEGIN;
SET LOCAL lock_timeout = '5s';
DROP INDEX IF EXISTS idx_scoring_criteria_name;
COMMIT;
