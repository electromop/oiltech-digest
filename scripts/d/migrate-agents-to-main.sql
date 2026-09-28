-- D: агентные таблицы основной базы ← содержимое базы агентов (схема agents_src).
-- Запускает migrate-agents-to-main.sh одной транзакцией (psql -1): любая ошибка или
-- расхождение сверки откатывает всё. Повторный запуск даёт тот же итог.

-- Разделение баз: базу агентов развернули дампом основной 18.09 в 11:24 UTC. После этого
-- обе базы выдавали одинаковые номера новым статьям и задачам.
CREATE TEMP TABLE d_fork AS SELECT timestamptz '2026-09-18 11:24+00' AS at;

-- Таблицы по порядку «родитель раньше ребёнка»; удаление — в обратном.
CREATE TEMP TABLE d_tables (pos int, name text);
INSERT INTO d_tables VALUES
  (1, 'agent_runs'), (2, 'agent_tasks'), (3, 'agent_actions'), (4, 'agent_memory'),
  (5, 'signal_agent_memory'), (6, 'signal_radar_topics'), (7, 'signal_generation_runs'),
  (8, 'signals'), (9, 'signal_evidence'), (10, 'signal_feedback_events'),
  (11, 'signal_training_examples'), (12, 'user_signal_states'), (13, 'source_candidates'),
  (14, 'source_candidate_articles');

CREATE FUNCTION pg_temp.cols(t text) RETURNS text LANGUAGE sql AS $$
  SELECT string_agg(quote_ident(column_name), ',' ORDER BY ordinal_position)
  FROM information_schema.columns WHERE table_schema = 'agents_src' AND table_name = t $$;

-- 1. Сторожа.
DO $$
DECLARE t text; lost bigint;
BEGIN
  IF (SELECT count(*) FROM agents_src.signals) = 0 THEN
    RAISE EXCEPTION 'agents_src.signals пуст — источник не загружен';
  END IF;
  -- После разделения в агентные таблицы основной базы пишет только лента — в
  -- signal_feedback_events (пометки статей); их строки сохраняются ниже (шаг 4). Остальные
  -- 13 таблиц основной базы после 18.09 не менялись. Номер после разделения совпадает с
  -- чужой строкой базы агентов, поэтому «своя» строка — та же пара id и created_at (так
  -- выглядят и строки, перенесённые прошлым прогоном).
  FOR t IN SELECT name FROM d_tables WHERE name NOT IN ('user_signal_states', 'signal_feedback_events') LOOP
    EXECUTE format(
      'SELECT count(*) FROM public.%I p WHERE p.created_at >= (SELECT at FROM d_fork)
         AND NOT EXISTS (SELECT 1 FROM agents_src.%I s WHERE s.id = p.id AND s.created_at = p.created_at)',
      t, t) INTO lost;
    IF lost > 0 THEN
      RAISE EXCEPTION 'в основной базе % строк в % новее разделения — их нет в базе агентов', lost, t;
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM agents_src.users s JOIN public.users u ON u.id = s.id
             WHERE lower(u.email) <> lower(s.email)) THEN
    RAISE EXCEPTION 'у пользователя с одним id разные email в базах';
  END IF;
  IF EXISTS (SELECT 1 FROM agents_src.users s JOIN public.users u ON lower(u.email) = lower(s.email)
             WHERE u.id <> s.id) THEN
    RAISE EXCEPTION 'один email под разными id в базах';
  END IF;
END $$;

