#!/bin/sh
# Локальный запуск приложения для проверки приёма файлов глазами.
# Внешний контур включён нарочно: без него загрузка отклоняется на входе (и правильно —
# задача без внешнего исполнителя не откладывается, а гибнет).
# Обработку после загрузки запускать вручную, локального обработчика у неё нет.
cd "$(dirname "$0")/.."
export UPLOAD_DIR="${UPLOAD_DIR:-$PWD/.local-documents}"
export EXTERNAL_WORKERS_ENABLED=true
export AI_EXECUTION_REGION=external
mkdir -p "$UPLOAD_DIR"
exec .venv/bin/python -m uvicorn oiltech_digest.api:app --host 127.0.0.1 --port 8011
