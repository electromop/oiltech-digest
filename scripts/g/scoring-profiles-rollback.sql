-- Сессия G (ADR 0002): откат кода на прежнюю сборку ПОСЛЕ шага 3 (DROP INDEX + seed-scoring).
-- Выполнить ДО выката старого кода — ранбук scoring-profiles-runbook.md, откат «а».
--
-- Старый код читает критерии без фильтра профиля: с профилем tech_radar он увидел бы оба набора
-- (сумма 200) — баллы через NL молча раздулись бы, локальные пути встали бы. Его «Сохранить»
-- пишет ON CONFLICT (name) и без глобального индекса имён падает. Колонки profile и
-- criteria_snapshot старому коду не мешают — их не трогаем.
-- Критерии tech_radar в баллы статей не входят, профиль удаляется целиком; если на них всё же
-- ссылается подпункт, DELETE откажет по внешнему ключу и ничего не изменится. Повтор безопасен.
-- Набор business вернуть к прежнему (если пресет уже применён) — откат «б» ранбука, до этого файла.
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/g/scoring-profiles-rollback.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
DELETE FROM scoring_criteria WHERE profile = 'tech_radar';
CREATE UNIQUE INDEX IF NOT EXISTS idx_scoring_criteria_name ON scoring_criteria(name);
COMMIT;
