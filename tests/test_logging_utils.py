"""Подробный лог (-v) не пишет адреса HTTP-запросов: у Telegram в адресе токен бота.

Контейнеры NL запущены с -v. 20.10 бот «Материалы коллег» оставлял в логе строку urllib3
`POST /bot<токен>/getUpdates` на каждый опрос Bot API; тот же путь у отправки кода допуска
(admission.send_notification). Проверка — в отдельном процессе, как у контейнера: настройка
логов меняет корневой логгер процесса и задела бы соседние тесты.
"""

from __future__ import annotations

import http.server
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bot_api():
    """Подставной Bot API на localhost: принимает любой POST и отвечает пустым JSON."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - имя метода задаёт http.server
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true, "result": []}')

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_verbose_log_keeps_own_debug_but_not_bot_token_from_request_url(bot_api):
    code = (
        "import logging, requests\n"
        "from oiltech_digest.logging_utils import setup_logging\n"
        "setup_logging('cli', verbose=True)\n"
        f"requests.post('{bot_api}/bot123456:SECRET-BOT-TOKEN/getUpdates', timeout=10)\n"
        "logging.getLogger('oiltech_digest.tg_materials_bot').debug('свой подробный лог')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True,
                            text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert "свой подробный лог" in result.stderr
    assert "SECRET-BOT-TOKEN" not in result.stderr
