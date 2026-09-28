#!/bin/bash
# D (единый контур): перенос агентных данных — радар и агент источников — из базы агентов
# в основную. Замена и сверка — в migrate-agents-to-main.sql, одной транзакцией.
#
# Запускать ФАЙЛОМ, не через `ssh … bash -s`: docker exec -i съел бы остаток скрипта.
#   bash scripts/d/migrate-agents-to-main.sh                     # РФ: базы по умолчанию
#   SRC_C=oiltech_pg SRC_DB=d_agents DST_DB=d_main bash …        # репетиция на копиях
# Повторный запуск безопасен: схема agents_src пересоздаётся, итог тот же.
set -euo pipefail

SRC_C=${SRC_C:-oiltech_agents_pg}
SRC_DB=${SRC_DB:-oiltech_agents}
DST_C=${DST_C:-oiltech_pg}
DST_DB=${DST_DB:-oiltech_digest}
PGUSER=${PGUSER:-oiltech}
FORK="2026-09-18 11:24+00"
HERE=$(cd "$(dirname "$0")" && pwd)
TABLES="agent_runs agent_tasks agent_actions agent_memory signal_agent_memory signal_radar_topics
signal_generation_runs signals signal_evidence signal_feedback_events signal_training_examples
user_signal_states source_candidates source_candidate_articles users"

src() { docker exec -e PGOPTIONS='-c default_transaction_read_only=on' "$SRC_C" \
  psql -U "$PGUSER" -d "$SRC_DB" -X -v ON_ERROR_STOP=1 "$@"; }
dst() { docker exec -i "$DST_C" psql -U "$PGUSER" -d "$DST_DB" -X -v ON_ERROR_STOP=1 "$@"; }
cols() { src -At -c "SELECT string_agg(quote_ident(column_name), ',' ORDER BY ordinal_position)
  FROM information_schema.columns WHERE table_schema='public' AND table_name='$1'" < /dev/null; }

echo "== $(date -u '+%F %T') ${SRC_C}/${SRC_DB} → ${DST_C}/${DST_DB}"

# Работа пользователей в интерфейсе агентов после разделения, которую этот перенос не
# забирает (выпуски, отзывы, документы), — стоп: её надо переносить отдельно.
extra=$(src -At -c "SELECT
  (SELECT count(*) FROM monthly_digests WHERE created_at >= '$FORK') +
  (SELECT count(*) FROM monthly_digest_items di JOIN monthly_digests d ON d.id = di.digest_id
     WHERE d.created_at >= '$FORK' OR d.updated_at >= '$FORK') +
  (SELECT count(*) FROM feedback_entries WHERE created_at >= '$FORK') +
  (SELECT count(*) FROM documents WHERE created_at >= '$FORK')" < /dev/null)
[ "$extra" = "0" ] || { echo "СТОП: в базе агентов после 18.09 есть выпуски/отзывы/документы ($extra строк)"; exit 1; }

# Колонки сигналов, которых нет у основной базы (schema.sql:728–733). Весь init-db на живой
# базе не запускается: в schema.sql есть массовые UPDATE (например, пометка дублей статей).
dst -q < /dev/null -c "
ALTER TABLE signals ADD COLUMN IF NOT EXISTS merged_into_signal_id BIGINT REFERENCES signals(id) ON DELETE SET NULL;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS merge_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_signals_merged_into ON signals(merged_into_signal_id) WHERE merged_into_signal_id IS NOT NULL;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS interest_score NUMERIC;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS why_interesting TEXT;"

dst -q -c "DROP SCHEMA IF EXISTS agents_src CASCADE; CREATE SCHEMA agents_src;" < /dev/null
for t in $TABLES; do
  c=$(cols "$t")
  [ -n "$c" ] || { echo "СТОП: в источнике нет таблицы $t"; exit 1; }
  # Колонки — по источнику: если какой-то нет в основной базе, CREATE упадёт здесь, до замены.
  dst -q -c "CREATE TABLE agents_src.$t AS SELECT $c FROM public.$t WITH NO DATA" < /dev/null
  src -c "COPY public.$t ($c) TO STDOUT" < /dev/null | dst -q -c "COPY agents_src.$t ($c) FROM STDIN"
  echo "  $t: $(dst -At -c "SELECT count(*) FROM agents_src.$t" < /dev/null)"
done

# Статьи агентов после разделения (их номера совпадают с другими статьями основной базы)
# и отметки пользователей о статьях после разделения — для перевода по url_key.
dst -q -c "CREATE TABLE agents_src.article_keys (id BIGINT PRIMARY KEY, url_key TEXT)" < /dev/null
src -c "COPY (SELECT id, url_key FROM articles WHERE created_at >= '$FORK') TO STDOUT" < /dev/null \
  | dst -q -c "COPY agents_src.article_keys FROM STDIN"
c=$(cols user_article_states)
dst -q -c "CREATE TABLE agents_src.user_article_states AS SELECT $c FROM public.user_article_states WITH NO DATA" < /dev/null
src -c "COPY (SELECT $c FROM user_article_states WHERE updated_at >= '$FORK') TO STDOUT" < /dev/null \
  | dst -q -c "COPY agents_src.user_article_states ($c) FROM STDIN"
echo "  article_keys: $(dst -At -c "SELECT count(*) FROM agents_src.article_keys" < /dev/null)," \
  "user_article_states: $(dst -At -c "SELECT count(*) FROM agents_src.user_article_states" < /dev/null)"

echo "== $(date -u '+%F %T') замена одной транзакцией"
dst -1 -f - < "$HERE/migrate-agents-to-main.sql"
echo "== $(date -u '+%F %T') готово. Схема agents_src оставлена для сверки; убрать: DROP SCHEMA agents_src CASCADE"
