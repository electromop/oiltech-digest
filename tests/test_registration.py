"""Регистрация на экране входа (решение владельца 16.11; до того закрыта по #33).

Ворота: код допуска (#113, tests/test_admission.py). Учётка из регистрации с ролью user, без допуска и с пометкой
self_registered; её код уходит подтверждающим с пометкой «завели сами», для таких учёток действуют потолки
регистраций и сообщений за час. Шов: API и команды на изолированной базе."""

import threading

import pytest

from oiltech_digest import admission, cli, config
from oiltech_digest.db import repository
from tests.test_admission import EMAIL, PASSWORD, _admit, _admitted, _as, _client, _code_from, _code_row, _login
from tests.test_admission import _notify_jobs, _sql


@pytest.fixture()
def notify_on(monkeypatch):
    monkeypatch.setattr(config, "ADMISSION_NOTIFY_ENABLED", True)


def _register(client, email=EMAIL, password=PASSWORD):
    return client.post("/api/auth/register", json={"email": email, "password": password})


def test_registration_creates_user_without_admission_and_code_goes_to_confirmers(isolated_db, notify_on):
    client = _client()

    response = _register(client, email=" Newcomer.Person@Example.test ")

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": False, "admission_required": True, "detail": admission.REQUIRED}
    assert config.AUTH_COOKIE_NAME not in response.cookies
    assert client.get("/api/auth/me").status_code == 401
    assert _sql("SELECT count(*) FROM user_sessions")[0][0] == 0
    user_id, role, admitted_at, self_registered = _sql(
        "SELECT id, role, admitted_at, self_registered FROM users WHERE email = %s", (EMAIL,))[0]
    assert (role, admitted_at, self_registered) == ("user", None, True)
    jobs = _notify_jobs()
    assert len(jobs) == 1
    assert f"Учётка №{user_id} {EMAIL} ждёт допуска." in jobs[0][3]["text"]
    assert admission.REGISTERED_LINE in jobs[0][3]["text"]

    admitted = _admit(client, _code_from(jobs[0]))

    assert admitted.status_code == 200, admitted.text
    assert client.get("/api/auth/me").json()["user"]["email"] == EMAIL
    assert _admitted(user_id)


def test_registration_without_notify_flag_asks_confirmers(isolated_db):
    response = _register(_client())

    assert response.json()["detail"] == admission.REQUIRED_ASK
    assert _notify_jobs() == []


def test_taken_email_gets_honest_409_and_account_stays_as_it_was(isolated_db, notify_on):
    """Скрыть занятую почту без подтверждения почты письмом нельзя: вход тем же паролем всё равно отличит. Честный
    ответ сразу говорит владельцу, что его почту заняли."""
    _register(_client())

    response = _register(_client(), email=EMAIL.upper(), password="another-long-password")

    assert response.status_code == 409
    assert "уже есть" in response.json()["detail"]
    assert _sql("SELECT count(*) FROM users")[0][0] == 1
    assert len(_notify_jobs()) == 1
    assert repository.authenticate_user(EMAIL, PASSWORD) is not None
    assert repository.authenticate_user(EMAIL, "another-long-password") is None


def test_registration_ignores_role_in_request(isolated_db):
    response = _client().post("/api/auth/register", json={"email": EMAIL, "password": PASSWORD, "role": "admin"})

    assert response.status_code == 200, response.text
    assert _sql("SELECT role, admitted_at FROM users WHERE email = %s", (EMAIL,))[0] == ("user", None)


