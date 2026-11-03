"""Offline-тесты новых стадий: перевод заголовков и перепрогон релевантности (+удаление)."""

import pytest

from oiltech_digest import config
from oiltech_digest.processing import external_ai, pipeline
from oiltech_digest.processing.openai_client import AIResponse, OfflineAIClient


class _RecordingClient:
    model = "fake"

    def __init__(self, relevant: bool = True) -> None:
        self.calls: list[str] = []
        self.relevant = relevant

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900,
                      model=None, reasoning_effort=None):
        name = schema["name"]
        self.calls.append(name)
        if name == "article_relevance":
            return AIResponse(data={"relevant": self.relevant, "reason": "x"}, model=model or "fake")
        if name == "article_title_translation":
            return AIResponse(data={"title_ru": "Переведено"}, model=model or "fake")
        return AIResponse(data={}, model="fake")


# --- эвристика языка / переводчик -------------------------------------------

def test_needs_translation_detects_language():
    assert pipeline._needs_translation("Directional drilling automation") is True
    assert pipeline._needs_translation("Война: удары по городу") is False
    assert pipeline._needs_translation("OPEC+ решение по добыче") is False  # большинство кириллицы
    assert pipeline._needs_translation("") is False
    assert pipeline._needs_translation("2026 — 100%") is False  # нет букв


def test_title_ru_for_russian_title_skips_ai():
    client = _RecordingClient()
    title_ru, response = pipeline.title_ru_for_article({"title": "Нефтегаз: новый проект"}, client)
    assert title_ru == "Нефтегаз: новый проект"
    assert response is None
    assert client.calls == []  # русский заголовок не идёт в модель


def test_title_ru_for_foreign_title_calls_ai():
    client = _RecordingClient()
    title_ru, response = pipeline.title_ru_for_article({"title": "Shell expands LNG capacity"}, client)
    assert title_ru == "Переведено"
    assert response is not None
    assert client.calls == ["article_title_translation"]


# --- русский заголовок принятой публикации: правка по правилам эксперта (#150) -------

VESTI_TITLE = "\"Татнефть\" построит предприятие мощностью 30 тысяч тонн сорбентов - Новости на Вести.ru"
VESTI = {"title": VESTI_TITLE, "source_name": "Вести", "language": "ru",
         "raw_text": "Татнефть построит в Татарстане предприятие по выпуску сорбентов мощностью 30 тысяч тонн в год."}


class _EditClient:
    """Подставная модель правки заголовка: заданный ответ; вызовы — схема, модель, уровень, вход."""

    def __init__(self, title_ru: str) -> None:
        self.title_ru = title_ru
        self.calls: list[dict] = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900,
                      model=None, reasoning_effort=None):
        self.calls.append({"schema": schema["name"], "model": model, "reasoning": reasoning_effort,
                           "input": user_input})
        return AIResponse(data={"title_ru": self.title_ru}, model=model or "fake", input_tokens=900,
                          output_tokens=40)


def test_russian_title_off_the_rules_is_edited_by_the_translation_model(monkeypatch):
    """Принятая публикация: русский заголовок не по правилам эксперта (тире, название издания) правит модель перевода
    — правка, а не перевод (решение владельца 03.11)."""
    monkeypatch.setattr(config, "OPENAI_TRANSLATE_MODEL", "translate-model")
    monkeypatch.setattr(config, "OPENAI_TRANSLATE_REASONING", "low")
    client = _EditClient("«Татнефть» построит предприятие на 30 тысяч тонн сорбентов")

    title_ru, response = pipeline.title_ru_for_article(dict(VESTI), client, edit_russian=True)

    assert title_ru == "«Татнефть» построит предприятие на 30 тысяч тонн сорбентов"
    assert response is not None
    [call] = client.calls
    assert (call["schema"], call["model"], call["reasoning"]) == ("article_title_edit", "translate-model", "low")
    assert f"title: {VESTI_TITLE}" in call["input"] and "source: Вести" in call["input"]


@pytest.mark.parametrize("answer", ["«Татнефть» построит завод сорбентов на 50 тысяч тонн", ""],
                         ids=["number-not-in-publication", "empty"])
