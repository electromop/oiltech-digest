"""Качество радара и эксплуатация: архив из CLI, прогон из CLI с настройками поиска ядра, своя модель «Дедупа»,
расход радара на экране «Статистика»."""

from __future__ import annotations


from oiltech_digest import cli, config, signal_dedup, signal_discovery
from oiltech_digest.db import connection, repository
from oiltech_digest.processing.openai_client import AIResponse


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


def test_cli_queues_a_radar_run_with_the_core_search_settings(monkeypatch):
    queued = {}
    monkeypatch.setattr(repository, "create_background_job",
                        lambda kind, payload, **kwargs: queued.update(payload) or {"id": 7, "queue_name": "external-agents"})
    monkeypatch.setattr(config, "SIGNAL_RESEARCH_REASONING", "medium")

    cli.main(["enqueue-signal-discovery", "--topic", "Бурение", "--days", "3", "--no-offline", "--dry-run"])

    # Модель, уровень, потолок и период поиска решает ядро в момент постановки (#138), NL своих не читает.
    assert queued == {"topic": "Бурение", "days": 3, "offline": False, "dry_run": True,
                      "research_model": config.SIGNAL_RESEARCH_MODEL, "research_reasoning": "medium",
                      "research_limit": config.SIGNAL_RESEARCH_EVENTS_PER_TOPIC,
                      "research_days": max(3, config.SIGNAL_RESEARCH_DAYS)}
    run = signal_discovery.config_from_payload(queued)
    assert (run.topic, run.offline, run.research_reasoning) == ("Бурение", False, "medium")


# --- Своя модель и лимит у «Дедупа» ---------------------------------------------------------------


class _Recorder:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
        self.calls.append({"model": model, "effort": reasoning_effort, "budget": max_output_tokens})
        return AIResponse(data=self.data, model=model or "m")


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


# --- Расход радара: на экран «Статистика» ---------------------------------------------------


def test_run_collects_ai_usage_by_stage(monkeypatch):
    from oiltech_digest import signal_research
    from tests.test_signal_research import _Research, _recent_event

    research = _Research([_recent_event(), _recent_event(source_url="https://www.slb.com/newsroom/press-release/2")])
    monkeypatch.setattr(signal_research, "make_client", lambda offline: research)
    page = " ".join(["SLB wins Aramco integrated well construction contracts with drilling automation."] * 20)
    monkeypatch.setattr(signal_discovery, "_fetch_full_text", lambda url, fallback_title="", **kwargs: {
        "ok": True, "error": None, "title": fallback_title, "raw_text": page, "published_at": None})
    monkeypatch.setattr(signal_discovery, "_radar_client",
                        lambda: _Recorder({"same_event": True, "reason": "одно событие"}))
    run_config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=True, research_model="gpt-6.1-sol",
                                                        research_reasoning="low")

    run = signal_discovery.run_discovery(run_config, {"topics": [{"name": "Бурение"}], "tags": []})

    # Поиск: вызов с его поисками; «Дедуп»: пара двух находок одного события.
    usage = {row["stage"]: row for row in run["ai_usage"]}
    assert usage == {
        "radar_research": {"stage": "radar_research", "model": "gpt-5-test", "calls": 1, "input_tokens": 17000,
                           "output_tokens": 1300, "web_search_calls": 7},
        "radar_dedup": {"stage": "radar_dedup", "model": config.SIGNAL_DEDUP_MODEL, "calls": 1, "input_tokens": 0,
                        "output_tokens": 0, "web_search_calls": 0},
    }


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
    usage = [{"stage": "radar_research", "model": "gpt-6.1-sol", "calls": 1, "input_tokens": 1, "output_tokens": 1,
              "web_search_calls": 1}]

    signal_discovery.apply_discovery(signal_discovery.SignalDiscoveryConfig(dry_run=True), {"topics": [], "ai_usage": usage})
    signal_discovery.apply_discovery(signal_discovery.SignalDiscoveryConfig(dry_run=False, background_job_id=5),
                                     {"topics": [], "ai_usage": usage})

    # Версия набора правил эксперта поиска (#190) едет в запись расхода: рецепта у поиска нет.
    assert written == [(5, [{**usage[0], "recipe": {"expert_rules_version": 0}}])]


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


def test_manual_radar_run_is_queued_with_a_single_attempt(monkeypatch):
    # Прогон «режима ChatGPT» оплачивается целиком: упавший не повторяется сам (ревью #89, 02.10).
    queued = {}
    monkeypatch.setattr(repository, "create_background_job",
                        lambda kind, payload, **kwargs: queued.update(kwargs) or {"id": 9, "queue_name": "external-agents"})

    cli.main(["enqueue-signal-discovery", "--topic", "Бурение", "--no-offline", "--dry-run"])

    assert queued["max_attempts"] == 1


def test_api_radar_run_is_queued_with_a_single_attempt(monkeypatch):
    from oiltech_digest import api, background_jobs

    queued = {}
    monkeypatch.setattr(background_jobs, "enqueue",
                        lambda kind, payload, **kwargs: queued.update(kwargs) or {"id": 10, "kind": kind})
    monkeypatch.setattr(api, "_job_payload", lambda job: job)

    api.enqueue_signal_discovery(api.SignalDiscoveryRequest(offline=False, dry_run=True), {"id": 1, "role": "admin"})

    assert queued["max_attempts"] == 1
