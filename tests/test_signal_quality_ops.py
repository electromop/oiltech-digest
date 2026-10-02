"""Качество радара — выкат и эксплуатация: SQL до выката, архив из CLI, режим поиска из CLI,
свои модели и лимиты у всех вызовов радара."""

from __future__ import annotations

from pathlib import Path

import pytest

from oiltech_digest import cli, config, signal_dedup, signal_discovery
from oiltech_digest.db import connection, repository
from oiltech_digest.processing.openai_client import AIResponse

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "radar" / "radar-quality-before-deploy.sql"
NEW_COLUMNS = {
    "signal_category", "event_date", "mixed_events", "mixed_events_reason", "score_profile",
    "score_items_json", "criteria_snapshot", "archived_at", "archive_reason",
    "oilfield_relevance", "oilfield_application", "filter_stage", "filter_reason",
}


def _signal_columns() -> set[str]:
    with connection.get_connection() as conn:
        return {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'signals'"
        )}


def _run_script() -> None:
    with connection.get_connection() as conn:
        conn.commit()
        conn.autocommit = True
        conn.execute(SCRIPT.read_text(encoding="utf-8"))


def test_before_deploy_sql_adds_columns_to_old_schema_and_repeats_safely(isolated_db):
    with connection.get_connection() as conn:
        for column in NEW_COLUMNS:
            conn.execute(f"ALTER TABLE signals DROP COLUMN IF EXISTS {column}")
        conn.execute("INSERT INTO signals (signal_key, title, theme) VALUES ('old', 'Старая карточка', 'Бурение')")
        conn.commit()

    _run_script()
    _run_script()  # повтор безопасен

    assert NEW_COLUMNS <= _signal_columns()
    with connection.get_connection() as conn:
        # Массовых UPDATE нет: у прежней карточки поля пустые — видимость читает её «до правки».
        assert conn.execute("SELECT signal_category, event_date, archived_at FROM signals").fetchall() == [(None, None, None)]


def test_schema_sql_has_the_same_columns_as_the_deploy_script(isolated_db):
    assert NEW_COLUMNS <= _signal_columns()


def _card(key, theme, first_seen):
    signal_id = repository.upsert_signal({"signal_key": key, "title": key, "theme": theme, "score": 30})
    repository.upsert_signal_evidence(signal_id, {"source_url": f"https://example.com/{key}", "title": key})
    with connection.get_connection() as conn:
        conn.execute("UPDATE signals SET first_seen_at = %s WHERE id = %s", (first_seen, signal_id))
        conn.commit()
    return signal_id


def test_archive_cli_is_a_dry_run_by_default_and_reversible(isolated_db, capsys):
    with connection.get_connection() as conn:
        conn.execute("INSERT INTO tags (name, enabled) VALUES ('Бурение', TRUE)")
        conn.commit()
    early = _card("early", "HSE/цифровые разрешения", "2026-09-13")
    _card("topical", "Бурение", "2026-09-13")

    cli.main(["archive-signals", "--created-before", "2026-09-14"])
    out = capsys.readouterr().out
    assert "карточек 1, из них разобранных 0" in out and "dry_run=True" in out and f"#{early}" in out
    assert repository.archive_signal_candidates(created_before="2026-09-14")  # ничего не записано

    cli.main(["archive-signals", "--created-before", "2026-09-14", "--apply"])
    assert "В архиве: 1" in capsys.readouterr().out
    assert repository.archive_signal_candidates(created_before="2026-09-14") == []

    cli.main(["unarchive-signals", "--reason", "early-free-theme-2026-09"])
    assert "возвращено карточек 1" in capsys.readouterr().out


def test_search_mode_flag_reaches_the_queued_job(monkeypatch):
    queued = {}
    monkeypatch.setattr(repository, "create_background_job",
                        lambda kind, payload, **kwargs: queued.update(payload) or {"id": 7, "queue_name": "external-agents"})

    cli.main(["enqueue-signal-discovery", "--topic", "Бурение", "--no-offline", "--dry-run", "--search-mode", "openai_web"])

    assert queued["search_mode"] == "openai_web"
    assert signal_discovery.config_from_payload(queued).search_mode == "openai_web"


