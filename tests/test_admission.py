"""Допуск по коду (#113, спека #110): вход только допущенной учётки.

Шов — API на изолированной базе: вход, ввод кода, лимиты, смена пароля, задача на отправку
в очереди external-notify и стирание текста после неё. Время двигаем в базе, а не часами
процесса: срок кода и интервал выдачи считает Postgres (now())."""

import re
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from oiltech_digest import admission, api, config
from oiltech_digest.config import DATABASE_URL
from oiltech_digest.db import connection, repository

PASSWORD = "long-enough-password"
EMAIL = "newcomer.person@example.test"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "admission" / "admission-before-deploy.sql"


@pytest.fixture()
def notify_on(monkeypatch):
    monkeypatch.setattr(config, "ADMISSION_NOTIFY_ENABLED", True)


def _client():
    return TestClient(api.app)


def _login(client, email=EMAIL, password=PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def _admit(client, code, email=EMAIL, password=PASSWORD):
    return client.post("/api/auth/admit", json={"email": email, "password": password, "code": code})


def _sql(query, params=()):
    with connection.get_connection() as conn:
        rows = conn.execute(query, params).fetchall() if query.lstrip().upper().startswith("SELECT") else None
        if rows is None:
            conn.execute(query, params)
        conn.commit()
        return rows


def _notify_jobs():
    return _sql("SELECT id, queue_name, execution_region, payload_json, status FROM background_jobs "
                "WHERE kind = 'admission_notify' ORDER BY id")


def _code_from(job):
    return re.search(r"\b(\d{8})\b", job[3]["text"]).group(1)


def _code_row(user_id):
    rows = _sql("SELECT attempts, used_at, notify_job_id, sent_at, send_error FROM admission_codes WHERE user_id = %s",
                (user_id,))
    return rows[0] if rows else None


def _admitted(user_id):
    return _sql("SELECT admitted_at FROM users WHERE id = %s", (user_id,))[0][0] is not None


@pytest.fixture()
def newcomer(isolated_db):
    return repository.create_user(EMAIL, PASSWORD, "user")


# --- учётка и вход --------------------------------------------------------------------------


def test_new_account_is_created_without_admission(newcomer):
    assert not _admitted(newcomer["id"])


def test_unadmitted_login_with_right_password_asks_for_code_and_opens_no_session(newcomer):
    client = _client()
    response = _login(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["admission_required"] is True
    assert "получите его у них" in body["detail"]
    assert "user" not in body
    assert config.AUTH_COOKIE_NAME not in response.cookies
    assert _sql("SELECT count(*) FROM user_sessions")[0][0] == 0


def test_wrong_password_is_plain_refusal_and_issues_no_code(newcomer, notify_on):
    response = _login(_client(), password="not-the-password")

    assert response.status_code == 401
    assert _code_row(newcomer["id"]) is None
    assert _notify_jobs() == []


def test_admitted_account_logs_in_as_before(newcomer):
    admission.admit(newcomer["id"])
    client = _client()

    response = _login(client)

    assert response.status_code == 200
    assert response.json()["user"]["email"] == EMAIL
    assert client.get("/api/auth/me").status_code == 200


def test_without_notify_flag_login_issues_no_code(newcomer):
    _login(_client())

    assert _code_row(newcomer["id"]) is None
    assert _notify_jobs() == []


def test_self_registration_stays_closed(isolated_db):
    response = _client().post("/api/auth/register", json={"email": "x@example.test", "password": PASSWORD})

    assert response.status_code == 403


# --- код и задача на отправку ---------------------------------------------------------------


def test_login_puts_notify_job_with_account_number_email_start_code_and_expiry(newcomer, notify_on):
    _login(_client())

    jobs = _notify_jobs()
    assert len(jobs) == 1
    _, queue, region, payload, status = jobs[0]
    assert (queue, region, status) == ("external-notify", "external", "queued")
    text = payload["text"]
    assert f"№{newcomer['id']}" in text
    assert "new…" in text
    assert EMAIL not in text and "example.test" not in text
    assert re.search(r"\b\d{8}\b", text)
    assert "действует до" in text
    assert _code_row(newcomer["id"])[2] == jobs[0][0]


def test_code_is_stored_only_as_fingerprint(newcomer, notify_on):
    _login(_client())
    code = _code_from(_notify_jobs()[0])

    stored = _sql("SELECT row_to_json(c)::text FROM admission_codes c")[0][0]
    assert code not in stored


def test_repeat_login_within_ten_minutes_issues_no_new_code(newcomer, notify_on):
    client = _client()
    _login(client)
    _login(client)

    assert len(_notify_jobs()) == 1


def test_active_code_is_not_replaced_even_after_ten_minutes(newcomer, notify_on):
    client = _client()
    _login(client)
    _sql("UPDATE admission_codes SET created_at = now() - interval '11 minutes'")
    _login(client)

    assert len(_notify_jobs()) == 1


def test_dead_code_older_than_ten_minutes_is_replaced_by_new_one(newcomer, notify_on):
    client = _client()
    _login(client)
    first = _code_from(_notify_jobs()[0])
    _sql("UPDATE admission_codes SET created_at = now() - interval '25 hours', expires_at = now() - interval '1 hour'")

    _login(client)

    jobs = _notify_jobs()
    assert len(jobs) == 2
    second = _code_from(jobs[1])
    assert _admit(client, first).status_code == 400 or first == second
    assert _admit(client, second).status_code == 200


def test_exhausted_code_is_replaced_only_after_ten_minutes(newcomer, notify_on):
    client = _client()
    _login(client)
    _sql("UPDATE admission_codes SET attempts = 5")
    _login(client)
    assert len(_notify_jobs()) == 1

    _sql("UPDATE admission_codes SET created_at = now() - interval '11 minutes'")
    _login(client)
    assert len(_notify_jobs()) == 2


# --- ввод кода ------------------------------------------------------------------------------


def test_right_code_admits_and_opens_session(newcomer, notify_on):
    client = _client()
    _login(client)
    code = _code_from(_notify_jobs()[0])

    response = _admit(client, code)

    assert response.status_code == 200, response.text
    assert response.json()["user"]["email"] == EMAIL
    assert client.get("/api/auth/me").status_code == 200
    assert _admitted(newcomer["id"])
    # Дальше — обычный вход, без кода.
    assert _login(_client()).json()["user"]["id"] == newcomer["id"]


def test_code_accepts_spaces_and_dashes(newcomer, notify_on):
    client = _client()
    _login(client)
    code = _code_from(_notify_jobs()[0])

    assert _admit(client, f" {code[:4]}-{code[4:]} ").status_code == 200


def test_used_code_is_refused(newcomer, notify_on):
    client = _client()
    _login(client)
    code = _code_from(_notify_jobs()[0])
    assert _admit(client, code).status_code == 200

    response = _admit(_client(), code)

    assert response.status_code == 400
    assert "использован" in response.json()["detail"]


def test_wrong_code_counts_attempt_and_fifth_kills_the_code(newcomer, notify_on):
    client = _client()
    _login(client)
    code = _code_from(_notify_jobs()[0])
    wrong = "00000000" if code != "00000000" else "11111111"

    first = _admit(client, wrong)
    assert first.status_code == 400
    assert "Осталось попыток: 4" in first.json()["detail"]
    for _ in range(4):
        _admit(client, wrong)

    assert _code_row(newcomer["id"])[0] == 5
    response = _admit(client, code)
    assert response.status_code == 400
    assert "попытки" in response.json()["detail"]
    assert not _admitted(newcomer["id"])


def test_expired_code_is_refused(newcomer, notify_on):
    client = _client()
    _login(client)
    code = _code_from(_notify_jobs()[0])
    _sql("UPDATE admission_codes SET expires_at = now() - interval '1 second'")

    response = _admit(client, code)

    assert response.status_code == 400
    assert "Срок кода истёк" in response.json()["detail"]
    assert not _admitted(newcomer["id"])


def test_code_with_wrong_password_is_plain_refusal_and_costs_no_attempt(newcomer, notify_on):
    client = _client()
    _login(client)
    code = _code_from(_notify_jobs()[0])

    response = _admit(client, code, password="not-the-password")

    assert response.status_code == 401
    assert _code_row(newcomer["id"])[0] == 0
    assert not _admitted(newcomer["id"])


def test_no_code_issued_is_refused(newcomer):
    response = _admit(_client(), "12345678")

    assert response.status_code == 400
    assert config.AUTH_COOKIE_NAME not in response.cookies


# --- смена пароля ---------------------------------------------------------------------------


def _as(user):
    api.app.dependency_overrides[api.require_user] = lambda: user
    return _client()


@pytest.fixture()
def admin_and_member(isolated_db):
    admin = repository.create_user("admin@example.test", PASSWORD, "admin")
    member = repository.create_user("member@example.test", PASSWORD, "user")
    admission.admit(admin["id"])
    admission.admit(member["id"])
    yield admin, member
    api.app.dependency_overrides.pop(api.require_user, None)


def test_changing_someone_elses_password_revokes_admission_and_closes_sessions(admin_and_member):
    admin, member = admin_and_member
    repository.create_user_session(member["id"])

    response = _as(admin).patch(f"/api/users/{member['id']}", json={"password": "brand-new-password"})

    assert response.status_code == 200, response.text
    assert not _admitted(member["id"])
    assert _sql("SELECT count(*) FROM user_sessions WHERE user_id = %s", (member["id"],))[0][0] == 0


def test_changing_own_password_keeps_admission_and_sessions(admin_and_member):
    admin, _ = admin_and_member
    repository.create_user_session(admin["id"])

    response = _as(admin).patch(f"/api/users/{admin['id']}", json={"password": "brand-new-password"})

    assert response.status_code == 200
    assert _admitted(admin["id"])
    assert _sql("SELECT count(*) FROM user_sessions WHERE user_id = %s", (admin["id"],))[0][0] == 1


def test_changing_only_role_keeps_admission(admin_and_member):
    admin, member = admin_and_member

    _as(admin).patch(f"/api/users/{member['id']}", json={"role": "admin"})

    assert _admitted(member["id"])


# --- после отправки текст сообщения стирается ------------------------------------------------


def _claim(client_headers):
    return _client().post("/api/external-worker/claim", headers=client_headers,
                          json={"worker_id": "nl-fetch-1#1", "queues": ["external-notify"],
                                "capabilities": ["http_fetch"]}).json()["job"]


@pytest.fixture()
def worker_headers(monkeypatch):
    monkeypatch.setattr(config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("worker-token"))
    return {"Authorization": "Bearer worker-token"}


def test_claimed_notify_job_carries_text_and_complete_erases_it(newcomer, notify_on, worker_headers):
    _login(_client())
    job = _claim(worker_headers)
    assert job["kind"] == "admission_notify"
    assert re.search(r"\b\d{8}\b", job["payload"]["text"])

    done = _client().post(f"/api/external-worker/jobs/{job['id']}/complete", headers=worker_headers,
                          json={"lease_token": job["lease_token"], "result": {"admission_notify": True}})

    assert done.status_code == 200, done.text
    _, _, _, payload, status = _notify_jobs()[0]
    assert status == "ok"
    assert "text" not in payload
    assert _code_row(newcomer["id"])[3] is not None


def test_final_failure_erases_text_and_records_send_error(newcomer, notify_on, worker_headers):
    _login(_client())
    _sql("UPDATE background_jobs SET max_attempts = 1 WHERE kind = 'admission_notify'")
    job = _claim(worker_headers)

    _client().post(f"/api/external-worker/jobs/{job['id']}/fail", headers=worker_headers,
                   json={"lease_token": job["lease_token"], "error": "Telegram: 502", "retryable": True})

    _, _, _, payload, status = _notify_jobs()[0]
    assert status == "failed"
    assert "text" not in payload
    assert "Telegram" in _code_row(newcomer["id"])[4]


def test_retryable_failure_keeps_text_for_next_attempt(newcomer, notify_on, worker_headers):
    _login(_client())
    job = _claim(worker_headers)

    _client().post(f"/api/external-worker/jobs/{job['id']}/fail", headers=worker_headers,
                   json={"lease_token": job["lease_token"], "error": "Telegram: 502", "retryable": True})

    _, _, _, payload, status = _notify_jobs()[0]
    assert status == "queued"
    assert "text" in payload


# --- выкат: нынешние учётки допущены, один раз ------------------------------------------------


def _run_script():
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(f'SET search_path TO "{_schema()}"')
        conn.execute(SCRIPT.read_text())
        conn.commit()


def _schema():
    with connection.get_connection() as conn:
        return conn.execute("SELECT current_schema()").fetchone()[0]


def test_before_deploy_script_admits_existing_accounts_once(isolated_db):
    _sql("ALTER TABLE users DROP COLUMN admitted_at")
    _sql("DROP TABLE admission_codes")
    old = repository.create_user("old@example.test", PASSWORD, "user")

    _run_script()
    assert _admitted(old["id"])

    admission.revoke(old["id"])
    _run_script()
    connection.init_db()
    assert not _admitted(old["id"])


def test_jobs_list_does_not_show_the_code(newcomer, notify_on, admin_and_member):
    admin, _ = admin_and_member
    _login(_client())
    job_id = _notify_jobs()[0][0]

    response = _as(admin).get(f"/api/jobs/{job_id}")

    assert response.status_code == 200
    assert "text" not in response.json()["payload"]
    assert response.json()["payload"]["user_id"] == newcomer["id"]


# --- запасной путь: команда на сервере -------------------------------------------------------


def test_admission_code_command_prints_fresh_code_that_admits(newcomer, notify_on, capsys):
    from oiltech_digest import cli

    _login(_client())  # действующий код уже есть и ушёл в чат — команда всё равно выдаёт новый
    cli.main(["admission-code", "--email", EMAIL.upper()])
    out = capsys.readouterr().out
    code = re.search(r"\b(\d{8})\b", out).group(1)

    assert f"№{newcomer['id']}" in out
    assert _admit(_client(), code).status_code == 200


def test_admission_code_command_by_account_number(newcomer, capsys):
    from oiltech_digest import cli

    cli.main(["admission-code", "--user-id", str(newcomer["id"])])

    code = re.search(r"\b(\d{8})\b", capsys.readouterr().out).group(1)
    assert _admit(_client(), code).status_code == 200


def test_admission_code_command_unknown_account(isolated_db, capsys):
    from oiltech_digest import cli

    with pytest.raises(SystemExit):
        cli.main(["admission-code", "--user-id", "999"])
    assert "не найдена" in capsys.readouterr().out
