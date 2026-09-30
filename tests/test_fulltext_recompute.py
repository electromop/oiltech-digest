"""Пересчёт сути, тега и балла после того, как обрывок статьи заменён полным текстом.

С #79 пакет планировщика зовёт у статьи только недостающие стадии (external_ai._stages_left):
суть, тег и балл, посчитанные по анонсу ленты, остались бы навсегда. Полное тело приходит позже —
зарубежным воркером (refetch_text) или локальной дозагрузкой (fetch-full-text), — и тогда статья
идёт в пересчёт один раз: только суть, тег и балл, только при реальном приросте текста, только
в окне ленты.
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from oiltech_digest import api, feed_window, scoring_profiles
from oiltech_digest.db import connection, repository
from oiltech_digest.ingestion import article_fetcher, external_fetch
from oiltech_digest.processing import external_ai, fulltext_recompute
from oiltech_digest.processing.openai_client import AIResponse
from oiltech_digest.processing.seed import seed_default_scoring_criteria

AUTH = {"Authorization": "Bearer secret"}
STAGES = ["summary", "tagging", "scoring"]
TITLE = "Aker BP starts drilling campaign at Yggdrasil field in the North Sea"
# Анонс ленты: 183 знака, как средний обрывок Oil & Gas Journal (замер 17.09).
TEASER = ("Aker BP has started a drilling campaign at the Yggdrasil field. The operator plans "
          "several wells this year, the company said in a statement on Monday, without details.")
FULL = (
    "Aker BP starts drilling campaign at Yggdrasil field in the North Sea. "
    + "The Yggdrasil drilling campaign covers production wells, subsea tie-backs and a new "
      "unmanned platform; the operator expects first oil in 2027 and plateau output of 65 000 "
      "barrels per day. " * 12
    + "ПОЛНЫЙ-ТЕКСТ-МАРКЕР: contract awarded to Halliburton for well construction services."
)


def _freeze_window(monkeypatch) -> None:
    # 29.09 по МСК: открыт только сентябрь, август — архив.
    monkeypatch.setattr(feed_window, "_now", lambda: datetime(2026, 9, 29, 12, 0, tzinfo=feed_window.MSK))


def _external_ai(monkeypatch, *, external: bool = True) -> None:
    monkeypatch.setattr("oiltech_digest.config.EXTERNAL_WORKERS_ENABLED", external)
    monkeypatch.setattr("oiltech_digest.config.AI_EXECUTION_REGION", "external" if external else "ru")
    monkeypatch.setattr("oiltech_digest.config.AI_BULK_LANE_ENABLED", True)


def _source(*, region: str = "external") -> int:
    with connection.get_connection() as conn:
        source_id = conn.execute(
            "INSERT INTO sources (name, source_type, url, enabled, parse_strategy, network_region) "
            "VALUES (%s, 'News', %s, TRUE, 'rss', %s) RETURNING id",
            (f"Лента {region}", f"https://{region}.example", region),
        ).fetchone()[0]
        conn.commit()
    return int(source_id)


def _tag_id() -> int:
    with connection.get_connection() as conn:
        row = conn.execute("SELECT id FROM tags WHERE name = 'Бурение'").fetchone()
        if row is None:
            row = conn.execute("INSERT INTO tags (name, enabled, sort_order) VALUES ('Бурение', TRUE, 1) RETURNING id").fetchone()
            conn.commit()
    return int(row[0])


def _article(source_id: int, n: int, *, text: str = TEASER, published: str = "2026-09-20 12:00+00",
             title: str = TITLE) -> int:
    with connection.get_connection() as conn:
        article_id = conn.execute(
            "INSERT INTO articles (source_id, title, url, published_at, collected_at, raw_text, text_truncated, "
            "language) VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'en') RETURNING id",
            (source_id, f"{title} {n}" if n else title, f"https://news.example/{source_id}/{n}", published, published, text),
        ).fetchone()[0]
        conn.commit()
    return int(article_id)


def _scored_on_teaser(article_id: int, *, relevant: bool = True) -> None:
    """Как пакет планировщика до дозагрузки: вердикт гейта, суть, перевод, тег и балл по анонсу."""
    repository.set_article_relevance(article_id, relevant, "по анонсу", "gate")
    if not relevant:
        return
    repository.upsert_article_card(article_id, "Суть по анонсу", "old-model")
    repository.set_article_title_ru(article_id, "Aker BP начала бурение на Yggdrasil")
    repository.upsert_article_tag(article_id, _tag_id(), 0.5, "по анонсу", "old-model")
    criteria = repository.list_enabled_scoring_criteria()
    items = [{"criterion_id": int(c["id"]), "ai_score": 84, "keyword_score": 0, "final_score": 84} for c in criteria]
    repository.replace_article_score(article_id, 84.0, "Высокая", "по анонсу", items, "old-model",
                                     profile="business", criteria_snapshot=scoring_profiles.criteria_snapshot(criteria))


def _refetch(article_id: int, text: str = FULL, status: str = "ok") -> dict:
    return external_fetch.apply_refetch_text_result(
        {"kind": "refetch_text", "results": [{"id": article_id, "status": status, "text": text}]}
    )


def _jobs() -> list[tuple]:
    with connection.get_connection() as conn:
        return conn.execute(
            "SELECT id, queue_name, status, payload_json FROM background_jobs "
            "WHERE kind = 'process_articles' ORDER BY id"
        ).fetchall()


def _recompute_jobs() -> list[tuple]:
    return [job for job in _jobs() if (job[3] or {}).get("after_full_text")]


class _Model:
    """Фейк модели NL: пишет, какие стадии звали и с каким входом."""

    model = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, instructions, user_input, schema, **kwargs):
        name = schema["name"]
        self.calls.append((name, user_input))
        if name == "article_relevance":
            return AIResponse(data={"relevant": True, "reason": "ok"}, model="fake")
        if name == "article_summary":
            return AIResponse(data={"summary": "Суть по полному тексту"}, model="fake")
        if name == "article_tag":
            return AIResponse(data={"tag_id": _TAG["id"], "confidence": 0.9, "rationale": "r"}, model="fake")
        if name == "article_score":
            return AIResponse(data={"incident_without_solution": False, "explanation": "по полному тексту",
                                    "items": []}, model="fake")
        return AIResponse(data={"title_ru": "перевод"}, model="fake")


_TAG: dict = {}


def _claim(core: TestClient) -> dict | None:
    # Повтор до 2 с: часы ВМ colima подводятся назад, и задача на миг «из будущего».
    deadline = time.monotonic() + 2.0
    while True:
        job = core.post("/api/external-worker/claim", headers=AUTH, json={
            "worker_id": "nl-ai-bulk-1", "queues": ["external-ai-bulk"], "capabilities": ["openai"],
        }).json()["job"]
        if job is not None or time.monotonic() > deadline:
            return job
        time.sleep(0.05)


def _core(monkeypatch) -> TestClient:
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    return TestClient(api.app)


# --- Прирост текста ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "gain"),
    [
        pytest.param(TEASER, FULL, True, id="анонс-183-в-полный-текст"),
        pytest.param("", "т" * 600, True, id="пустое-тело"),
        pytest.param("т" * 450, "т" * 850, False, id="меньше-чем-вдвое"),
        pytest.param(FULL, FULL, False, id="то-же-тело"),
        pytest.param(FULL, FULL.replace(". ", ".\n\n"), False, id="то-же-тело-другие-пробелы"),
        # Модель читает первые 6000 знаков: 4000 → 20 000 для неё — 4000 → 6000.
        pytest.param("т" * 4000, "т" * 20_000, False, id="дальше-6000-модель-не-читает"),
        pytest.param("т" * 2500, "т" * 20_000, True, id="2500-в-6000-видимых"),
        pytest.param(TEASER, "", False, id="нового-тела-нет"),
    ],
)
def test_substantial_gain_is_measured_on_what_the_model_reads(old, new, gain):
    assert fulltext_recompute.substantial_gain(old, new) is gain


# --- Внешний путь: обрывок → суть и балл → полное тело → пересчёт ----------------------------


def test_teaser_scored_then_full_body_goes_to_next_batch_with_exactly_summary_tag_score(isolated_db, monkeypatch):
    _freeze_window(monkeypatch)
    _external_ai(monkeypatch)
    seed_default_scoring_criteria()   # оба профиля: балл статьи — business
    _TAG["id"] = _tag_id()
    article_id = _article(_source(), 0)
    _scored_on_teaser(article_id)

    applied = _refetch(article_id)

    assert applied["applied"] == 1 and applied["recompute"] == 1
    ((job_id, queue, status, payload),) = _jobs()
    assert (queue, status) == ("external-ai-bulk", "queued")
    assert payload == {"article_ids": [article_id], "limit": 1, "offline": False, "only": STAGES,
                       "after_full_text": True}

    core = _core(monkeypatch)
    job = _claim(core)
    assert job is not None and job["id"] == job_id
    (sent,) = job["payload"]["articles"]
    assert sent["raw_text"] == FULL and sent["summary"] is None   # старая суть в промпт не идёт
    business = repository.list_enabled_scoring_criteria()
    assert [c["id"] for c in job["payload"]["criteria"]] == [c["id"] for c in business]

    model = _Model()
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: model)
    result = external_ai.process_payload(job["payload"])

    # Ровно суть, тег и балл: ни гейта, ни перевода заголовка.
    assert [name for name, _ in model.calls] == ["article_summary", "article_tag", "article_score"]
    summary_input = model.calls[0][1]
    assert "ПОЛНЫЙ-ТЕКСТ-МАРКЕР" in summary_input and "Суть по анонсу" not in summary_input

    done = core.post(f"/api/external-worker/jobs/{job_id}/complete", headers=AUTH,
                     json={"lease_token": job["lease_token"], "result": result})
    assert done.status_code == 200
    with connection.get_connection() as conn:
        card = conn.execute("SELECT summary, title_ru, relevant FROM article_cards WHERE article_id = %s",
                            (article_id,)).fetchone()
        score = conn.execute("SELECT model, profile, explanation FROM article_scores WHERE article_id = %s",
                             (article_id,)).fetchone()
        tag = conn.execute("SELECT model, rationale FROM article_tags WHERE article_id = %s", (article_id,)).fetchone()
    assert card == ("Суть по полному тексту", "Aker BP начала бурение на Yggdrasil", True)
    assert score == ("fake", "business", "по полному тексту")
    assert tag == ("fake", "r")

    # Повторная дозагрузка того же тела (ретрай задачи refetch_text) в пересчёт не ставит.
    assert _refetch(article_id)["recompute"] == 0
    assert len(_jobs()) == 1


def test_body_without_gain_is_written_but_not_recomputed(isolated_db, monkeypatch):
    _freeze_window(monkeypatch)
    _external_ai(monkeypatch)
    seed_default_scoring_criteria()
    source_id = _source()
    short = _article(source_id, 1, text="Aker BP Yggdrasil drilling " * 16)   # 432 знака
    _scored_on_teaser(short)

    applied = _refetch(short, "Aker BP Yggdrasil drilling campaign North Sea " * 14)   # 630: меньше вдвое

    assert applied == {"applied": 1, "skipped": 0, "mismatched": 0, "recompute": 0}
    assert _jobs() == []


def test_archive_rejected_hidden_and_unprocessed_articles_are_not_recomputed(isolated_db, monkeypatch):
    """Архив — только просмотр: статья закрытого месяца, выбранная людьми в дайджест, не меняется.
    Отвергнутую гейтом и скрытую не пересчитываем, а ещё не обработанную возьмёт обычный пакет —
    уже по полному тексту."""
    _freeze_window(monkeypatch)
    _external_ai(monkeypatch)
    seed_default_scoring_criteria()
    source_id = _source()
    august = _article(source_id, 1, published="2026-08-28 12:00+00")
    rejected = _article(source_id, 2)
    hidden = _article(source_id, 3)
    fresh = _article(source_id, 4)
    for article_id in (august, hidden):
        _scored_on_teaser(article_id)
    _scored_on_teaser(rejected, relevant=False)
    with connection.get_connection() as conn:
        user_id = conn.execute("INSERT INTO users (email, password_salt, password_hash) "
                               "VALUES ('d@example.com', 's', 'h') RETURNING id").fetchone()[0]
        conn.execute("INSERT INTO user_article_states (user_id, article_id, status) VALUES (%s, %s, 'digest')",
                     (user_id, august))
        conn.execute("UPDATE articles SET pending_deletion = TRUE WHERE id = %s", (hidden,))
        conn.commit()

    for n, article_id in enumerate((august, rejected, hidden, fresh), start=1):
        assert _refetch(article_id, FULL.replace("ПОЛНЫЙ", f"ПОЛНЫЙ-{n}"))["applied"] == 1

    assert _jobs() == []


def test_article_in_flight_on_teaser_is_recomputed_after_that_batch_unless_gate_rejects(isolated_db, monkeypatch):
    """Главный случай зарубежных источников: пакет дня выдан с анонсом (резерв при выдаче), а
    refetch_text той же минуты кладёт полное тело. Суть ляжет по анонсу уже после замены тела —
    пересчёт ставится сразу и ждёт, пока пакет не допишет. Кого пакет отверг гейтом, тот
    пересчёта не получает: модель за него не платится."""
    _freeze_window(monkeypatch)
    _external_ai(monkeypatch)
    seed_default_scoring_criteria()
    _TAG["id"] = _tag_id()
    source_id = _source()
    kept, rejected = _article(source_id, 1), _article(source_id, 2)
    batch = repository.create_background_job("process_articles", {"limit": 5}, queue_name="external-ai",
                                             execution_region="external", capability="openai")
    with connection.get_connection() as conn:   # выдан воркеру NL: аренда живая
        conn.execute("UPDATE background_jobs SET status = 'running', lease_expires_at = now() + interval '10 minutes' "
                     "WHERE id = %s", (batch["id"],))
        conn.commit()
    assert sorted(repository.reserve_process_articles(int(batch["id"]), limit=5)) == sorted([kept, rejected])

    applied = external_fetch.apply_refetch_text_result({"kind": "refetch_text", "results": [
        {"id": kept, "status": "ok", "text": FULL.replace("ПОЛНЫЙ", "ПОЛНЫЙ-1")},
        {"id": rejected, "status": "ok", "text": FULL.replace("ПОЛНЫЙ", "ПОЛНЫЙ-2")},
    ]})
    assert applied["recompute"] == 2
    ((recompute_id, _, _, payload),) = _recompute_jobs()
    assert payload["article_ids"] == sorted([kept, rejected])

    core = _core(monkeypatch)
    assert _claim(core) is None   # пакет дня ещё пишет итог по анонсу: пересчёт ждёт его
    assert repository.get_background_job(recompute_id)["status"] == "queued"

    _scored_on_teaser(kept)
    _scored_on_teaser(rejected, relevant=False)
    with connection.get_connection() as conn:
        conn.execute("UPDATE background_jobs SET status = 'ok' WHERE id = %s", (batch["id"],))
        conn.execute("UPDATE background_jobs SET run_after = now() - interval '1 second' WHERE id = %s", (recompute_id,))
        conn.commit()

    job = _claim(core)
    assert job is not None and job["id"] == recompute_id
    assert [a["id"] for a in job["payload"]["articles"]] == [kept]
    assert job["payload"]["articles"][0]["summary"] is None


def test_digest_article_of_month_closed_while_recompute_waited_is_left_as_is(isolated_db, monkeypatch):
    """04.10 сентябрь ещё открыт (зазор до 5-го), и пересчёт ставится. Если задача дождалась
    закрытия месяца — статья, выбранная людьми в сентябрьский дайджест, не меняется: архив —
    только просмотр, и модель за неё не зовётся."""
    monkeypatch.setattr(feed_window, "_now", lambda: datetime(2026, 10, 4, 12, 0, tzinfo=feed_window.MSK))
    _external_ai(monkeypatch)
    seed_default_scoring_criteria()
    article_id = _article(_source(), 0)
    _scored_on_teaser(article_id)
    with connection.get_connection() as conn:
        user_id = conn.execute("INSERT INTO users (email, password_salt, password_hash) "
                               "VALUES ('d@example.com', 's', 'h') RETURNING id").fetchone()[0]
        conn.execute("INSERT INTO user_article_states (user_id, article_id, status) VALUES (%s, %s, 'digest')",
                     (user_id, article_id))
        conn.commit()

    assert _refetch(article_id)["recompute"] == 1
    monkeypatch.setattr(feed_window, "_now", lambda: datetime(2026, 10, 5, 12, 0, tzinfo=feed_window.MSK))

    job = _claim(_core(monkeypatch))
    assert job is not None and job["payload"]["articles"] == []


# --- Локальная дозагрузка ---------------------------------------------------------------------


def test_local_fetch_full_text_step_enqueues_recompute_once(isolated_db, monkeypatch, capsys):
    """Шаг планировщика fetch-full-text (источники РФ): обрывок, посчитанный пакетом раньше
    дозагрузки (очередь дозагрузки длиннее FULL_TEXT_LIMIT или повтор --retry-too-short)."""
    from oiltech_digest import cli

    _freeze_window(monkeypatch)
    _external_ai(monkeypatch)
    seed_default_scoring_criteria()
    article_id = _article(_source(region="auto"), 0)
    _scored_on_teaser(article_id)
    monkeypatch.setattr(article_fetcher, "fetch_article_text",
                        lambda article, min_chars=800: article_fetcher.ExtractionResult(FULL, "ok"))
    step = cli.build_parser().parse_args(["fetch-full-text", "--limit", "10"])

    cli.cmd_fetch_full_text(step)

    assert "обновлено=1" in (out := capsys.readouterr().out) and "пересчёт ИИ=1" in out
    ((_, queue, _, payload),) = _jobs()
    assert queue == "external-ai-bulk" and payload["article_ids"] == [article_id] and payload["only"] == STAGES
    # Статья с полным текстом в дозагрузку больше не выбирается — второго пересчёта нет.
    cli.cmd_fetch_full_text(step)
    assert "проверено=0" in (out := capsys.readouterr().out) and "пересчёт ИИ=0" in out
    assert len(_jobs()) == 1


def test_recompute_is_not_queued_twice_and_not_without_external_ai(isolated_db, monkeypatch):
    _freeze_window(monkeypatch)
    _external_ai(monkeypatch)
    seed_default_scoring_criteria()
    article_id = _article(_source(), 0)
    _scored_on_teaser(article_id)

    first = fulltext_recompute.enqueue_after_body([article_id])
    second = fulltext_recompute.enqueue_after_body([article_id])

    assert first["articles"] == [article_id] and len(first["jobs"]) == 1
    assert second == {"articles": [], "jobs": [], "skipped": None}
    assert len(_jobs()) == 1

    # Местный конвейер пометки only не знает: у посчитанной статьи он ничего не пересчитал бы.
    _external_ai(monkeypatch, external=False)
    local = fulltext_recompute.enqueue_after_body([_article(_source(region="auto"), 5)])
    assert local["jobs"] == [] and local["skipped"]
    assert len(_jobs()) == 1