def test_edit_that_cannot_be_taken_keeps_the_publisher_title(answer):
    """Число, которого нет ни в заголовке, ни в тексте, — выдумка модели; пустой ответ — не заголовок. Остаётся
    заголовок издателя, а оплаченный вызов учитывается."""
    title_ru, response = pipeline.title_ru_for_article(dict(VESTI), _EditClient(answer), edit_russian=True)

    assert title_ru == VESTI_TITLE
    assert response is not None


def test_russian_title_by_the_rules_does_not_call_the_model():
    """Заголовок уже по правилам, которые видно кодом, — как есть, модель не зовётся."""
    client = _EditClient("не должен прийти")
    title = "Минприроды РФ предлагает расширить доступ к шельфовым проектам для опытных компаний"

    assert pipeline.title_ru_for_article({"title": title, "source_name": "Агентство нефтегазовой информации (сайт)"},
                                         client, edit_russian=True) == (title, None)
    assert client.calls == []


@pytest.mark.parametrize(("title", "source", "by_rules"), [
    # Заголовки принятых статей прода за сентябрь и октябрь 2026; составные помечены.
    ("Минприроды РФ предлагает расширить доступ к шельфовым проектам для опытных компаний",
     "Агентство нефтегазовой информации (сайт)", True),
    ("«Роснефть» совершенствует технологии ремонта скважин", "Роснефть", True),  # издатель — автор события
    ("Второй контур реактора №1 АЭС Аккую в Турции прошел проверку на прочность", "Neftegaz.ru", True),  # № — не эмодзи
    ("Минэнерго РФ опровергло слухи о росте цен на бензин из-за затрат на импорт топлива",
     "Агентство нефтегазовой информации (сайт)", True),  # дефис внутри слова
    ("Подрядчик выполнит 400 операций ГРП в 2027–2029 годах", "Neftegaz.ru", True),  # составной: тире диапазона
    ("Александр Новак: Россия не ведет переговоров с американскими компаниями о возвращении в нефтегазовые проекты",
     "Агентство нефтегазовой информации (сайт)", False),  # длиннее 90 знаков
    ("Хуситы ударили по НПЗ в Эр-Рияде — Новости о нефти и газе в России и мире - Нефть и капитал",
     "OilCapital", False),
    ("Власти США могут снять санкции с РФПИ - New York Times", "Energy Today", False),
    ("Химический состав нефти | Геологический портал GeoKniga", "Geokniga", False),
    ("«Росатом» завершил оснащение АЭС «Руппур» (Бангладеш) системами управления энергоблоками", "Росатом", False),
    ("Главные новости понедельника: топ-10 от Нефтегаз.РУ", "Neftegaz.ru", False),  # издание доменом
    ("Дорогие читатели! Вышел сентябрьский номер журнала Neftegaz.RU", "Neftegaz.ru", False),
    ("Главные события в ТЭК за неделю в еженедельном дайджесте Energy Today", "Energy Today", False),  # составной
    ("Властям Франции вроде хочется Total пощипать, но опасно...", "Energy Today", False),  # обрыв
    ("🛢 Новак сообщил о возобновлении ударов по российским НПЗ", "Газбатюшка", False),
    ("#ЦифраДня Вода температурой 300 градусов все еще жидкая", "Энергия+", False),  # составной: хэштег
])
def test_title_rules_seen_by_code(title, source, by_rules):
    assert pipeline.title_follows_rules(title, source) is by_rules


# --- внешний перепрогон релевантности ---------------------------------------

def _recheck_payload(relevant_text: str) -> dict:
    return {
        "offline": True,
        "articles": [{
            "id": 7, "title": "t", "url": "https://e/x", "language": "ru",
            "raw_text": relevant_text, "source_name": "Источник", "source_category": "Новости",
        }],
        "tags": [{"id": 1, "name": "Бурение", "parent_id": None, "negative_keywords_json": ["каблук"]}],
    }


