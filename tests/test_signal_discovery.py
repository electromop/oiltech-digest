from oiltech_digest import signal_discovery


def test_discover_signals_builds_radar_signal_from_article_evidence(monkeypatch):
    saved = {"signals": 0, "evidence": 0}

    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_radar_topics",
        lambda enabled_only=True: [{"name": "HSE robotics / Physical AI", "query_seeds_json": []}],
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_article_evidence",
        lambda **kwargs: [
            {
                "article_id": 1,
                "title": "ADNOC selected autonomous inspection robots for hazardous sites",
                "title_ru": "ADNOC выбрала автономных роботов для опасных объектов",
                "source_url": "https://example.com/adnoc-robots",
                "published_at": None,
                "collected_at": None,
                "raw_text": "Autonomous robot supplier will inspect equipment and reduce human exposure.",
                "publisher": "Industrial News",
                "summary": "ADNOC допустила поставщика autonomous robots для инспекций и environmental monitoring.",
                "relevant": True,
                "relevance_reason": "HSE robotics",
                "tag_name": "HSE",
                "tag_name_en": "HSE",
                "total_score": 85,
                "score_label": "Высокая",
                "score_explanation": "Промышленный HSE-сценарий",
            },
            {
                "article_id": 2,
                "title": "Physical AI robots reduce hazardous work",
                "title_ru": "Physical AI снижает присутствие людей в опасных операциях",
                "source_url": "https://example.com/physical-ai",
                "published_at": None,
                "collected_at": None,
                "raw_text": "Robotic deployment for oilfield hazardous operations includes autonomous navigation.",
                "publisher": "Robotics Wire",
                "summary": "Robotic deployment переносит инспекции в автономный контур.",
                "relevant": True,
                "relevance_reason": "HSE robotics",
                "tag_name": "HSE",
                "tag_name_en": "HSE",
                "total_score": 80,
                "score_label": "Высокая",
                "score_explanation": "Снижение exposure",
            },
        ],
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "upsert_signal",
        lambda payload: saved.__setitem__("signals", saved["signals"] + 1) or 10,
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "upsert_signal_evidence",
        lambda signal_id, evidence: saved.__setitem__("evidence", saved["evidence"] + 1) or 20,
    )

    result = signal_discovery.discover_signals(
        signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True)
    )

    assert result["signals"]
    assert result["signals"][0]["maturity"] in {"watch", "shortlist", "proven"}
    assert result["signals"][0]["evidence_count"] == 2
    assert saved == {"signals": 0, "evidence": 0}


def test_discover_signals_persists_when_not_dry_run(monkeypatch):
    saved = {"signals": 0, "evidence": 0}

    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_radar_topics",
        lambda enabled_only=True: [{"name": "Predictive HSE", "query_seeds_json": []}],
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_article_evidence",
        lambda **kwargs: [
            {
                "article_id": 3,
                "title": "Predictive safety deployment detects hazardous equipment condition",
                "title_ru": "Predictive safety выявляет опасное состояние оборудования",
                "source_url": "https://example.com/predictive-safety",
                "published_at": None,
                "raw_text": "Deployment uses predictive analytics for hazardous condition detection.",
                "publisher": "Mining Safety",
                "summary": "Deployment predicts hazardous condition before incidents.",
                "relevant": True,
                "total_score": 90,
            }
        ],
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "upsert_signal",
        lambda payload: saved.__setitem__("signals", saved["signals"] + 1) or 10,
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "upsert_signal_evidence",
        lambda signal_id, evidence: saved.__setitem__("evidence", saved["evidence"] + 1) or 20,
    )

    result = signal_discovery.discover_signals(
        signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=False)
    )

    assert result["signals"]
    assert saved == {"signals": 1, "evidence": 1}


