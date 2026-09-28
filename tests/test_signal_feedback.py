from oiltech_digest import signal_feedback


def test_extract_feedback_memories_parses_glossary_rules_and_title():
    row = {
        "Сигнал": "ADNOC deploys automated rig",
        "Источник": "ADNOC",
        "URL": "https://adnoc.example/news",
        "Комментарий": (
            "1. Корректировка названия: ADNOC вводит полностью автоматизированную шагающую буровую с AI-мониторингом\n"
            "2. walking island rig -> шагающая буровая для искусственных островов; "
            "automated pipe handling → автоматизированные спуско-подъемные / трубные операции\n"
            "3. Не путать automated и autonomous. Всегда проверять дату публикации отдельно от даты обнаружения.\n"
            "Источник отличный"
        ),
    }

    memories = signal_feedback.extract_feedback_memories(row)
    by_type = {}
    for memory in memories:
        by_type.setdefault(memory["memory_type"], []).append(memory)

    glossary = {memory["subject"]: memory["facts"]["preferred_ru"] for memory in by_type["signal_glossary"]}
    assert glossary["walking island rig"] == "шагающая буровая для искусственных островов"
    assert glossary["automated pipe handling"] == "автоматизированные спуско-подъемные / трубные операции"
    assert by_type["signal_title_correction"][0]["facts"]["preferred_title"].startswith("ADNOC вводит")
    assert any("Не путать automated и autonomous" in memory["subject"] for memory in by_type["signal_quality_rule"])
    assert any(memory["subject"] == "adnoc.example" for memory in by_type["signal_source_preference"])
    assert any("walking island rig" in memory["subject"] for memory in by_type["signal_query_hint"])


def test_import_signal_feedback_csv_dry_run(tmp_path):
    csv_path = tmp_path / "feedback.csv"
    csv_path.write_text(
        "#,Сигнал,Суть,Почему сейчас,Перенос в нефтесервис,Словарь,Источник,URL,Комментарий\n"
        "1,ADNOC rig,,,,,ADNOC,https://adnoc.example/news,"
        "\"walking capability -> возможность перемещения между скважинами без демонтажа\"\n",
        encoding="utf-8",
    )

    result = signal_feedback.import_signal_feedback_csv(csv_path, dry_run=True)

    assert result["rows"] == 1
    assert result["feedback_events"] == 1
    assert result["memories"] >= 1
    assert result["extracted"][0]["memory_type"] == "signal_glossary"


def test_store_signal_feedback_writes_event_and_memories(monkeypatch):
    calls = {"events": [], "memories": []}

    monkeypatch.setattr(
        signal_feedback.repository,
        "record_signal_feedback_event",
        lambda *args, **kwargs: calls["events"].append((args, kwargs)) or 42,
    )
    monkeypatch.setattr(
        signal_feedback.repository,
        "upsert_signal_agent_memory",
        lambda **kwargs: calls["memories"].append(kwargs) or len(calls["memories"]) + 1,
    )
    monkeypatch.setattr(
        signal_feedback.repository,
        "attach_feedback_to_signal_training_examples",
        lambda *args, **kwargs: calls.setdefault("attachments", []).append((args, kwargs)) or 1,
    )

    result = signal_feedback.store_signal_feedback(
        {
            "signal_id": 7,
            "source_url": "https://example.com/signal",
            "signal_title": "Closed loop drilling",
            "comment": "closed-loop control -> управление с замкнутым контуром",
        },
        user_id=3,
    )

    assert result["event_id"] == 42
    assert result["memories"] >= 1
    assert calls["events"][0][1]["signal_id"] == 7
    assert calls["events"][0][1]["user_id"] == 3
    assert calls["attachments"][0][1]["signal_id"] == 7
    assert any(call["memory_type"] == "signal_glossary" for call in calls["memories"])


def test_structured_signal_feedback_writes_verdict_and_corrections(monkeypatch):
    calls = {"events": [], "memories": []}

    monkeypatch.setattr(
        signal_feedback.repository,
        "record_signal_feedback_event",
        lambda *args, **kwargs: calls["events"].append((args, kwargs)) or 43,
    )
    monkeypatch.setattr(
        signal_feedback.repository,
        "upsert_signal_agent_memory",
        lambda **kwargs: calls["memories"].append(kwargs) or len(calls["memories"]) + 1,
    )
    monkeypatch.setattr(
        signal_feedback.repository,
        "attach_feedback_to_signal_training_examples",
        lambda *args, **kwargs: calls.setdefault("attachments", []).append((args, kwargs)) or 1,
    )

    result = signal_feedback.store_signal_feedback(
        {
            "signal_id": 7,
            "source_url": "https://example.com/signal",
            "signal_title": "Closed loop drilling",
            "comment": "",
            "verdict": "merge_duplicate",
            "reason": "Тот же инфоповод",
            "corrected_title": "Управление бурением с замкнутым контуром",
            "corrected_thesis": "Промышленный переход к автоматизированному управлению бурением.",
            "duplicate_of_signal_id": 5,
        },
        user_id=3,
    )

    assert result["event_id"] == 43
    event_kwargs = calls["events"][0][1]
    assert event_kwargs["verdict"] == "merge_duplicate"
    assert event_kwargs["reason"] == "Тот же инфоповод"
    assert event_kwargs["corrected_title"] == "Управление бурением с замкнутым контуром"
    assert event_kwargs["duplicate_of_signal_id"] == 5
    assert calls["attachments"][0][0] == (43,)
    assert calls["attachments"][0][1]["signal_id"] == 7
    memory_types = {call["memory_type"] for call in calls["memories"]}
    assert "signal_verdict" in memory_types
    assert "signal_duplicate" in memory_types
    assert "signal_title_correction" in memory_types
