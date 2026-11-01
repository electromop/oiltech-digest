-- Страна сигнала у «Карточки» (#137): страны события. Схема ДО выката кода, который в неё пишет.
--
-- Только добавление, повторный прогон безопасен (IF NOT EXISTS), заполнений нет: страну старые карточки получат
-- прогоном истории «Карточки» (отдельное решение владельца). Страна участника — поле в записи participants_json,
-- колонки ей не нужно. lock_timeout: ALTER берёт на таблицу эксклюзивный замок; если его держит долгий запрос, шаг
-- откажет через 5 с, ничего не изменив, и его нужно повторить позже. Окно не во время радара (00:00-02:30 МСК).
--
--   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--     < scripts/signals/card-country-before-deploy.sql
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS event_countries TEXT[];
COMMIT;
