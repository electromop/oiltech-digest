from oiltech_digest import network_policy


def test_ai_processing_routes_local_by_default(monkeypatch):
    monkeypatch.setattr(network_policy.config, "EXTERNAL_WORKERS_ENABLED", False)
    monkeypatch.setattr(network_policy.config, "AI_EXECUTION_REGION", "external")

    decision = network_policy.route_ai_processing()

    assert decision.queue_name == "ai"
    assert decision.execution_region == "ru"
    assert decision.capability == "openai"


def test_ai_processing_can_route_to_external(monkeypatch):
    monkeypatch.setattr(network_policy.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(network_policy.config, "AI_EXECUTION_REGION", "external")

    decision = network_policy.route_ai_processing()

    assert decision.queue_name == "external-ai"
    assert decision.execution_region == "external"
    assert decision.capability == "openai"


def test_source_task_routes_external_source_when_enabled(monkeypatch):
    monkeypatch.setattr(network_policy.config, "EXTERNAL_WORKERS_ENABLED", True)
    monkeypatch.setattr(network_policy.config, "FETCH_EXTERNAL_ENABLED", True)

    decision = network_policy.route_source_task(
        {"parse_strategy": "playwright", "network_region": "external", "network_profile": "browser"},
        task_kind="scrape",
    )

    assert decision.queue_name == "external-playwright"
    assert decision.execution_region == "external"
    assert decision.capability == "playwright"


def test_source_task_falls_back_to_local_when_external_disabled(monkeypatch):
    monkeypatch.setattr(network_policy.config, "EXTERNAL_WORKERS_ENABLED", False)
    monkeypatch.setattr(network_policy.config, "FETCH_EXTERNAL_ENABLED", False)

    decision = network_policy.route_source_task(
        {"parse_strategy": "request", "network_region": "external", "network_profile": "direct"},
        task_kind="scrape",
    )

    assert decision.queue_name == "default"
    assert decision.execution_region == "ru"
    assert decision.capability == "http_fetch"


def test_digest_pdf_routes_to_local_playwright():
    decision = network_policy.route_digest_export("pdf")

    assert decision.queue_name == "playwright"
    assert decision.execution_region == "ru"
    assert decision.capability == "playwright"

