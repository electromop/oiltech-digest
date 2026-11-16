"""Лимиты входа и регистрации (16.11.2026, защита входа «по полному»): счётчики в базе (auth_attempts).

С одного адреса: заявок регистрации и неудачных попыток входа за час; на одну почту: неудачных паролей за окно, считая
после последнего верного пароля, и знакомый адрес паузой почты не закрывается. Адрес клиента ставит uvicorn по
X-Forwarded-For от Caddy; в тестах его задаёт TestClient(client=...). Время двигаем в базе, как в тестах допуска."""

import logging
import threading

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import admission, api, auth, auth_limits, cli, config
from oiltech_digest.db import repository
from oiltech_digest.maintenance import maintenance_cleanup
from tests.test_admission import _sql

HOME = "198.51.100.7"
OFFICE = "198.51.100.8"
ATTACKERS = ("203.0.113.1", "203.0.113.2", "203.0.113.3", "203.0.113.4", "203.0.113.5")
CADDY = "172.19.0.9"
EMAIL = "member@example.test"
PASSWORD = "Kx7#pLm2qRt9"


def _client(ip):
    return TestClient(api.app, client=(ip, 50000))


def _login(ip, email=EMAIL, password=PASSWORD):
    return _client(ip).post("/api/auth/login", json={"email": email, "password": password})


def _register(ip, email, password=PASSWORD):
    return _client(ip).post("/api/auth/register", json={"email": email, "password": password})


def _age(minutes):
    _sql("UPDATE auth_attempts SET created_at = created_at - make_interval(mins => %s)", (minutes,))


def _limits(monkeypatch, *, per_ip=100, per_email=100, register_per_ip=100):
    monkeypatch.setattr(config, "AUTH_FAILURES_PER_IP_HOUR", per_ip)
    monkeypatch.setattr(config, "AUTH_FAILURES_PER_EMAIL", per_email)
    monkeypatch.setattr(config, "AUTH_REGISTER_PER_IP_HOUR", register_per_ip)


@pytest.fixture()
def member(isolated_db):
    user = repository.create_user(EMAIL, PASSWORD, "user")
    admission.admit(int(user["id"]))
    return user


# --- адрес клиента --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("host, expected", [
    ("198.51.100.7", "198.51.100.7"),
    ("::ffff:198.51.100.7", "198.51.100.7"),
    ("2001:db8::5", "2001:db8::5"),
    ("172.19.0.9", None),
    ("10.1.2.3", None),
    ("192.168.1.10", None),
    ("127.0.0.1", None),
    ("::1", None),
    ("fd00::7", None),
    ("testclient", None),
    ("", None),
    (None, None),
])
def test_client_address_is_public_ip_or_nothing(host, expected):
    assert auth_limits.client_address(host) == expected


# --- вход: лимит адреса ----------------------------------------------------------------------------------------------


def test_failed_logins_from_one_address_stop_at_the_limit_before_the_password_check(isolated_db, monkeypatch):
    _limits(monkeypatch, per_ip=3)
    for n in range(3):
        assert _login(HOME, email=f"guess{n}@example.test").status_code == 401

    checks = []
    real_verify = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda *args: checks.append(args) or False)
    refused = _login(HOME, email="guess9@example.test")
    refused_right_password = _login(HOME, email="guess0@example.test", password=PASSWORD)
    monkeypatch.setattr(auth, "verify_password", real_verify)

    assert refused.status_code == 429
    assert "с вашего адреса" in refused.json()["detail"]
    assert 0 < int(refused.headers["Retry-After"]) <= 3600
    assert refused_right_password.status_code == 429
    assert checks == []
    assert _login(OFFICE, email="guess9@example.test").status_code == 401


def test_successful_logins_do_not_spend_the_address_limit(member, monkeypatch):
    """Офис заказчика за одним адресом: удачные входы коллег лимит адреса не тратят."""
    _limits(monkeypatch, per_ip=3)
    assert _login(OFFICE, password="wrong-password-1").status_code == 401
    assert _login(OFFICE, password="wrong-password-2").status_code == 401
    for _ in range(5):
        assert _login(OFFICE).status_code == 200

    assert _login(OFFICE, password="wrong-password-3").status_code == 401
    assert _login(OFFICE, password="wrong-password-4").status_code == 429


def test_address_limit_frees_after_an_hour(isolated_db, monkeypatch):
    _limits(monkeypatch, per_ip=2)
    _login(HOME, email="a@example.test")
    _login(HOME, email="b@example.test")
    assert _login(HOME, email="c@example.test").status_code == 429

    _age(61)

    assert _login(HOME, email="c@example.test").status_code == 401


def test_concurrent_attempts_cannot_overshoot_the_address_limit(isolated_db, monkeypatch):
    """Попытка записывается до проверки пароля под замком адреса: пачка одновременных запросов лимит не проскочит."""
    _limits(monkeypatch, per_ip=5)
    passed, refused = [], []
    barrier = threading.Barrier(20)

    def attempt(n):
        barrier.wait()
        try:
            passed.append(auth_limits.begin_sign_in("login", HOME, f"burst{n}@example.test"))
        except auth_limits.Refused:
            refused.append(n)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert (len(passed), len(refused)) == (5, 15)


