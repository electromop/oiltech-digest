-- Сессия G (ADR 0002), шаг 1 из 3: схема профилей скоринга — ДО выката кода.
--
-- Только добавления; повторный прогон безопасен: колонки и индекс — IF NOT EXISTS,
-- ограничение — через duplicate_object. Массовых UPDATE нет: прежние строки получают
-- profile = 'business' умолчанием колонки (PostgreSQL 11+ таблицу при этом не переписывает).
-- Глобальный индекс имён idx_scoring_criteria_name здесь НЕ снимается: старый код пишет
-- ON CONFLICT (name) и без него не сохранил бы новый критерий — это шаг 3, после выката.
--
-- lock_timeout: ALTER берёт на таблицу эксклюзивный замок. Если его держит долгий запрос,
-- шаг откажет через 5 с (ничего не изменив), а не встанет в очередь впереди ленты —
-- тогда повторить позже. Окно — без ИИ-аренд (cli live-ai-leases).
--
-- Из своей копии репозитория, на РФ-ядро:
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/g/scoring-profiles-before-deploy.sql
-- Затем: scripts/deploy-core.sh --no-schema app scheduler worker playwright-worker (шаг 2).
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE scoring_criteria ADD COLUMN IF NOT EXISTS profile TEXT NOT NULL DEFAULT 'business';
DO $$
BEGIN
  ALTER TABLE scoring_criteria
    ADD CONSTRAINT scoring_criteria_profile_check CHECK (profile IN ('business', 'tech_radar'));
EXCEPTION
  WHEN duplicate_object THEN NULL;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS idx_scoring_criteria_profile_name ON scoring_criteria(profile, name);
ALTER TABLE article_scores ADD COLUMN IF NOT EXISTS profile TEXT;
ALTER TABLE article_scores ADD COLUMN IF NOT EXISTS criteria_snapshot JSONB;
COMMIT;