-- 2. Пользователи, заведённые в базе агентов (21, 22).
DO $$
DECLARE c text := pg_temp.cols('users'); n bigint;
BEGIN
  EXECUTE format('INSERT INTO public.users (%s) OVERRIDING SYSTEM VALUE
                  SELECT %s FROM agents_src.users s
                  WHERE NOT EXISTS (SELECT 1 FROM public.users u WHERE u.id = s.id)', c, c);
  GET DIAGNOSTICS n = ROW_COUNT;
  RAISE NOTICE 'users: добавлено %', n;
END $$;

-- 3. Ссылки на основную базу — до вставки, в промежуточной схеме.
-- Задачи агентов не переносятся, а номера их задач после разделения совпадают с другими
-- задачами основной базы.
UPDATE agents_src.signal_generation_runs r SET background_job_id = NULL
WHERE r.background_job_id IS NOT NULL
  AND (r.created_at >= (SELECT at FROM d_fork)
       OR NOT EXISTS (SELECT 1 FROM public.background_jobs b WHERE b.id = r.background_job_id));

-- Статьи агентов после разделения — по url_key в основной базе (иначе NULL).
CREATE TEMP TABLE d_article_map AS
SELECT k.id AS agents_id,
       (SELECT m.id FROM public.articles m WHERE m.url_key = k.url_key
        ORDER BY m.pending_deletion NULLS FIRST, m.id LIMIT 1) AS main_id
FROM agents_src.article_keys k;

CREATE TEMP TABLE d_feedback_orphans AS
SELECT f.id FROM agents_src.signal_feedback_events f
WHERE f.signal_id IS NULL AND f.article_id IS NOT NULL
  AND (EXISTS (SELECT 1 FROM d_article_map m WHERE m.agents_id = f.article_id AND m.main_id IS NULL)
       OR (NOT EXISTS (SELECT 1 FROM d_article_map m WHERE m.agents_id = f.article_id)
           AND NOT EXISTS (SELECT 1 FROM public.articles a WHERE a.id = f.article_id)));

UPDATE agents_src.signal_evidence e SET article_id = m.main_id
FROM d_article_map m WHERE e.article_id = m.agents_id;
UPDATE agents_src.signal_feedback_events f SET article_id = m.main_id
FROM d_article_map m WHERE f.article_id = m.agents_id;
UPDATE agents_src.signal_evidence e SET article_id = NULL
WHERE e.article_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.articles a WHERE a.id = e.article_id);
UPDATE agents_src.signal_feedback_events f SET article_id = NULL
WHERE f.article_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.articles a WHERE a.id = f.article_id);
-- Отметка по статье, которой в основной базе нет, без сигнала ни к чему не относится.
DELETE FROM agents_src.signal_feedback_events f USING d_feedback_orphans o WHERE f.id = o.id;

UPDATE agents_src.signal_feedback_events f SET user_id = NULL
WHERE f.user_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.users u WHERE u.id = f.user_id);
UPDATE agents_src.source_candidates c SET approved_source_id = NULL
WHERE c.approved_source_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.sources s WHERE s.id = c.approved_source_id);
UPDATE agents_src.source_candidate_articles a SET tag_id = NULL
WHERE a.tag_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.tags t WHERE t.id = a.tag_id);
DELETE FROM agents_src.user_signal_states s
WHERE NOT EXISTS (SELECT 1 FROM public.users u WHERE u.id = s.user_id);

-- 4. Единственная ссылка из основных таблиц в агентные: background_jobs.agent_run_id
-- (ON DELETE SET NULL). Запомнить до замены, вернуть после.
CREATE TEMP TABLE d_keep_agent_run AS
SELECT id, agent_run_id FROM public.background_jobs WHERE agent_run_id IS NOT NULL;
-- Пометки статей в основной ленте после разделения (PATCH /api/articles пишет журнал в
-- signal_feedback_events): вернуть после замены с новыми номерами.
CREATE TEMP TABLE d_main_feedback_new AS
SELECT * FROM public.signal_feedback_events p
WHERE p.created_at >= (SELECT at FROM d_fork)
  AND NOT EXISTS (SELECT 1 FROM agents_src.signal_feedback_events s
                  WHERE s.id = p.id AND s.created_at = p.created_at);

-- 5. Замена: удалить от детей к родителям, вставить от родителей к детям.
DO $$
DECLARE t text; c text; n bigint;
BEGIN
  FOR t IN SELECT name FROM d_tables ORDER BY pos DESC LOOP
    EXECUTE format('DELETE FROM public.%I', t);
  END LOOP;
  FOR t IN SELECT name FROM d_tables ORDER BY pos LOOP
    c := pg_temp.cols(t);
    IF t = 'user_signal_states' THEN  -- без столбца идентичности
      EXECUTE format('INSERT INTO public.%I (%s) SELECT %s FROM agents_src.%I', t, c, c, t);
    ELSE
      EXECUTE format('INSERT INTO public.%I (%s) OVERRIDING SYSTEM VALUE SELECT %s FROM agents_src.%I', t, c, c, t);
    END IF;
    GET DIAGNOSTICS n = ROW_COUNT;
    RAISE NOTICE '%: %', t, n;
  END LOOP;
END $$;

UPDATE public.background_jobs b SET agent_run_id = k.agent_run_id
FROM d_keep_agent_run k
WHERE b.id = k.id AND EXISTS (SELECT 1 FROM public.agent_runs r WHERE r.id = k.agent_run_id);