def test_closed_by_flag_registration_creates_nothing(isolated_db, notify_on, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ALLOW_SELF_REGISTRATION", False)

    response = _register(_client())

    assert response.status_code == 403
    assert "администратор" in response.json()["detail"]
    assert _sql("SELECT count(*) FROM users")[0][0] == 0
    assert _notify_jobs() == []


def test_registrations_over_hourly_cap_get_429_and_create_nothing(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "AUTH_SELF_REGISTRATION_PER_HOUR", 2)
    client = _client()
    assert _register(client, email="one@example.test").status_code == 200
    assert _register(client, email="two@example.test").status_code == 200
    # Учётки от администратора в потолок не идут.
    repository.create_user("colleague@example.test", PASSWORD, "user")

    response = _register(client, email="three@example.test")

    assert response.status_code == 429
    assert "Попробуйте через 60 мин." in response.json()["detail"]
    assert 3500 < int(response.headers["Retry-After"]) <= 3600
    assert _sql("SELECT count(*) FROM users WHERE self_registered")[0][0] == 2
    # Допущенные в потолок тоже не идут: подтверждающие разобрали заявки, регистрация снова открыта.
    _sql("UPDATE users SET admitted_at = now()")
    assert _register(client, email="three@example.test").status_code == 200


def test_cap_holds_under_simultaneous_registrations(isolated_db):
    """Подсчёт и вставка под одним замком: пачка одновременных заявок не проходит мимо потолка."""
    outcomes, lock = [], threading.Lock()

    def register(n):
        try:
            repository.register_user(f"bot{n}@example.test", PASSWORD, per_hour=3)
            result = "created"
        except repository.RegistrationsCapped:
            result = "capped"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=register, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["capped"] * 5 + ["created"] * 3
    assert _sql("SELECT count(*) FROM users")[0][0] == 3


def test_code_cap_stops_only_self_registered_accounts(isolated_db, notify_on, monkeypatch):
    """Потолок сообщений за час: сверх него учётке из регистрации код не выдаётся (выдаёт команда), а учётке от
    администратора выдаётся как прежде."""
    monkeypatch.setattr(config, "ADMISSION_NOTIFY_PER_HOUR", 1)
    _register(_client(), email="one@example.test")

    second = _register(_client(), email="two@example.test")
    second_id = _sql("SELECT id FROM users WHERE email = 'two@example.test'")[0][0]
    colleague = repository.create_user("colleague@example.test", PASSWORD, "user")
    colleague_login = _login(_client(), email="colleague@example.test")

    assert second.json()["detail"] == admission.REQUIRED_ASK
    assert _code_row(second_id) is None
    assert colleague_login.json()["detail"] == admission.REQUIRED
    assert _code_row(colleague["id"]) is not None
    assert len(_notify_jobs()) == 2


def test_every_code_of_self_registered_account_carries_the_warning(isolated_db, notify_on, capsys):
    """Пометка «завели сами» не только в первом сообщении: и в коде после входа паролем, и в выводе команды."""
    _register(_client())
    _sql("UPDATE admission_codes SET created_at = now() - interval '25 hours', expires_at = now() - interval '1 hour'")

    _login(_client())
    cli.main(["admission-code", "--email", EMAIL])

    texts = [job[3]["text"] for job in _notify_jobs()]
    assert len(texts) == 2
    assert all(admission.REGISTERED_LINE in text for text in texts)
    assert admission.REGISTERED_LINE in capsys.readouterr().out


def test_users_list_shows_who_registered_and_waits_for_code(isolated_db):
    admin = repository.create_user("admin@example.test", PASSWORD, "admin")
    admission.admit(admin["id"])
    _register(_client(), email="stranger@example.test")

    try:
        rows = {row["email"]: row for row in _as(admin).get("/api/users").json()["users"]}
    finally:
        from oiltech_digest import api
        api.app.dependency_overrides.pop(api.require_user, None)

    assert rows["stranger@example.test"]["admitted_at"] is None
    assert rows["stranger@example.test"]["self_registered"] is True
    assert rows["stranger@example.test"]["role"] == "user"
    assert rows["admin@example.test"]["admitted_at"] is not None
    assert rows["admin@example.test"]["self_registered"] is False


def test_first_admin_is_never_taken_from_self_registered_accounts(isolated_db):
    """Свежая установка: посторонний зарегистрировался раньше, чем завели администратора; init-db его админом не
    делает."""
    _register(_client(), email="stranger@example.test")

    assert repository.ensure_admin_bootstrap() is None

    colleague = repository.create_user("colleague@example.test", PASSWORD, "user")
    assert repository.ensure_admin_bootstrap() == colleague["id"]
    assert _sql("SELECT role FROM users WHERE email = 'stranger@example.test'")[0][0] == "user"


def test_create_user_command_follows_the_same_email_and_password_rules(isolated_db, capsys):
    with pytest.raises(SystemExit):
        cli.main(["create-user", "--email", "<b>x</b>@example.test", "--password", PASSWORD])
    with pytest.raises(SystemExit):
        cli.main(["create-user", "--email", EMAIL, "--password", "short"])

    assert _sql("SELECT count(*) FROM users")[0][0] == 0
