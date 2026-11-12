-- Контур ОС (#188): погасить правила замен старой памяти агента, поимённо. Только по «да» владельца, после выката ядра.
--
-- Правила заводил разбор «Рекомендаций AI-агенту»: каждая стрелка «А → Б» становилась заменой слов в заголовках
-- найденных страниц. После #188 их не читает никто (прогон радара словарь замен не применяет), гашение убирает их из
-- истории памяти как действующие. Разбор 53 правил (12.11): 27 переводов терминов перешли в словарь «Сути и перевода»
-- (processing/domain_glossary.json), 3 подсказки толкования (id 3, 4, 5) забирает тикет правил эксперта, 22 цепочки
-- рассуждений и одно число сняты. Строки не удаляются: статус superseded; вернуть: тот же список, статус active.
--
-- Критерий: memory_type = 'signal_glossary', status = 'active', id из списка ниже (прод 12.11.2026: 53 строки). Если
-- под критерий попадает не 53 строки, скрипт останавливается и ничего не меняет: список сверить заново.
--
--   1. Дамп до:     ssh <РФ-ядро> 'docker exec oiltech_pg pg_dump -U oiltech -d oiltech_digest -t signal_agent_memory -Fc \
--                     > /root/oiltech-backups/signal_agent_memory_before_188.dump'
--   2. Гашение:     ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
--                     < scripts/signals/os-glossary-retire.sql
--   3. Дамп после:  как в шаге 1, в signal_agent_memory_after_188.dump
BEGIN;
SET LOCAL lock_timeout = '5s';
CREATE TEMP TABLE os_glossary_retire (id BIGINT PRIMARY KEY) ON COMMIT DROP;
INSERT INTO os_glossary_retire (id) VALUES
  (1), (2), (3), (4), (5), (6), (7), (8), (9), (10), (11), (12), (13), (14), (15), (16), (17), (18), (19), (20),
  (21), (22), (23), (24), (25), (26), (27), (28), (29), (30), (31), (32), (33), (34), (35), (36), (37), (38), (39),
  (40), (41), (189), (201), (222), (223), (224), (308), (309), (310), (314), (315), (316), (1347);
DO $$
DECLARE
  listed INTEGER := (SELECT count(*) FROM os_glossary_retire);
  matching INTEGER := (SELECT count(*) FROM signal_agent_memory m JOIN os_glossary_retire r ON r.id = m.id
                       WHERE m.memory_type = 'signal_glossary' AND m.status = 'active');
BEGIN
  IF listed <> 53 OR matching <> 53 THEN
    RAISE EXCEPTION 'Список не сходится: в списке %, действующих правил замен из него %; ожидалось 53 и 53', listed, matching;
  END IF;
END $$;
UPDATE signal_agent_memory m SET status = 'superseded', updated_at = now()
FROM os_glossary_retire r
WHERE r.id = m.id AND m.memory_type = 'signal_glossary' AND m.status = 'active';
SELECT count(*) AS still_active_glossary FROM signal_agent_memory WHERE memory_type = 'signal_glossary' AND status = 'active';
COMMIT;
