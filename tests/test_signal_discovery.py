import hashlib

import pytest

from oiltech_digest import signal_discovery


@pytest.fixture(autouse=True)
def no_signal_feedback_memory(monkeypatch):
    monkeypatch.setattr(signal_discovery, "apply_feedback_glossary", lambda text, topic=None: text)
    # Темы — из таблицы, которую тесты подменяют; фильтр разобранного — пустой.
    # Иначе разведка пошла бы в настоящую локальную базу за тегами и адресами.
    monkeypatch.setattr(signal_discovery.app_config, "SIGNAL_RADAR_TOPIC_SOURCE", "table")
    monkeypatch.setattr(signal_discovery.repository, "list_reviewed_signal_urls", lambda: [])


def test_default_radar_topics_cover_business_directions():
    names = [topic["name"] for topic in signal_discovery.DEFAULT_RADAR_TOPICS]
    expected_fragments = [
        "Сейсморазведка",
        "ГИС",
        "Бурение",
        "Цементирование",
        "Заканчивание",
        "ГРП",
        "КРС",
        "Добыча",
        "Повышение нефтеотдачи",
        "Промысловая инфраструктура",
        "Энергетика",
        "Роботизация",
        "Логистика",
        "Экология",
        "Лабораторные",
        "Инжиниринг",
        "Рынок",
    ]

    assert len(signal_discovery.DEFAULT_RADAR_TOPICS) >= 21
    for fragment in expected_fragments:
        assert any(fragment in name for name in names), fragment


def test_finding_key_is_its_link_in_any_spelling_and_matches_keys_written_before():
    """Ключ находки по ссылке: другое написание адреса даёт ту же находку, найденную снова, и ключ тот же, что у
    находок, заведённых до 08.11 (формула прежняя): решённое «Отбором» второй раз не решается."""
    evidence = [
        {
            "source_url": "https://www.example.com/news/autonomous-trucks?utm=1",
            "title": "Autonomous trucks launched in oilfield logistics",
            "strength": 0.8,
        }
    ]
    first = signal_discovery._signal_key(evidence)
    second = signal_discovery._signal_key([
        {**evidence[0], "source_url": "http://example.com/news/autonomous-trucks#section"}
    ])

    assert first == second == hashlib.sha256(b"url:example.com/news/autonomous-trucks").hexdigest()[:24]


def test_refresh_signal_evidence_count_uses_actual_linked_evidence(isolated_db):
    from oiltech_digest.db import repository

    signal_id = repository.upsert_signal(
        {
            "signal_key": "test-refresh-evidence-count",
            "title": "Автономная инспекция трубопровода",
            "theme": "Роботизация и автономные системы",
            "summary": "Тестовый сигнал",
            "thesis": "Тестовый сигнал",
            "transferability": "",
            "maturity": "watch",
            "confidence": 0.8,
            "score": 70,
            "why_now": "",
            "why_not_noise": "",
            "companies": [],
            "industries": [],
            "evidence_count": 9,
        }
    )
    repository.upsert_signal_evidence(
        signal_id,
        {
            "source_url": "https://example.com/pipeline-robot",
            "title": "Pipeline robot inspects oil and gas infrastructure",
            "publisher": "example.com",
            "evidence_type": "deployment",
            "strength": 0.8,
        },
    )

    assert repository.refresh_signal_evidence_count(signal_id) == 1
    stored = repository.list_signals(limit=1)[0]
    assert stored["evidence_count"] == 1