def test_process_recheck_payload_gate_only():
    result = external_ai.process_recheck_payload(_recheck_payload("Нефтесервисная новость"))
    assert result["recheck_relevance"] is True
    item = result["articles"][0]
    assert item["relevance"]["relevant"] is True   # offline-гейт = relevant
    assert "summary" not in item and "scoring" not in item  # ТОЛЬКО гейт


def test_process_recheck_payload_negative_keyword_blocks():
    result = external_ai.process_recheck_payload(_recheck_payload("Новый ГОСТ на каблук"))
    item = result["articles"][0]
    assert item["relevance"]["relevant"] is False
    assert item["relevance"]["model"] == "negative-keyword"
    assert result["stats"]["rejected"] == 1


def test_apply_recheck_result_deletes_irrelevant_and_keeps_relevant(monkeypatch):
    deleted, kept = [], []
    monkeypatch.setattr(external_ai.repository, "delete_article",
                        lambda article_id, force=False: deleted.append((article_id, force)) or True)
    monkeypatch.setattr(external_ai.repository, "set_article_relevance",
                        lambda *args: kept.append(args))
    monkeypatch.setattr(external_ai.repository, "insert_ai_run", lambda rec: None)

    stats = external_ai.apply_recheck_result({
        "recheck_relevance": True,
        "articles": [
            {"article_id": 1, "relevance": {"relevant": True, "reason": "ok", "model": "gpt"}, "errors": []},
            {"article_id": 2, "relevance": {"relevant": False, "reason": "war", "model": "gpt"}, "errors": []},
        ],
    })

    assert stats["kept"] == 1 and stats["deleted"] == 1
    assert deleted == [(2, False)]            # нерелевантная удалена, дайджест защищён
    assert kept[0][0] == 1                    # релевантная сохранена


def test_apply_recheck_result_skips_digest_member(monkeypatch):
    monkeypatch.setattr(external_ai.repository, "delete_article",
                        lambda article_id, force=False: False)  # в сохранённом дайджесте
    monkeypatch.setattr(external_ai.repository, "insert_ai_run", lambda rec: None)
    stats = external_ai.apply_recheck_result({
        "recheck_relevance": True,
        "articles": [{"article_id": 9, "relevance": {"relevant": False, "reason": "x", "model": "gpt"}, "errors": []}],
    })
    assert stats["deleted"] == 0 and stats["skipped_in_digest"] == 1


def test_apply_recheck_result_dry_run_deletes_nothing_and_previews(monkeypatch):
    calls = {"delete": 0, "relevance": 0}
    monkeypatch.setattr(external_ai.repository, "delete_article",
                        lambda *a, **k: calls.__setitem__("delete", calls["delete"] + 1) or True)
    monkeypatch.setattr(external_ai.repository, "set_article_relevance",
                        lambda *a: calls.__setitem__("relevance", calls["relevance"] + 1))
    monkeypatch.setattr(external_ai.repository, "insert_ai_run", lambda rec: None)
    monkeypatch.setattr(external_ai.repository, "get_articles_by_ids",
                        lambda ids: [{"id": 2, "title": "Война и БПЛА", "source_name": "Интерфакс ТЭК"}])

    stats = external_ai.apply_recheck_result({
        "recheck_relevance": True,
        "articles": [
            {"article_id": 1, "relevance": {"relevant": True, "reason": "ok", "model": "gpt"}, "errors": []},
            {"article_id": 2, "relevance": {"relevant": False, "reason": "боевые действия", "model": "gpt"}, "errors": []},
        ],
    }, dry_run=True)

    assert calls["delete"] == 0 and calls["relevance"] == 0       # dry-run ничего не трогает
    assert stats["kept"] == 1 and stats["deleted"] == 1           # deleted = «сколько бы срезали»
    assert stats["rejected_preview"][0]["title"] == "Война и БПЛА"
    assert stats["rejected_preview"][0]["reason"] == "боевые действия"
    assert stats["rejected_preview"][0]["source"] == "Интерфакс ТЭК"


