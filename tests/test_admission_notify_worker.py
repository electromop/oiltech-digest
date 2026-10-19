"""Отправка кода допуска на NL (#113): обработчик задачи admission_notify с подставным Telegram."""

import pytest
import requests

from oiltech_digest import admission, config, contract, external_worker, lanes

TOKEN = "123456:secret-bot-token"


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture()
def telegram(monkeypatch):
    monkeypatch.setattr(config, "ADMISSION_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "ADMISSION_TELEGRAM_CHAT_ID", "-100777")
    calls = []
    answer = {"value": _Response(200, {"ok": True, "result": {"message_id": 42}})}

    def post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        if isinstance(answer["value"], Exception):
            raise answer["value"]
        return answer["value"]

    monkeypatch.setattr(requests, "post", post)
    return calls, answer


def test_one_send_message_request_to_configured_chat(telegram):
    calls, _ = telegram

    result = external_worker._handler("admission_notify")({"user_id": 5, "text": "Код: 12345678"})

    assert result == {"admission_notify": True, "message_id": 42}
    assert calls == [{"url": f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      "json": {"chat_id": "-100777", "text": "Код: 12345678"}, "timeout": 20}]


@pytest.mark.parametrize("reply", [
    _Response(502, ValueError("not json")),
    _Response(400, {"ok": False, "description": f"Bad Request: chat not found for bot{TOKEN}"}),
    requests.ConnectionError(f"HTTPSConnectionPool: /bot{TOKEN}/sendMessage"),
])
def test_telegram_failure_is_retryable_error_without_token(telegram, reply):
    _, answer = telegram
    answer["value"] = reply

    with pytest.raises(admission.TelegramError) as caught:
        admission.send_notification({"text": "Код: 12345678"})

    assert TOKEN not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_missing_bot_settings_fail_the_job(monkeypatch):
    monkeypatch.setattr(config, "ADMISSION_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "ADMISSION_TELEGRAM_CHAT_ID", "")

    with pytest.raises(admission.TelegramError):
        admission.send_notification({"text": "Код: 12345678"})


def test_worker_reports_telegram_failure_as_retryable(telegram):
    _, answer = telegram
    answer["value"] = _Response(500, {"ok": False, "description": "Internal"})
    reported = {}

    class Client:
        def progress(self, job, value):
            pass

        def fail(self, job, error, *, retryable=True, **_):
            reported.update(error=error, retryable=retryable)

        def complete(self, job, result):
            reported.update(complete=result)

    job = {"id": 9, "kind": "admission_notify", "payload": {"text": "Код: 12345678"}, "lease_token": "t"}
    external_worker.worker_shutdown.SHUTDOWN.track(Client(), job)
    try:
        external_worker._run_job(Client(), job, lambda done=None: None)
    finally:
        external_worker.worker_shutdown.SHUTDOWN.untrack(job)

    assert reported["retryable"] is True
    assert "Telegram" in reported["error"]
    assert "complete" not in reported


def test_contract_three_and_notify_lane():
    assert contract.CONTRACT == 3
    assert lanes.serves("external-notify", "admission_notify")
    assert not lanes.serves("external-fetch", "admission_notify")
    assert "external-notify" not in lanes.AI_LANES
    assert lanes.STALE_AFTER_MINUTES["external-notify"] == 10