# --- вход: пауза почты -----------------------------------------------------------------------------------------------


def test_failures_on_one_email_pause_it_from_any_new_address(member, monkeypatch):
    _limits(monkeypatch, per_email=3)
    for ip in ATTACKERS[:3]:
        assert _login(ip, password="wrong-password").status_code == 401

    paused = _login(ATTACKERS[3])

    assert paused.status_code == 429
    assert "с этой почтой" in paused.json()["detail"]
    assert 0 < int(paused.headers["Retry-After"]) <= 15 * 60

    _age(16)

    assert _login(ATTACKERS[3]).status_code == 200


def test_unknown_email_is_paused_the_same_way(isolated_db, monkeypatch):
    """Пауза по почте, а не по учётке: несуществующая почта ведёт себя так же, и по паузе учётку не узнать."""
    _limits(monkeypatch, per_email=3)
    for ip in ATTACKERS[:3]:
        assert _login(ip, email="ghost@example.test").status_code == 401

    assert _login(ATTACKERS[3], email="ghost@example.test").status_code == 429


def test_right_password_resets_the_email_window(member, monkeypatch):
    _limits(monkeypatch, per_email=3)
    _login(ATTACKERS[0], password="wrong-password")
    _login(ATTACKERS[1], password="wrong-password")
    assert _login(HOME).status_code == 200

    assert _login(ATTACKERS[2], password="wrong-password").status_code == 401
    assert _login(ATTACKERS[3], password="wrong-password").status_code == 401
    assert _login(ATTACKERS[4], password="wrong-password").status_code == 401
    assert _login(ATTACKERS[4]).status_code == 429


def test_known_address_is_not_paused_but_a_new_one_is(member, monkeypatch):
    """Злоумышленник, зная почту, не закроет вход человеку с адреса, откуда тот уже входил с верным паролем."""
    _limits(monkeypatch, per_email=3)
    assert _login(HOME).status_code == 200
    for ip in ATTACKERS[:3]:
        _login(ip, password="wrong-password")

    assert _login(OFFICE).status_code == 429
    assert _login(HOME).status_code == 200


def test_known_address_expires(member, monkeypatch):
    _limits(monkeypatch, per_email=3)
    assert _login(HOME).status_code == 200
    _age(config.AUTH_KNOWN_ADDRESS_DAYS * 24 * 60 + 1)
    for ip in ATTACKERS[:3]:
        _login(ip, password="wrong-password")

    assert _login(HOME).status_code == 429


# --- регистрация -----------------------------------------------------------------------------------------------------


def test_registrations_from_one_address_stop_at_the_limit(isolated_db, monkeypatch):
    _limits(monkeypatch, register_per_ip=2)
    for _ in range(3):
        assert _register(HOME, "not-an-email").status_code == 400
    assert _register(HOME, "first@example.test").status_code == 200
    assert _register(HOME, "second@example.test").status_code == 200

    refused = _register(HOME, "third@example.test")

    assert refused.status_code == 429
    assert "регистраций с вашего адреса" in refused.json()["detail"]
    assert int(refused.headers["Retry-After"]) > 0
    assert _sql("SELECT count(*) FROM users WHERE email = 'third@example.test'")[0][0] == 0
    assert _register(OFFICE, "third@example.test").status_code == 200


def test_taken_email_counts_as_a_registration_attempt(isolated_db, monkeypatch):
    """Честный 409 показывает, что почта занята: лимит адреса ограничивает перебор почт через регистрацию."""
    _limits(monkeypatch, register_per_ip=2)
    repository.create_user("taken@example.test", PASSWORD, "user")
    assert _register(HOME, "taken@example.test").status_code == 409
    assert _register(HOME, "taken@example.test").status_code == 409

    assert _register(HOME, "taken@example.test").status_code == 429


# --- внутренний адрес: заголовку не поверили --------------------------------------------------------------------------


def test_internal_address_turns_off_address_limits_but_not_the_email_pause(member, monkeypatch, caplog):
    """Если compose потеряет FORWARDED_ALLOW_IPS, все клиенты станут адресом Caddy. Лимит адреса тогда закрыл бы вход
    всем после чужих ошибок, поэтому он не действует (и знакомый адрес тоже), а в журнале предупреждение."""
    _limits(monkeypatch, per_ip=2, per_email=3, register_per_ip=1)
    monkeypatch.setattr(auth_limits, "_last_warning", float("-inf"))
    with caplog.at_level(logging.WARNING, logger="oiltech_digest.auth_limits"):
        for n in range(4):
            assert _login(CADDY, email=f"guess{n}@example.test").status_code == 401
        assert _register(CADDY, "one@example.test").status_code == 200
        assert _register(CADDY, "two@example.test").status_code == 200

    assert "FORWARDED_ALLOW_IPS" in caplog.text
    assert _login(CADDY).status_code == 200
    for _ in range(3):
        _login(CADDY, password="wrong-password")
    assert _login(CADDY).status_code == 429


