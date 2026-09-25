#!/bin/sh
set -u

# Ровно один планировщик (ADR 0001, п. 4): весь процесс — и циклы, и паузы — идёт под
# advisory lock в Postgres. 21.09 второй планировщик, поднятый лишним `compose up`, 8,5 ч
# дублировал сбор и ИИ. Второй экземпляр пишет в лог, у кого замок, и ждёт, не делая ни
# одного шага. Обёртка заодно передаёт SIGTERM шагам: shell под PID 1 его игнорировал.
if [ "${SCHEDULER_LOCK_HELD:-0}" != "1" ]; then
  exec python -m oiltech_digest.cli scheduler-lock -- "$0" "$@"
fi

log() {
  printf '%s %s\n' "$(date -Iseconds)" "$*"
}

# Шаг идёт под сроком (oiltech_digest/step_timeout.py): дольше STEP_TIMEOUT_SECONDS —
# SIGTERM, через STEP_KILL_AFTER_SECONDS — SIGKILL, код 124, и цикл идёт дальше. 24.09
# parse не вернулся 20 ч 45 мин — стоял весь цикл. Код шага берётся до всякого `if`:
# `code="$?"` после `if …; fi` давал 0 (так по POSIX выходит `if` без ветки) — лог писал
# «exit=0», а run_required_step не останавливал скрипт никогда.
run_step() {
  name="$1"
  shift
  log "START ${name}"
  python -m oiltech_digest.step_timeout "$STEP_TIMEOUT_SECONDS" "$STEP_KILL_AFTER_SECONDS" -- "$@"
  code="$?"
  if [ "$code" -eq 0 ]; then
    log "OK ${name}"
    return 0
  fi
  if [ "$code" -eq 124 ]; then
    log "TIMEOUT ${name}: дольше ${STEP_TIMEOUT_SECONDS} с — шаг снят"
  fi
  log "FAIL ${name} exit=${code}"
  return "$code"
}

run_required_step() {
  name="$1"
  shift
  run_step "$name" "$@" || exit 1
}

CYCLE_INTERVAL_SECONDS="${CYCLE_INTERVAL_SECONDS:-21600}"
# Потолок шага. Норма на проде (сентябрь): parse 620–744 с, discover-rss ~400 с, прочие
# до 130 с — час даёт запас впятеро. 0 — без потолка.
STEP_TIMEOUT_SECONDS="${STEP_TIMEOUT_SECONDS:-3600}"
STEP_KILL_AFTER_SECONDS="${STEP_KILL_AFTER_SECONDS:-30}"
RUN_DISCOVER_ON_START="${RUN_DISCOVER_ON_START:-1}"
DISCOVER_EVERY_CYCLES="${DISCOVER_EVERY_CYCLES:-4}"
RUN_MAINTENANCE_ON_START="${RUN_MAINTENANCE_ON_START:-1}"
MAINTENANCE_EVERY_CYCLES="${MAINTENANCE_EVERY_CYCLES:-24}"
DISCOVER_TIMEOUT="${DISCOVER_TIMEOUT:-4}"
DISCOVER_WORKERS="${DISCOVER_WORKERS:-10}"
PARSE_WORKERS="${PARSE_WORKERS:-10}"
FULL_TEXT_LIMIT="${FULL_TEXT_LIMIT:-200}"
FULL_TEXT_MIN_CHARS="${FULL_TEXT_MIN_CHARS:-800}"
AI_PROCESS_LIMIT="${AI_PROCESS_LIMIT:-100}"
AI_OFFLINE="${AI_OFFLINE:-0}"
SKIP_BOOTSTRAP="${SKIP_BOOTSTRAP:-0}"
# STREAMING_PIPELINE=1 заменяет parse+fetch-full-text+process на единый parse-process.
# На сервере с 1.9 ГБ RAM рекомендуется PARSE_WORKERS<=5 при стриминге.
STREAMING_PIPELINE="${STREAMING_PIPELINE:-0}"
STREAM_POLL_INTERVAL="${STREAM_POLL_INTERVAL:-10}"
STREAM_PROCESS_BATCH="${STREAM_PROCESS_BATCH:-20}"
# FULLTEXT_RETRY_TOO_SHORT=1 — повторять попытку для статей со статусом too_short
# (полезно после добавления trafilatura — запустить один раз вручную).
FULLTEXT_RETRY_TOO_SHORT="${FULLTEXT_RETRY_TOO_SHORT:-0}"
# FETCH_EXTERNAL_ENABLED=1 — источники network_region='external' (западные WAF/таймаут
# с РФ-сервера) фетчатся через зарубежный воркер. Шаг enqueue-external-scrape ставит
# их в external-fetch/external-playwright; команда сама no-op при выключенном контуре.
FETCH_EXTERNAL_ENABLED="${FETCH_EXTERNAL_ENABLED:-0}"
EXTERNAL_REFETCH_LIMIT="${EXTERNAL_REFETCH_LIMIT:-100}"
# Перепечатки (№21): одна новость, разошедшаяся по изданиям. Правило сужает корпус
# до десятков пар, решает модель, копия помечается (не удаляется) и уходит из ленты.
# Окно намеренно шире периода запуска: уже помеченные пары правило не выдаёт
# повторно, поэтому перекрытие почти ничего не стоит, а пропуск дубля стоит того,
# что заказчик снова видит четыре карточки одной новости.
# Раз в REPRINTS_INTERVAL_HOURS часов по времени прошлого прогона в базе (0 — выключено).
REPRINTS_INTERVAL_HOURS="${REPRINTS_INTERVAL_HOURS:-12}"
REPRINTS_DAYS="${REPRINTS_DAYS:-7}"
REPRINTS_LIMIT="${REPRINTS_LIMIT:-200}"