def test_apply_recheck_result_mark_marks_instead_of_deleting(monkeypatch):
    calls = {"delete": 0, "mark": []}
    monkeypatch.setattr(external_ai.repository, "delete_article",
                        lambda *a, **k: calls.__setitem__("delete", calls["delete"] + 1) or True)
    monkeypatch.setattr(external_ai.repository, "set_article_relevance", lambda *a: None)
    monkeypatch.setattr(external_ai.repository, "insert_ai_run", lambda rec: None)
    monkeypatch.setattr(external_ai.repository, "mark_article_for_deletion",
                        lambda aid, reason, force=False: calls["mark"].append((aid, reason)) or "marked")

    stats = external_ai.apply_recheck_result({
        "recheck_relevance": True,
        "articles": [
            {"article_id": 1, "relevance": {"relevant": True, "reason": "ok", "model": "gpt"}, "errors": []},
            {"article_id": 2, "relevance": {"relevant": False, "reason": "война", "model": "gpt"}, "errors": []},
        ],
    }, mark=True)

    assert calls["delete"] == 0                     # mark-режим НЕ удаляет физически
    assert calls["mark"] == [(2, "война")]          # нерелевантная помечена
    assert stats["kept"] == 1 and stats["marked"] == 1 and stats["deleted"] == 0


# --- внешний бэкфилл перевода ------------------------------------------------

def test_process_translate_payload_translates_foreign_only():
    payload = {"offline": True, "articles": [
        {"id": 1, "title": "Halliburton wins contract", "language": "en", "source_name": "S"},
        {"id": 2, "title": "Газпром нефть запускает проект", "language": "ru", "source_name": "S"},
    ]}
    result = external_ai.process_translate_payload(payload)
    assert result["translate_titles"] is True
    assert result["stats"]["translated"] == 1   # AI только для иностранного
    by_id = {item["article_id"]: item for item in result["articles"]}
    assert by_id[1]["translation"]["title_ru"]
    assert by_id[2]["translation"]["title_ru"] == "Газпром нефть запускает проект"


def test_backfill_keeps_a_russian_title_as_is_even_off_the_rules(monkeypatch):
    """Бэкфилл перевода идёт по всей базе, а не по принятому: русский заголовок не по правилам — копией, без модели
    (правка — только у принятых публикаций, решение владельца 03.11)."""
    client = _EditClient("не должен прийти")
    monkeypatch.setattr(external_ai, "make_client", lambda offline: client)

    result = external_ai.process_translate_payload({"articles": [{"id": 3, **VESTI}]})

    assert result["articles"][0]["translation"]["title_ru"] == VESTI_TITLE
    assert client.calls == [] and result["stats"]["translated"] == 0


def test_apply_translate_result_sets_title_ru(monkeypatch):
    saved = []
    monkeypatch.setattr(external_ai.repository, "set_article_title_ru",
                        lambda article_id, title_ru: saved.append((article_id, title_ru)))
    monkeypatch.setattr(external_ai.repository, "insert_ai_run", lambda rec: None)
    stats = external_ai.apply_translate_result({
        "translate_titles": True,
        "articles": [{"article_id": 5, "translation": {"title_ru": "Заголовок", "model": "gpt", "provider": "openai"}, "errors": []}],
    })
    assert stats["translation"] == 1
    assert saved == [(5, "Заголовок")]


# --- локальный перепрогон (pipeline) ----------------------------------------

def test_recheck_relevance_articles_deletes_irrelevant(monkeypatch):
    monkeypatch.setattr(pipeline.repository, "list_enabled_tags", lambda: [])
    deleted, kept = [], []
    monkeypatch.setattr(pipeline.repository, "delete_article",
                        lambda article_id, force=False: deleted.append(article_id) or True)
    monkeypatch.setattr(pipeline.repository, "set_article_relevance", lambda *a: kept.append(a))
    monkeypatch.setattr(pipeline.repository, "insert_ai_run", lambda rec: None)

    irrelevant_client = _RecordingClient(relevant=False)
    stats = pipeline.recheck_relevance_articles(
        [{"id": 3, "title": "t", "raw_text": "x", "source_name": "S"}], irrelevant_client)
    assert stats["deleted"] == 1 and stats["kept"] == 0
    assert deleted == [3]
