from oiltech_digest.source_discovery import budget, readiness


def _usage(monkeypatch, **values):
    usage = {"loop_runs": 0, "candidates_created": 0, "candidate_evaluations": 0, **values}
    monkeypatch.setattr(budget.repository, "source_discovery_daily_usage", lambda: usage)


def _limits(loop_runs=4, candidates=100, evaluations=100):
    return {"loop_runs": loop_runs, "candidates_created": candidates, "candidate_evaluations": evaluations}


def test_default_limits_read_environment(monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_MAX_DAILY_LOOP_RUNS", "6")
    monkeypatch.setenv("SOURCE_DISCOVERY_MAX_DAILY_CANDIDATES", "20")
    monkeypatch.delenv("SOURCE_DISCOVERY_MAX_DAILY_EVALUATIONS", raising=False)

    assert budget.default_limits() == {"loop_runs": 6, "candidates_created": 20, "candidate_evaluations": 100}


def test_default_limits_fall_back_on_garbage_and_clip_negative(monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_MAX_DAILY_LOOP_RUNS", "шесть")
    monkeypatch.setenv("SOURCE_DISCOVERY_MAX_DAILY_CANDIDATES", "-5")
    monkeypatch.delenv("SOURCE_DISCOVERY_MAX_DAILY_EVALUATIONS", raising=False)

    assert budget.default_limits() == {"loop_runs": 4, "candidates_created": 0, "candidate_evaluations": 100}


def test_limits_from_payload_override_only_given_fields(monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_MAX_DAILY_CANDIDATES", "20")
    monkeypatch.delenv("SOURCE_DISCOVERY_MAX_DAILY_LOOP_RUNS", raising=False)
    monkeypatch.delenv("SOURCE_DISCOVERY_MAX_DAILY_EVALUATIONS", raising=False)

    limits = budget.limits_from_payload({"max_daily_evaluations": 3, "max_daily_candidates": None})

    assert limits == {"loop_runs": 4, "candidates_created": 20, "candidate_evaluations": 3}


def test_check_passes_under_limits(monkeypatch):
    _usage(monkeypatch, loop_runs=4, candidates_created=99, candidate_evaluations=99)

    state = budget.check(_limits())

    assert state["blocked"] is False
    assert state["reason"] == ""
    assert state["projected"] == {"loop_runs": 4, "candidates_created": 99, "candidate_evaluations": 99}


def test_check_counts_current_loop_run_strictly(monkeypatch):
    # Текущий цикл уже в agent_runs: пятый при лимите 4 — стоп, четвёртый — нет.
    _usage(monkeypatch, loop_runs=5)

    assert budget.check(_limits(loop_runs=4))["reason"] == "daily_loop_budget_reached"


def test_check_ignores_loop_runs_for_one_off_jobs(monkeypatch):
    _usage(monkeypatch, loop_runs=99)

    assert budget.check(_limits(loop_runs=4), count_loop_runs=False)["blocked"] is False


def test_check_blocks_candidates_and_evaluations_at_limit(monkeypatch):
    _usage(monkeypatch, candidates_created=10)
    assert budget.check(_limits(candidates=10))["reason"] == "daily_candidate_budget_reached"

    _usage(monkeypatch, candidate_evaluations=7)
    assert budget.check(_limits(evaluations=7))["reason"] == "daily_evaluation_budget_reached"


def test_check_adds_work_of_current_run(monkeypatch):
    _usage(monkeypatch, candidates_created=8, candidate_evaluations=1)

    state = budget.check(_limits(candidates=10, evaluations=3), candidates_in_run=2, evaluations_in_run=2)

    assert state["projected"]["candidates_created"] == 10
    assert state["reason"] == "daily_candidate_budget_reached"


def test_zero_limit_means_unlimited(monkeypatch):
    _usage(monkeypatch, loop_runs=500, candidates_created=500, candidate_evaluations=500)

    assert budget.check(_limits(loop_runs=0, candidates=0, evaluations=0))["blocked"] is False


def test_check_blocks_when_usage_cannot_be_read(monkeypatch):
    def broken():
        raise RuntimeError("relation agent_runs does not exist")

    monkeypatch.setattr(budget.repository, "source_discovery_daily_usage", broken)

    state = budget.check(_limits())

    assert state["blocked"] is True
    assert state["reason"] == "budget_usage_unavailable"
    assert "agent_runs" in state["error"]


def test_readiness_shows_the_same_limits_as_the_budget(monkeypatch):
    # Экран готовности и проверка бюджета читают одни и те же лимиты.
    monkeypatch.setenv("SOURCE_DISCOVERY_MAX_DAILY_LOOP_RUNS", "6")
    _usage(monkeypatch, loop_runs=6)

    status = readiness._budget_status()

    assert status["limits"] == budget.default_limits()
    assert status["limits"]["loop_runs"] == 6
    assert status["issues"][0]["code"] == "loop_runs_budget_reached"