-- 6. Отметки пользователей о статьях в интерфейсе агентов после разделения. Статья агентов
-- после 18.09 — по url_key; статья до разделения — тот же номер в обеих базах. Отметка
-- основной базы по той же паре пользователь×статья остаётся (ON CONFLICT DO NOTHING).
DO $$
DECLARE n bigint; total bigint;
BEGIN
  INSERT INTO public.user_article_states (user_id, article_id, status, analyst_comment, updated_at)
  SELECT s.user_id, coalesce(m.main_id, s.article_id), s.status, s.analyst_comment, s.updated_at
  FROM agents_src.user_article_states s
  LEFT JOIN d_article_map m ON m.agents_id = s.article_id
  WHERE EXISTS (SELECT 1 FROM public.users u WHERE u.id = s.user_id)
    AND (m.main_id IS NOT NULL
         OR (m.agents_id IS NULL AND EXISTS (SELECT 1 FROM public.articles a WHERE a.id = s.article_id)))
  ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  SELECT count(*) INTO total FROM agents_src.user_article_states;
  RAISE NOTICE 'user_article_states: в источнике %, добавлено % (остальные — уже отмечены в основной или без статьи)', total, n;
END $$;

-- 7. Счётчики номеров — за максимумом вставленного.
DO $$
DECLARE t text;
BEGIN
  FOR t IN SELECT name FROM d_tables WHERE name <> 'user_signal_states' UNION ALL SELECT 'users' LOOP
    EXECUTE format('SELECT setval(pg_get_serial_sequence(%L, ''id''), GREATEST((SELECT max(id) FROM public.%I), 1))',
                   'public.' || t, t);
  END LOOP;
END $$;

-- Пометки основной ленты после разделения — обратно, с новыми номерами. Ссылки на строки
-- радара, которых после замены нет, — NULL.
DO $$
DECLARE c text; n bigint;
BEGIN
  SELECT string_agg(quote_ident(column_name), ',' ORDER BY ordinal_position) INTO c
  FROM information_schema.columns
  WHERE table_schema = 'public' AND table_name = 'signal_feedback_events' AND column_name <> 'id';
  UPDATE d_main_feedback_new f SET signal_id = NULL
  WHERE f.signal_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.signals s WHERE s.id = f.signal_id);
  UPDATE d_main_feedback_new f SET duplicate_of_signal_id = NULL
  WHERE f.duplicate_of_signal_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.signals s WHERE s.id = f.duplicate_of_signal_id);
  UPDATE d_main_feedback_new f SET signal_evidence_id = NULL
  WHERE f.signal_evidence_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.signal_evidence e WHERE e.id = f.signal_evidence_id);
  EXECUTE format('INSERT INTO public.signal_feedback_events (%s) SELECT %s FROM d_main_feedback_new ORDER BY id', c, c);
  GET DIAGNOSTICS n = ROW_COUNT;
  RAISE NOTICE 'signal_feedback_events: пометок основной ленты после разделения возвращено %', n;
END $$;

-- 8. Сверка: в основной базе ровно то, что в источнике (плюс возвращённые пометки ленты).
DO $$
DECLARE t text; a bigint; b bigint;
BEGIN
  FOR t IN SELECT name FROM d_tables ORDER BY pos LOOP
    EXECUTE format('SELECT count(*) FROM agents_src.%I', t) INTO a;
    IF t = 'signal_feedback_events' THEN
      a := a + (SELECT count(*) FROM d_main_feedback_new);
    END IF;
    EXECUTE format('SELECT count(*) FROM public.%I', t) INTO b;
    IF a <> b THEN
      RAISE EXCEPTION 'сверка %: ожидалось %, в основной %', t, a, b;
    END IF;
  END LOOP;
  IF (SELECT count(*) FROM public.users) < (SELECT count(*) FROM agents_src.users) THEN
    RAISE EXCEPTION 'сверка users: пользователей меньше, чем в базе агентов';
  END IF;
  RAISE NOTICE 'сверка сошлась; пометок ленты возвращено: %; отметок по статьям без пары удалено: %; ссылок agent_run_id возвращено: %',
    (SELECT count(*) FROM d_main_feedback_new),
    (SELECT count(*) FROM d_feedback_orphans),
    (SELECT count(*) FROM public.background_jobs b JOIN d_keep_agent_run k ON k.id = b.id
     WHERE b.agent_run_id = k.agent_run_id);
END $$;
