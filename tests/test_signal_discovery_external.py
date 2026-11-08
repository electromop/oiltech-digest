"""Радар через внешний воркер: снимок на ядре → прогон без базы → запись на ядре.

И память ОС заказчика: чему отзыв учит (подсказки поиска пишутся как история), что не
возвращаем повторно."""

import json

import pytest

from oiltech_digest import background_jobs, external_worker, signal_discovery, signal_feedback, signal_research
from oiltech_digest.db import repository
from oiltech_digest.processing.openai_client import AIResponse
from oiltech_digest.signal_research import PAGE_NOT_FETCHED


DRILLING = "Бурение, направленное бурение, буровые растворы и буровое оборудование"

TAGS = [
    {
        "id": 20,
        "parent_id": None,
        "name": DRILLING,
        "name_en": "Drilling",
        "description": "Строительство скважин и буровое оборудование",
        "keywords_json": ["бурение"],
        "keywords_en_json": ["closed-loop drilling"],
        "negative_keywords_json": ["лотерея"],
    },
    {
        "id": 32,
        "parent_id": None,
        "name": repository.SYSTEM_TAG_UNCLASSIFIED,
        "name_en": "Unclassified",
        "description": "",
        "keywords_json": [],
        "keywords_en_json": [],
        "negative_keywords_json": [],
    },
]

CHEMISTRY = "Химия, материалы, вода и извлечение ценных компонентов"
MEMORY = {
    "signal_query_hint": [
        {"subject": "2026 closed-loop drilling automation oil gas", "score": 65,
         "facts_json": {"verdict": "approved", "topic": DRILLING}},
        {"subject": "2026 lithium brine extraction oil gas", "score": 65,
         "facts_json": {"verdict": "approved", "topic": CHEMISTRY}},
    ],
}


@pytest.fixture
def core_repository(monkeypatch):
    monkeypatch.setattr(signal_discovery.app_config, "SIGNAL_RADAR_TOPIC_SOURCE", "tags")
    monkeypatch.setattr(repository, "list_enabled_tags", lambda: TAGS)
    monkeypatch.setattr(
        repository,
        "list_signal_agent_memory",
        lambda memory_type=None, status="active", limit=50: MEMORY.get(memory_type, [])[:limit],
    )
    monkeypatch.setattr(repository, "list_reviewed_signal_urls", lambda: ["https://known.example/old/"])
    monkeypatch.setattr(repository, "list_signals_for_dedup", lambda: [])
    monkeypatch.setattr(repository, "list_feed_signals_for_dedup", lambda **kwargs: [])
    monkeypatch.setattr(repository, "collected_url_keys_since", lambda **kwargs: [])
    monkeypatch.setattr(repository, "covered_collection_sites", lambda **kwargs: [])


def _take_database_away(monkeypatch):
    def no_database(*args, **kwargs):
        raise RuntimeError("у внешнего воркера нет базы")

    for name in (
        "get_connection",
        "list_enabled_tags",
        "list_signal_agent_memory",
        "list_signal_radar_topics",
        "list_reviewed_signal_urls",
        "list_signals_for_dedup",
        "list_feed_signals_for_dedup",
    ):
        monkeypatch.setattr(repository, name, no_database)


class _Search:
    """Подставной поиск GPT: находки темы — новая буровая и уже разобранная ссылка. Промпты запоминает."""

    EVENTS = [
        {"title": "Operator deploys closed-loop drilling automation on 40 wells", "company": "Operator",
         "event_date": "", "summary": "Автоматизация бурения на 40 скважинах", "why_important": "",
         "source_url": "https://new.example/rig", "publisher": "New", "source_kind": "primary", "technology": ""},
        {"title": "Reviewed drilling item about a rig in the field", "company": "Operator", "event_date": "",
         "summary": "", "why_important": "", "source_url": "https://known.example/old", "publisher": "Known",
         "source_kind": "primary", "technology": ""},
    ]

    def __init__(self):
        self.prompts: list[str] = []

    def research_json(self, instructions, user_input, schema, **kwargs):
        self.prompts.append(user_input)
        return AIResponse(data={"events": self.EVENTS, "_cited_urls": [], "_web_search_calls": 2},
                          model="gpt-6.1-sol", input_tokens=100, output_tokens=10)


