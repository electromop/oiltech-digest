"""Правило пароля (16.11.2026, защита входа «по полному»): длина и запрет слабых паролей.

Слабый пароль подбирают первыми попытками любого словаря, и лимиты входа его не спасают (10 попыток на почту за
15 минут хватает на 12345678). Правило действует при регистрации, заведении учётки и смене пароля; действующие пароли
не трогаются. Здесь же фиктивный хэш: неизвестная почта проверяется так же долго, как известная."""

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, auth, cli


@pytest.mark.parametrize("password", [
    "12345678", "123456789", "1234567890", "password", "Password1", "qwerty123", "QWERTY123", "1q2w3e4r", "1qaz2wsx",
    "iloveyou", "aaaaaaaa", "abababab", "11112222", "abcdefgh", "87654321", "123123123", "qweqweqwe", "qwertyuiop",
    "asdfghjkl", "йцукенгш", "пароль123", "password2026!", "01011990", "89991234567", "oiltech2026", "OilTech-Digest",
    " 12345678 ",
])
def test_weak_passwords_are_refused(password):
    assert auth.password_problem(password) == auth.WEAK_PASSWORD


@pytest.mark.parametrize("password", [
    "Kx7#pLm2qRt9", "лунный-кот-читает-сводки", "correct horse battery staple", "8fG2kq0vNw", "Морошка_на_льду_17",
    "tundra-Falcon-58", "0987plum!Kiwi",
])
def test_ordinary_passwords_pass(password):
    assert auth.password_problem(password) is None


def test_short_password_keeps_its_own_message():
    assert auth.password_problem("Kx7#pL") == auth.PASSWORD_TOO_SHORT
    assert auth.password_problem("") == auth.PASSWORD_TOO_SHORT


@pytest.mark.parametrize("password", ["ivan.petrov", "IvanPetrov", "ivan.petrov2026", "ivan.petrov@example.test"])
def test_own_email_is_not_a_password(password):
    assert auth.password_problem(password, "ivan.petrov@example.test") == auth.WEAK_PASSWORD
    assert auth.password_problem(password) in (None, auth.WEAK_PASSWORD)


def test_long_digit_string_is_not_weak_but_a_date_is():
    assert auth.password_problem("731958204615") is None
    assert auth.password_problem("16.11.2026") == auth.WEAK_PASSWORD


def test_unknown_email_costs_the_same_password_check(monkeypatch):
    """Почты нет в базе: проверка всё равно проходит PBKDF2, иначе по времени ответа видно, есть ли учётка."""
    calls = []
    real = auth.hash_password
    monkeypatch.setattr(auth, "hash_password", lambda password, salt_hex=None: calls.append(password) or real(
        password, salt_hex=salt_hex))

    assert auth.burn_password_check("any-password") is False
    assert calls == ["any-password"]


def test_authenticate_unknown_email_runs_the_dummy_check(isolated_db, monkeypatch):
    from oiltech_digest.db import repository

    calls = []
    monkeypatch.setattr(auth, "burn_password_check", lambda password: calls.append(password) or False)

    assert repository.authenticate_user("nobody@example.test", "Kx7#pLm2qRt9") is None
    assert calls == ["Kx7#pLm2qRt9"]


# --- где правило действует ------------------------------------------------------------------------------------------


def test_registration_refuses_weak_password(isolated_db):
    response = TestClient(api.app).post("/api/auth/register", json={"email": "new@example.test", "password": "qwerty123"})

    assert response.status_code == 400
    assert response.json()["detail"] == auth.WEAK_PASSWORD


def test_registration_refuses_own_email_as_password(isolated_db):
    response = TestClient(api.app).post("/api/auth/register",
                                        json={"email": "ivan.petrov@example.test", "password": "ivan.petrov2026"})

    assert response.status_code == 400
    assert response.json()["detail"] == auth.WEAK_PASSWORD


def test_admin_cannot_create_or_set_weak_password(monkeypatch):
    created, changed = [], []
    monkeypatch.setattr(api.repository, "create_user", lambda *args: created.append(args))
    monkeypatch.setattr(api.repository, "get_user_by_id", lambda user_id: {"id": user_id, "email": "u@example.test",
                                                                           "role": "user"})
    monkeypatch.setattr(api.repository, "set_user_password", lambda *args: changed.append(args))
    api.app.dependency_overrides[api.require_admin] = lambda: {"id": 1, "email": "admin@example.test", "role": "admin"}
    try:
        client = TestClient(api.app)
        created_response = client.post("/api/users", json={"email": "u@example.test", "password": "12345678"})
        changed_response = client.patch("/api/users/7", json={"password": "password1"})
    finally:
        api.app.dependency_overrides.pop(api.require_admin, None)

    assert created_response.status_code == 400
    assert created_response.json()["detail"] == auth.WEAK_PASSWORD
    assert changed_response.status_code == 400
    assert changed_response.json()["detail"] == auth.WEAK_PASSWORD
    assert created == [] and changed == []


def test_cli_create_user_refuses_weak_password(monkeypatch, capsys):
    created = []
    monkeypatch.setattr("oiltech_digest.db.repository.create_user", lambda *args: created.append(args))

    with pytest.raises(SystemExit):
        cli.main(["create-user", "--email", "u@example.test", "--password", "12345678"])

    assert auth.WEAK_PASSWORD in capsys.readouterr().out
    assert created == []