if [ "$SKIP_BOOTSTRAP" != "1" ]; then
  log "Bootstrapping database and seed data"
  run_required_step "init-db" python -m oiltech_digest.cli init-db
  run_required_step "seed-sources" python -m oiltech_digest.cli seed-sources
  run_required_step "seed-tags" python -m oiltech_digest.cli seed-tags
  run_required_step "seed-scoring" python -m oiltech_digest.cli seed-scoring
  run_step "apply-source-overrides" python -m oiltech_digest.cli apply-source-overrides
fi

cycle=0
while true; do
  log "Cycle ${cycle} started"

  if [ "$RUN_MAINTENANCE_ON_START" = "1" ] && [ "$cycle" -eq 0 ]; then
    run_step "maintenance-cleanup" python -m oiltech_digest.cli maintenance-cleanup
  elif [ "$MAINTENANCE_EVERY_CYCLES" -gt 0 ] && [ $((cycle % MAINTENANCE_EVERY_CYCLES)) -eq 0 ]; then
    run_step "maintenance-cleanup" python -m oiltech_digest.cli maintenance-cleanup
  fi

  if [ "$RUN_DISCOVER_ON_START" = "1" ] && [ "$cycle" -eq 0 ]; then
    run_step "discover-rss" python -m oiltech_digest.cli discover-rss --workers "$DISCOVER_WORKERS" --timeout "$DISCOVER_TIMEOUT"
  elif [ "$DISCOVER_EVERY_CYCLES" -gt 0 ] && [ $((cycle % DISCOVER_EVERY_CYCLES)) -eq 0 ]; then
    run_step "discover-rss" python -m oiltech_digest.cli discover-rss --workers "$DISCOVER_WORKERS" --timeout "$DISCOVER_TIMEOUT"
  fi

  if [ "$STREAMING_PIPELINE" = "1" ]; then
    # Стриминг: parse + AI-обработка параллельно в одном процессе.
    if [ "$AI_PROCESS_LIMIT" -gt 0 ] && { [ "$AI_OFFLINE" = "1" ] || [ -n "${OPENAI_API_KEY:-}" ]; }; then
      _offline_flag=""
      [ "$AI_OFFLINE" = "1" ] && _offline_flag="--offline"
      run_step "parse-process" python -m oiltech_digest.cli parse-process \
        --workers "$PARSE_WORKERS" \
        --process-limit "$STREAM_PROCESS_BATCH" \
        --poll-interval "$STREAM_POLL_INTERVAL" \
        ${_offline_flag}
    else
      run_step "parse" python -m oiltech_digest.cli parse --workers "$PARSE_WORKERS"
      log "SKIP process: OPENAI_API_KEY is empty and AI_OFFLINE!=1"
    fi
  else
    # Классический последовательный режим.
    run_step "parse" python -m oiltech_digest.cli parse --workers "$PARSE_WORKERS"
    _retry_flag=""
    [ "$FULLTEXT_RETRY_TOO_SHORT" = "1" ] && _retry_flag="--retry-too-short"
    run_step "fetch-full-text" python -m oiltech_digest.cli fetch-full-text --limit "$FULL_TEXT_LIMIT" --min-chars "$FULL_TEXT_MIN_CHARS" ${_retry_flag}

    if [ "$AI_PROCESS_LIMIT" -gt 0 ]; then
      if [ "${AI_EXECUTION_REGION:-ru}" = "external" ]; then
        # Внешний контур: РФ-core не зовёт OpenAI сам, а ставит задачу в external-ai —
        # её заберёт зарубежный worker.
        run_step "enqueue-process" python -m oiltech_digest.cli enqueue-process --limit "$AI_PROCESS_LIMIT"
      elif [ "$AI_OFFLINE" = "1" ]; then
        run_step "process-offline" python -m oiltech_digest.cli process --offline --limit "$AI_PROCESS_LIMIT"
      elif [ -n "${OPENAI_API_KEY:-}" ]; then
        run_step "process" python -m oiltech_digest.cli process --limit "$AI_PROCESS_LIMIT"
      else
        log "SKIP process: OPENAI_API_KEY is empty"
      fi
    fi
  fi

  if [ "$FETCH_EXTERNAL_ENABLED" = "1" ]; then
    # Западные источники (network_region='external') фетчим через зарубежный воркер —
    # с РФ-сервера к ним нет доступа. Задачи разберёт NL external-worker.
    run_step "enqueue-external-scrape" python -m oiltech_digest.cli enqueue-external-scrape
    # Обрывки у тех же источников: лента даёт анонс, а локальная дозагрузка их не
    # берёт (403 с РФ-адреса, попытка одна навсегда). Тело добирает воркер.
    run_step "enqueue-external-refetch" python -m oiltech_digest.cli enqueue-external-refetch \
      --limit "$EXTERNAL_REFETCH_LIMIT"
  fi

  # Срок — от прошлого прогона с записью в базе, а не «каждый 24-й цикл»: счётчик
  # обнулялся при каждом перезапуске, а цикл идёт ~41 мин, а не 30 — «дважды в сутки»
  # на деле выходило раз в 16,5 ч и сдвигалось каждым выкатом (19.09). Повтор при
  # перезапуске исключает та же проверка по базе.
  if [ "$REPRINTS_INTERVAL_HOURS" != "0" ]; then
    if [ "$AI_OFFLINE" = "1" ] || [ -n "${OPENAI_API_KEY:-}" ]; then
      run_step "find-reprints" python -m oiltech_digest.cli find-reprints \
        --days "$REPRINTS_DAYS" --limit "$REPRINTS_LIMIT" --apply \
        --min-interval-hours "$REPRINTS_INTERVAL_HOURS"
    else
      log "SKIP find-reprints: OPENAI_API_KEY is empty"
    fi
  fi

  # Сторож полос: застой внешней очереди или очередь без живого воркера — «FAIL
  # check-lanes» и строки ТРЕВОГА в логе (цикл не прерывается).
  run_step "check-lanes" python -m oiltech_digest.cli check-lanes

  run_step "stats" python -m oiltech_digest.cli stats
  cycle=$((cycle + 1))
  log "Cycle finished. Sleeping ${CYCLE_INTERVAL_SECONDS}s"
  sleep "$CYCLE_INTERVAL_SECONDS"
done
