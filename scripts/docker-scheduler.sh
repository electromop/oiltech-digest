#!/bin/sh
set -u

log() {
  printf '%s %s\n' "$(date -Iseconds)" "$*"
}

run_step() {
  name="$1"
  shift
  log "START ${name}"
  if "$@"; then
    log "OK ${name}"
    return 0
  fi
  code="$?"
  log "FAIL ${name} exit=${code}"
  return "$code"
}

run_required_step() {
  name="$1"
  shift
  run_step "$name" "$@" || exit 1
}

CYCLE_INTERVAL_SECONDS="${CYCLE_INTERVAL_SECONDS:-21600}"
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
      if [ "$AI_OFFLINE" = "1" ]; then
        run_step "process-offline" python -m oiltech_digest.cli process --offline --limit "$AI_PROCESS_LIMIT"
      elif [ -n "${OPENAI_API_KEY:-}" ]; then
        run_step "process" python -m oiltech_digest.cli process --limit "$AI_PROCESS_LIMIT"
      else
        log "SKIP process: OPENAI_API_KEY is empty"
      fi
    fi
  fi

  run_step "stats" python -m oiltech_digest.cli stats
  cycle=$((cycle + 1))
  log "Cycle finished. Sleeping ${CYCLE_INTERVAL_SECONDS}s"
  sleep "$CYCLE_INTERVAL_SECONDS"
done