def test_worker_run_gets_tags_memory_and_reviewed_urls_from_snapshot(monkeypatch, core_repository):
    payload = signal_discovery.build_external_payload({"offline": False})
    json.dumps(payload)  # уезжает воркеру по HTTP

    # Служебный приёмник темой радара не становится.
    assert [topic["name"] for topic in payload["snapshot"]["topics"]] == [DRILLING]

    _take_database_away(monkeypatch)
    search = _Search()
    monkeypatch.setattr(signal_research, "make_client", lambda offline=False: search)
    page = " ".join(["Operator deploys closed-loop drilling automation on 40 wells in the field."] * 20)
    monkeypatch.setattr(signal_discovery, "_fetch_full_text", lambda url, fallback_title="", **kwargs: {
        "ok": True, "error": None, "title": fallback_title, "raw_text": page, "published_at": None})
    beats: list[int] = []

    result = signal_discovery.process_external_payload(payload, heartbeat=lambda: beats.append(1))

    json.dumps(result)  # уходит ядру по HTTP
    # Ключи тематики из снимка тегов попали в промпт поиска, ОС экспертов — нет (граница данных О-7).
    [prompt] = search.prompts
    assert "Ключевые слова: бурение, closed-loop drilling" in prompt
    assert "2026 closed-loop drilling automation oil gas" not in prompt
    topic = result["run"]["topics"][0]
    # Разобранная человеком ссылка (известна ядру по снимку) находкой не становится.
    assert topic["skipped_reviewed"] == 1
    [candidate] = topic["candidates"]
    assert candidate["signal"]["evidence"][0]["source_url"] == "https://new.example/rig"
    assert candidate["signal"]["theme"] == DRILLING
    assert beats  # воркер продлевает аренду задачи по ходу прогона


def test_core_applies_external_result_and_records_generation_run(isolated_db, monkeypatch):
    created: dict = {}
    finished: list[tuple] = []
    monkeypatch.setattr(repository, "create_signal_generation_run", lambda **kwargs: created.update(kwargs) or 77)
    monkeypatch.setattr(
        repository, "finish_signal_generation_run", lambda run_id, **kwargs: finished.append((run_id, kwargs))
    )
    finding = {"title": "Operator deploys closed-loop drilling", "theme": DRILLING, "summary": "", "companies": [],
               "maturity": "watch", "score": 0, "signal_key": "k1", "evidence_count": 1,
               "evidence": [{"source_url": "https://new.example/rig", "title": "Operator deploys closed-loop drilling"}]}
    research = {"status": "ok", "events": 2, "evidence_count": 1, "web_search_calls": 2, "attempts": 1,
                "fulltext": {"attempted": 2, "fetched": 1, "too_short": 0, "failed": 1, "via": {}, "pdf": 0}}
    result = {
        "signal_discovery": True,
        "config": {"offline": False, "dry_run": False},
        "run": {"topics": [{
            "topic": DRILLING,
            "total_evidence": 1,
            "skipped_reviewed": 0,
            "clusters": 1,
            "web_search": {"status": "ok", "provider": "openai_web_search", "research": research, "evidence_count": 1},
            "candidates": [{"signal": finding}],
            "filtered_findings": [{"stage": "search", "title": "Paywalled rig contract in Kuwait",
                                   "url": "https://bad.example/rig", "reason": PAGE_NOT_FETCHED}],
        }]},
    }

    summary = signal_discovery.apply_external_result(result, job_id=55)

    assert created["background_job_id"] == 55
    # Находка ждёт «Отбора»; технический отсев поиска — карточкой «Отсеяно» в разделах «Поиск → …».
    with repository.get_connection() as conn:
        stored = {key: (selector, stage)
                  for key, selector, stage in conn.execute("SELECT signal_key, selector, filter_stage FROM signals")}
    assert stored.pop("k1") == ("otbor", None)
    assert list(stored.values()) == [(None, "search")]
    assert finished == [(77, {"status": "ok", "result": {
        "topics": 1, "signals": 1, "search_health": {"topics": 1, "failed": 0, "first_error": None},
    }})]
    assert summary["signals"] == 1
    [finding_id] = summary["topics"][0]["signal_ids"]
    assert summary["topics"][0] == {
        "topic": DRILLING, "web_status": "ok", "web_error": None, "provider": "openai_web_search",
        "research": {"status": "ok", "events": 2, "evidence_count": 1, "web_search_calls": 2, "attempts": 1,
                     "unverified": 0, "dropped": 0, "fulltext": research["fulltext"]},
        "total_evidence": 1, "skipped_reviewed": 0, "clusters": 1, "signals": 1, "duplicates": 0,
        "signal_ids": [finding_id], "found_again_ids": [], "already_collected": [],
    }
    assert summary["dedup"] is None  # прогон без «Дедупа» (офлайн) пишется так же


class _FakeClient:
    def __init__(self):
        self.completed: list[dict] = []
        self.failed: list[str] = []

    def progress(self, job, progress):
        pass

    def heartbeat(self, job):
        pass

    def complete(self, job, result):
        self.completed.append(result)

    def fail(self, job, error, *, retryable=True, retry_after_seconds=300):
        self.failed.append(error)