def test_enqueue_without_flag_leaves_the_choice_to_core_settings(monkeypatch):
    queued = {}
    monkeypatch.setattr(repository, "create_background_job",
                        lambda kind, payload, **kwargs: queued.update(payload) or {"id": 8, "queue_name": "external-agents"})
    monkeypatch.setattr(config, "SIGNAL_SEARCH_MODE", "openai_web")

    cli.main(["enqueue-signal-discovery", "--topic", "Бурение", "--no-offline", "--dry-run"])

    assert "search_mode" not in queued
    assert signal_discovery.config_from_payload(queued).search_mode == "openai_web"


def test_discover_signals_cli_passes_search_mode(monkeypatch):
    seen = {}
    monkeypatch.setattr(signal_discovery, "discover_signals", lambda config_: seen.update(mode=config_.search_mode) or {
        "dry_run": True, "offline": True, "web_search": False, "web_only": False, "days": 14, "topics": [],
        "signals": [], "all_signals": 0, "topic_results": [], "generation_run_id": None, "dedup": None,
    })

    cli.main(["discover-signals", "--search-mode", "both", "--json"])

    assert seen["mode"] == "both"


# --- Свои модели и лимиты у каждого вызова радара ---------------------------------------------


class _Recorder:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
        self.calls.append({"model": model, "effort": reasoning_effort, "budget": max_output_tokens})
        return AIResponse(data=self.data, model=model or "m")


def test_batch_review_uses_review_model_and_grows_budget_with_batch(monkeypatch):
    recorder = _Recorder({"decisions": []})
    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: recorder)
    monkeypatch.setattr(config, "SIGNAL_REVIEW_MODEL", "gpt-5")
    monkeypatch.setattr(config, "SIGNAL_REVIEW_REASONING", "medium")
    candidates = [{"signal": {"signal_key": f"k{i}", "title": f"t{i}", "score": 50}, "rejected": False} for i in range(4)]

    signal_discovery._batch_review_candidates(candidates, "Бурение", offline=False)

    # 900 + 250 на кандидата + запас на medium.
    assert recorder.calls == [{"model": "gpt-5", "effort": "medium", "budget": 900 + 250 * 4 + 6000}]


def test_dedup_judge_uses_dedup_model(monkeypatch):
    recorder = _Recorder({"same_event": False, "reason": "разные"})
    monkeypatch.setattr(config, "SIGNAL_DEDUP_MODEL", "gpt-5-mini")
    monkeypatch.setattr(config, "SIGNAL_DEDUP_REASONING", "low")
    nodes = [
        {"kind": "new", "signal": {"title_ru": "SLB купила стартап бурения", "companies": ["SLB"]}, "urls": ["https://a/1"],
         "fresh": True, "reviewed": False},
        {"kind": "new", "signal": {"title_ru": "SLB стартап бурения купила", "companies": ["SLB"]}, "urls": ["https://b/2"],
         "fresh": True, "reviewed": False},
    ]

    signal_dedup.dedupe(nodes, client_factory=lambda: recorder)

    assert recorder.calls and recorder.calls[0]["model"] == "gpt-5-mini"
    assert recorder.calls[0]["budget"] == 700 + 2000


def test_search_queries_budget_follows_reasoning(monkeypatch):
    from oiltech_digest.source_discovery import agent

    recorder = _Recorder({"queries": ["q1"]})
    monkeypatch.setattr(agent, "make_client", lambda offline: recorder)
    monkeypatch.setattr(agent.repository, "list_agent_memory", lambda **kwargs: [])
    monkeypatch.setattr(config, "OPENAI_REASONING_EFFORT", "medium")

    agent.generate_search_queries("бурение", offline=False, limit=3)

    assert recorder.calls[0]["budget"] == 1200 + 6000