# --- код допуска -----------------------------------------------------------------------------------------------------


def test_wrong_codes_count_for_the_address_but_do_not_pause_the_email(isolated_db, monkeypatch):
    _limits(monkeypatch, per_ip=3, per_email=2)
    user = repository.create_user(EMAIL, PASSWORD, "user")
    code, _ = admission.issue_code(int(user["id"]), force=True)
    wrong = "00000000" if code != "00000000" else "11111111"

    def admit(ip, entered):
        return _client(ip).post("/api/auth/admit", json={"email": EMAIL, "password": PASSWORD, "code": entered})

    assert admit(HOME, wrong).status_code == 400
    assert admit(HOME, wrong).status_code == 400
    assert admit(HOME, wrong).status_code == 400
    assert admit(HOME, code).status_code == 429

    assert admit(OFFICE, code).status_code == 200


def test_admit_with_wrong_password_counts_for_the_email(isolated_db, monkeypatch):
    _limits(monkeypatch, per_email=2)
    user = repository.create_user(EMAIL, PASSWORD, "user")
    code, _ = admission.issue_code(int(user["id"]), force=True)

    def admit(ip, password):
        return _client(ip).post("/api/auth/admit", json={"email": EMAIL, "password": password, "code": code})

    assert admit(ATTACKERS[0], "wrong-password").status_code == 401
    assert admit(ATTACKERS[1], "wrong-password").status_code == 401
    assert admit(ATTACKERS[2], PASSWORD).status_code == 429


# --- команды и чистка ------------------------------------------------------------------------------------------------


def test_unlock_command_clears_the_email_pause(member, monkeypatch, capsys):
    _limits(monkeypatch, per_email=2)
    _login(ATTACKERS[0], password="wrong-password")
    _login(ATTACKERS[1], password="wrong-password")
    assert _login(OFFICE).status_code == 429

    cli.main(["auth-unlock", "--email", " Member@Example.test "])

    assert "снято: 2" in capsys.readouterr().out
    assert _login(OFFICE).status_code == 200


def test_unlock_command_clears_the_address_limit(isolated_db, monkeypatch, capsys):
    _limits(monkeypatch, per_ip=2)
    _login(HOME, email="a@example.test")
    _login(HOME, email="b@example.test")
    assert _login(HOME, email="c@example.test").status_code == 429

    cli.main(["auth-unlock", "--ip", HOME])

    assert "снято: 2" in capsys.readouterr().out
    assert _login(HOME, email="c@example.test").status_code == 401


def test_unlocking_an_address_keeps_the_email_reset_by_a_right_password(isolated_db, monkeypatch, capsys):
    """Верный пароль на шаге кода (неверный код) обнулил окно почты. Снятие лимита с адреса прощает его неудачи, но
    факт верного пароля остаётся: пауза почты от этого не возвращается."""
    _limits(monkeypatch, per_ip=3, per_email=2)
    user = repository.create_user(EMAIL, PASSWORD, "user")
    code, _ = admission.issue_code(int(user["id"]), force=True)
    wrong = "00000000" if code != "00000000" else "11111111"
    assert _login(ATTACKERS[0], password="wrong-password").status_code == 401
    assert _client(HOME).post("/api/auth/admit",
                              json={"email": EMAIL, "password": PASSWORD, "code": wrong}).status_code == 400
    assert _login(ATTACKERS[1], password="wrong-password").status_code == 401

    cli.main(["auth-unlock", "--ip", f"::ffff:{HOME}"])

    assert "снято: 1" in capsys.readouterr().out
    assert _login(OFFICE).status_code == 200


def test_unlock_command_needs_a_valid_target(isolated_db, capsys):
    with pytest.raises(SystemExit):
        cli.main(["auth-unlock"])
    with pytest.raises(SystemExit):
        cli.main(["auth-unlock", "--ip", "not-an-address"])

    assert "не адрес" in capsys.readouterr().out


def test_attempts_report_shows_addresses_and_emails(member, monkeypatch, capsys):
    _limits(monkeypatch, per_email=2)
    _login(ATTACKERS[0], password="wrong-password")
    _login(ATTACKERS[0], password="wrong-password")
    _login(HOME)

    cli.main(["auth-attempts", "--hours", "24"])

    out = capsys.readouterr().out
    assert ATTACKERS[0] in out
    assert EMAIL in out
    assert "пауза" in out


def test_maintenance_removes_attempts_older_than_retention(isolated_db):
    _login(HOME, email="old@example.test")
    _age(config.AUTH_ATTEMPTS_RETENTION_DAYS * 24 * 60 + 1)
    _login(HOME, email="fresh@example.test")

    result = maintenance_cleanup()

    assert result["auth_attempts"] == 1
    assert _sql("SELECT email FROM auth_attempts") == [("fresh@example.test",)]