def test_external_worker_handles_signal_discovery_kind(monkeypatch):
    monkeypatch.setattr(
        signal_discovery,
        "process_external_payload",
        lambda payload, heartbeat=None: {"signal_discovery": True, "run": {"topics": []}, "config": payload["config"]},
    )
    client = _FakeClient()

    external_worker._handle_job(client, {"id": 1, "kind": "signal_discovery", "payload": {"config": {"offline": False}}})

    assert client.failed == []  # прежде: Unsupported external job kind: signal_discovery
    assert client.completed == [{"signal_discovery": True, "run": {"topics": []}, "config": {"offline": False}}]


def test_topics_from_tags_take_root_customer_tags_only():
    tags = [
        *TAGS,
        {"id": 40, "parent_id": 20, "name": "Буровые растворы", "description": ""},
    ]

    topics = signal_discovery.topics_from_tags(tags)

    assert topics == [{"name": DRILLING, "description": "Строительство скважин и буровое оборудование",
                       "query_seeds_json": [], "tag_id": 20}]


def test_topic_tag_context_takes_exactly_the_named_tag():
    neighbours = [
        TAGS[0],
        {"id": 24, "parent_id": None, "name": "Добыча, механизированный фонд и внутрискважинное оборудование",
         "keywords_json": ["ЭЦН"], "keywords_en_json": ["ESP"], "negative_keywords_json": []},
        {"id": 41, "parent_id": 20, "name": "Буровые растворы", "keywords_json": ["раствор"],
         "keywords_en_json": [], "negative_keywords_json": []},
    ]

    selected = signal_discovery._select_topic_tags(DRILLING, neighbours)

    # Общее слово «оборудование» больше не притягивает «Добычу».
    assert [tag["id"] for tag in selected] == [20, 41]


# --- обучение на ОС ---------------------------------------------------------

_LITHIUM = {
    "Комментарий": "Школьный проект «Больших вызовов», промышленного внедрения нет",
    "Сигнал": "Добыча лития из попутно добываемой воды",
    "URL": "https://example.com/lithium",
    "reason": "Образовательный проект, не сигнал",
}


@pytest.mark.parametrize("verdict", ["reject", "wrong_domain", "too_generic", "merge_duplicate"])
def test_rejected_feedback_does_not_become_search_hint(verdict):
    memories = signal_feedback.extract_feedback_memories({**_LITHIUM, "verdict": verdict})

    assert [m for m in memories if m["memory_type"] == "signal_query_hint"] == []


def test_approved_feedback_still_becomes_search_hint():
    memories = signal_feedback.extract_feedback_memories({**_LITHIUM, "verdict": "approved"})

    assert [m["subject"] for m in memories if m["memory_type"] == "signal_query_hint"]


def test_failed_daily_radar_is_not_requeued_the_same_day(isolated_db, monkeypatch):
    monkeypatch.setattr(background_jobs.config, "SIGNAL_DISCOVERY_DAILY_ENABLED", True)
    with repository.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO background_jobs (kind, queue_name, status, payload_json, max_attempts)
            VALUES ('signal_discovery', 'external-ai', 'failed', %s::jsonb, 1)
            """,
            (json.dumps(background_jobs.daily_signal_discovery_payload()),),
        )
        conn.commit()
    enqueued: list = []
    monkeypatch.setattr(background_jobs, "enqueue", lambda *args, **kwargs: enqueued.append(args))

    result = background_jobs.enqueue_daily_signal_discovery()

    assert result["enqueued"] is False and result["reason"] == "already_scheduled"
    assert enqueued == []


def test_yesterday_evening_daily_radar_does_not_block_this_morning(isolated_db, monkeypatch):
    """18.09: ежедневный радар запущен в 17:15 МСК, и утренний крон 19.09 решил, что
    «уже было» — окно было «24 часа», а не «те же сутки»."""
    monkeypatch.setattr(background_jobs.config, "SIGNAL_DISCOVERY_DAILY_ENABLED", True)
    enqueued: list = []
    monkeypatch.setattr(background_jobs, "enqueue", lambda *args, **kwargs: enqueued.append(args) or {"id": 1})

    def daily_job_at(sql_created_at):
        with repository.get_connection() as conn:
            conn.execute(
                f"""
                INSERT INTO background_jobs (kind, queue_name, status, payload_json, max_attempts, created_at)
                VALUES ('signal_discovery', 'external-ai', 'ok', %s::jsonb, 1, {sql_created_at})
                """,
                (json.dumps(background_jobs.daily_signal_discovery_payload()),),
            )
            conn.commit()

    # Вчера в 23:59 по Москве — меньше 24 часов назад, но это прошлые сутки.
    daily_job_at("(date_trunc('day', now() AT TIME ZONE 'Europe/Moscow') - interval '1 minute') AT TIME ZONE 'Europe/Moscow'")
    assert background_jobs.enqueue_daily_signal_discovery()["enqueued"] is True

    # Сегодняшняя — блокирует, как и раньше.
    daily_job_at("now() - interval '1 second'")
    assert background_jobs.enqueue_daily_signal_discovery()["reason"] == "already_scheduled"
    assert len(enqueued) == 1