def test_excluded_topics_setting_parses_several_prefixes(monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_RADAR_EXCLUDED_TOPICS", ("Рынок", "Логистика"))
    monkeypatch.setattr(signal_discovery, "_radar_topics",
                        lambda: [{"name": "Бурение"}, {"name": "Рынок, M&A"}, {"name": "Логистика, транспорт"}])

    assert [row["name"] for row in signal_discovery._selected_topics(None)] == ["Бурение"]


# --- Расход радара — на экран «Статистика» ---------------------------------------------------


def test_run_collects_ai_usage_by_stage(monkeypatch):
    from oiltech_digest import signal_research
    from tests.test_signal_research import _Research, _event

    judge_answer = {"title": "t", "title_ru": "t", "theme": "Бурение", "summary": "s", "thesis": "", "transferability": "",
                    "maturity": "watch", "confidence": 0.5, "score": 60, "why_now": "", "why_not_noise": "",
                    "companies": [], "industries": [], "signal_category": "technology", "event_date": "2026-09-24",
                    "mixed_events": False, "mixed_events_reason": ""}

    class Judge:
        def complete_json(self, *args, **kwargs):
            return AIResponse(data=dict(judge_answer), model="gpt-5-judge", input_tokens=2500, output_tokens=900)

    research = _Research([_event()])
    monkeypatch.setattr(signal_research, "make_client", lambda offline: research)
    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: Judge())
    monkeypatch.setattr(signal_discovery, "_fetch_full_text",
                        lambda url, fallback_title="", **kwargs: {"ok": False, "error": "403", "title": "", "raw_text": ""})
    run_config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=True, web_only=True, search_mode="openai_web")

    run = signal_discovery.run_discovery(run_config, {"topics": [{"name": "Бурение"}], "tags": []})

    usage = {row["stage"]: row for row in run["ai_usage"]}
    assert usage["radar_research"] == {"stage": "radar_research", "model": "gpt-5-test", "calls": 1,
                                       "input_tokens": 17000, "output_tokens": 1300, "web_search_calls": 7}
    assert usage["radar_judge"]["calls"] == 1 and usage["radar_judge"]["input_tokens"] == 2500


def test_radar_usage_is_stored_with_cost_and_not_doubled_on_reapply(isolated_db):
    usage = [
        {"stage": "radar_research", "model": "gpt-5-2025-08-07", "calls": 1, "input_tokens": 1_000_000,
         "output_tokens": 100_000, "web_search_calls": 20},
        {"stage": "radar_judge", "model": "gpt-5-2025-08-07", "calls": 8, "input_tokens": 20_000, "output_tokens": 0,
         "web_search_calls": 0},
        {"stage": "radar_dedup", "model": "gpt-5-mini", "calls": 0, "input_tokens": 0, "output_tokens": 0,
         "web_search_calls": 0},
    ]

    assert repository.record_radar_ai_usage(77, usage) == 2  # стадия без вызовов не пишется
    repository.record_radar_ai_usage(77, usage)  # повторное применение итога той же задачи

    with connection.get_connection() as conn:
        rows = conn.execute(
            "SELECT stage, cost_usd, error_message FROM ai_processing_runs WHERE job_id = 77 ORDER BY stage"
        ).fetchall()
    # 1 млн входных × $1,25 + 100 тыс. выходных × $10 + 20 поисков × $0,01 = 1,25 + 1 + 0,2.
    assert [(stage, float(cost)) for stage, cost, _ in rows] == [("radar_judge", 0.025), ("radar_research", 2.45)]
    assert rows[1][2] == "calls=1 web_search=20"


def test_apply_writes_radar_usage_only_for_a_real_run(isolated_db, monkeypatch):
    written = []
    monkeypatch.setattr(repository, "record_radar_ai_usage", lambda job_id, usage: written.append((job_id, usage)))
    usage = [{"stage": "radar_judge", "model": "gpt-5", "calls": 1, "input_tokens": 1, "output_tokens": 1,
              "web_search_calls": 0}]

    signal_discovery.apply_discovery(signal_discovery.SignalDiscoveryConfig(dry_run=True), {"topics": [], "ai_usage": usage})
    signal_discovery.apply_discovery(signal_discovery.SignalDiscoveryConfig(dry_run=False, background_job_id=5),
                                     {"topics": [], "ai_usage": usage})

    assert written == [(5, usage)]


def test_statistics_split_radar_from_the_feed(isolated_db):
    from oiltech_digest.db import analytics

    with connection.get_connection() as conn:
        conn.execute("""INSERT INTO ai_processing_runs (stage, model, cost_usd) VALUES
                        ('scoring', 'gpt-5-mini', 1.5), ('radar_judge', 'gpt-5', 2.0), ('radar_research', 'gpt-5', 0.5)""")
        conn.commit()

    month = analytics.monthly_analytics(1, include_cost=True)["ai_cost"][-1]
    assert (float(month["cost_usd"]), float(month["radar_cost_usd"])) == (4.0, 2.5)
    areas = {(row["model"], row["area"]): float(row["cost_usd"]) for row in repository.monthly_ai_cost(1)}
    assert areas == {("gpt-5-mini", "feed"): 1.5, ("gpt-5", "radar"): 2.5}