def test_discover_signals_filters_generic_ai_without_industry_context(monkeypatch):
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_radar_topics",
        lambda enabled_only=True: [{"name": "HSE robotics / Physical AI", "query_seeds_json": []}],
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_article_evidence",
        lambda **kwargs: [
            {
                "article_id": 1,
                "title": "AI robotics breakthrough for office workflows",
                "title_ru": "AI robotics breakthrough for office workflows",
                "source_url": "https://example.com/generic-ai",
                "published_at": None,
                "raw_text": "A software agent automates generic knowledge work.",
                "publisher": "Tech News",
                "summary": "Generic automation news for office teams.",
                "relevant": True,
                "total_score": 95,
            }
        ],
    )

    result = signal_discovery.discover_signals(
        signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True)
    )

    assert result["signals"] == []


def test_discover_signals_can_use_web_evidence_without_registered_sources(monkeypatch):
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_radar_topics",
        lambda enabled_only=True: [{"name": "Digital PTW / Control of Work", "query_seeds_json": []}],
    )
    monkeypatch.setattr(signal_discovery.repository, "list_signal_article_evidence", lambda **kwargs: [])
    monkeypatch.setattr(
        signal_discovery,
        "_search_web_evidence",
        lambda topic, config: {
            "status": "ok",
            "provider": "test",
            "queries": ["2026 electronic permit to work oil gas"],
            "results": 2,
            "evidence": [
                {
                    "article_id": None,
                    "source_url": "https://example.com/ptw",
                    "title": "Oil refinery deploys electronic permit to work with interlocks",
                    "title_ru": "НПЗ внедрил электронный наряд-допуск с цифровыми блокировками",
                    "publisher": "example.com",
                    "published_at": None,
                    "evidence_type": "deployment",
                    "extracted_fact": "Oil and gas operator moved high-risk work permits into a digital control system.",
                    "summary_ru": "Оператор нефтегаза перевёл наряды-допуски в цифровой контроль.",
                    "strength": 0.8,
                    "topic": "Digital PTW / Control of Work",
                    "raw_payload": {"evidence_source": "web_search"},
                },
                {
                    "article_id": None,
                    "source_url": "https://example.com/ptw-2",
                    "title": "Petrochemical operator reports 75% faster PTW approvals",
                    "title_ru": "Нефтехимический оператор ускорил согласование PTW на 75%",
                    "publisher": "example.com",
                    "published_at": None,
                    "evidence_type": "deployment",
                    "extracted_fact": "Petrochemical site reports digital permit to work deployment with 75% faster approvals.",
                    "summary_ru": "Нефтехимический объект внедрил электронный наряд-допуск и ускорил согласования на 75%.",
                    "strength": 0.8,
                    "topic": "Digital PTW / Control of Work",
                    "raw_payload": {"evidence_source": "web_search"},
                },
            ],
        },
    )

    result = signal_discovery.discover_signals(
        signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True, web_search=True)
    )

    assert result["signals"]
    assert result["topic_results"][0]["web_search"]["status"] == "ok"
    assert result["topic_results"][0]["article_evidence"] == 0


def test_web_only_skips_local_article_evidence(monkeypatch):
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_radar_topics",
        lambda enabled_only=True: [{"name": "Digital PTW / Control of Work", "query_seeds_json": []}],
    )
    monkeypatch.setattr(
        signal_discovery.repository,
        "list_signal_article_evidence",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("local articles must not be queried")),
    )
    monkeypatch.setattr(
        signal_discovery,
        "_search_web_evidence",
        lambda topic, config: {"status": "empty", "provider": "test", "queries": [], "results": 0, "evidence": []},
    )

    result = signal_discovery.discover_signals(
        signal_discovery.SignalDiscoveryConfig(offline=True, dry_run=True, web_only=True)
    )

    assert result["web_search"] is True
    assert result["web_only"] is True
    assert result["topic_results"][0]["article_evidence"] == 0


def test_search_result_evidence_cleans_html_snippets():
    evidence = signal_discovery._search_result_to_evidence(
        {
            "url": "https://example.com/oil-gas-robots",
            "title": "Oil &amp; Gas <strong>robots</strong>",
            "snippet": "<strong>Robots</strong> inspect oil and gas facilities.",
            "provider": "test",
        },
        "HSE robotics / Physical AI",
    )

    assert evidence["title"] == "Oil & Gas robots"
    assert evidence["extracted_fact"] == "Robots inspect oil and gas facilities."
