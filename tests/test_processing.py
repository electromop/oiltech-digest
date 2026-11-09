import os
from pathlib import Path
import subprocess
import sys

import pytest

from oiltech_digest import config
from oiltech_digest.processing import pipeline
from oiltech_digest.processing.domain_glossary import (
    enforce_glossary_text,
    glossary_golden_cases,
    glossary_prompt_block,
    mixed_script_words,
    normalize_scripts,
    relevant_glossary_terms,
    run_terminology_eval,
    terminology_warnings,
    validate_glossary,
)
from oiltech_digest.processing import digest
from oiltech_digest.processing import external_ai
from oiltech_digest.processing.openai_client import AIResponse, OfflineAIClient, _extract_output_text, output_budget
from oiltech_digest.processing.seed import DEFAULT_SCORING_PROFILES, _split_keywords, _tag_signal_enrichment


class _RecordingClient:
    """Фейк AI-клиент: пишет порядок вызовов по имени схемы + переданные model/effort/вход."""

    model = "fake"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900,
                      model=None, reasoning_effort=None):
        name = schema["name"]
        self.calls.append({
            "name": name,
            "model": model,
            "reasoning": reasoning_effort,
            "input": user_input,
            "max_output_tokens": max_output_tokens,
        })
        if name == "article_summary":
            return AIResponse(data={"summary": "s"}, model="fake")
        if name == "article_title_edit":
            return AIResponse(data={"title_ru": "Хуситы ударили по НПЗ в Эр-Рияде"}, model="fake")
        if name == "article_title_translation":
            return AIResponse(data={"title_ru": "Переведённый заголовок"}, model="fake")
        return AIResponse(data={}, model="fake")


def _external_payload(article_extra: dict | None = None) -> dict:
    """Пакет ядра для воркера: только статьи (тегов и критериев в пакете с 08.11 нет)."""
    article = {
        "id": 1,
        "title": "Война: удары по городу, есть жертвы",
        "url": "https://example.com/a",
        "language": "ru",
        "raw_text": "Военная сводка без отношения к нефтегазу.",
        "source_name": "Интерфакс ТЭК",
        "source_category": "Новости",
        "otbor_decision": "accept",
    }
    article.update(article_extra or {})
    return {"articles": [article]}


@pytest.mark.parametrize("decision", [
    pytest.param({"otbor_decision": "reject"}, id="otbor-rejected"),
    pytest.param({"otbor_decision": None, "relevant": False}, id="gate-rejected-before-otbor"),
])
@pytest.mark.parametrize("path", [
    pytest.param({}, id="scheduler-batch"),
    pytest.param({"article_ids": [1]}, id="explicit-list"),
    pytest.param({"only": ["summary"]}, id="only"),
])
def test_rejected_article_gets_no_summary_in_any_path(monkeypatch, decision, path):
    """Отклонённую статью («Отбором», без его решения гейтом до «Отбора») воркер не обрабатывает ни в каком пути:
    гейта, который мог передумать, с 08.11 нет, и платить за суть отклонённой незачем."""
    client = _RecordingClient()
    monkeypatch.setattr(external_ai, "make_client", lambda offline: client)

    result = external_ai.process_payload({**_external_payload(decision), **path})

    assert client.calls == []
    assert set(result["articles"][0]) == {"article_id", "errors"}
    assert result["stats"] == {"processed": 1, "summary": 0, "translated": 0, "errors": 0}


def test_scheduler_batch_calls_only_the_missing_stage(monkeypatch):
    """#53: пакет планировщика (без article_ids) проходит на NL только недостающие стадии, как
    локальный конвейер: статья с готовой сутью без перевода получает только перевод, не новую суть."""
    client = _RecordingClient()
    monkeypatch.setattr(external_ai, "make_client", lambda offline: client)
    payload = _external_payload({"summary": "готовая суть", "title_ru": None})

    result = external_ai.process_payload(payload)

    assert [c["name"] for c in client.calls] == []  # русский заголовок по правилам: без модели
    item = result["articles"][0]
    assert set(item) == {"article_id", "errors", "translation"}  # недостающее: только перевод
    assert item["translation"]["title_ru"] == payload["articles"][0]["title"]


def test_accepted_article_gets_its_russian_title_edited_by_the_rules(monkeypatch):
    """Решение владельца 03.11: у принятой статьи русский заголовок не по правилам эксперта (#150) правит «Суть и
    перевод»: тем же шагом, что переводит иноязычный; в итоге модель и токены, ядро учтёт расход."""
    client = _RecordingClient()
    monkeypatch.setattr(external_ai, "make_client", lambda offline: client)
    title = "Хуситы ударили по НПЗ в Эр-Рияде — Новости о нефти и газе в России и мире - Нефть и капитал"
    payload = _external_payload({"title": title, "summary": "готовая суть", "title_ru": None})

    item = external_ai.process_payload(payload)["articles"][0]

    assert [c["name"] for c in client.calls] == ["article_title_edit"]
    assert item["translation"]["title_ru"] == "Хуситы ударили по НПЗ в Эр-Рияде"
    assert item["translation"]["model"] == "fake" and item["translation"]["provider"] == "openai"


@pytest.mark.parametrize(
    ("extra", "fail_on", "calls", "written"),
    [
        # Принята «Отбором», ничего нет: суть, затем перевод иноязычного заголовка.
        pytest.param({"title": "Rig count rises", "language": "en"}, None,
                     ["article_summary", "article_title_translation"], {"summary", "translation"},
                     id="E1-accepted-nothing-yet"),
        # Принята гейтом до «Отбора» (история), суть есть: догоняет только перевод.
        pytest.param({"otbor_decision": None, "relevant": True, "summary": "готовая суть",
                      "title": "Rig count rises", "language": "en"}, None,
                     ["article_title_translation"], {"translation"}, id="E2-gate-accepted-only-translation"),
        # Всё есть: модель не зовётся.
        pytest.param({"summary": "готовая суть", "title_ru": "готовый заголовок"}, None, [], set(),
                     id="E3-nothing-missing"),
        # Упала суть: в этом проходе перевода нет, статья придёт в следующий пакет.
        pytest.param({"title": "Rig count rises", "language": "en"}, "article_summary", ["article_summary"], set(),
                     id="E4-summary-failed"),
    ],
)
def test_scheduler_batch_stage_edges(monkeypatch, extra, fail_on, calls, written):
    """Граничные случаи цикла стадий пакета планировщика (#53, ревью #79): страж цикла."""

    class _Client(_RecordingClient):
        def complete_json(self, instructions, user_input, schema, **kwargs):
            response = super().complete_json(instructions, user_input, schema, **kwargs)
            if schema["name"] == fail_on:
                raise RuntimeError("сбой модели")
            return response

    client = _Client()
    monkeypatch.setattr(external_ai, "make_client", lambda offline: client)

    item = external_ai.process_payload(_external_payload(extra))["articles"][0]

    assert [c["name"] for c in client.calls] == calls
    assert set(item) - {"article_id", "errors"} == written
    assert bool(item["errors"]) is (fail_on is not None)


def test_explicit_article_list_and_only_recompute_what_is_asked(monkeypatch):
    """Страж #53: явный список (перекачка тела, импорт статьи) пересчитывает суть и перевод, как раньше, а
    пометка only пересчитывает запрошенные стадии, даже у статьи с готовыми сутью и переводом."""
    client = _RecordingClient()
    monkeypatch.setattr(external_ai, "make_client", lambda offline: client)
    done = {"summary": "готовая суть", "title_ru": "готовый заголовок", "title": "Rig count rises", "language": "en"}

    external_ai.process_payload({**_external_payload(done), "article_ids": [1]})
    assert [c["name"] for c in client.calls] == ["article_summary", "article_title_translation"]

    client.calls.clear()
    external_ai.process_payload({**_external_payload(done), "only": ["summary"]})
    assert [c["name"] for c in client.calls] == ["article_summary"]


def test_worker_payload_needs_no_tags_or_criteria(monkeypatch):
    """Пакет без тегов и критериев (тег и балл статьи сняты 08.11) воркер исполняет: раньше он падал до первой
    статьи («No tags supplied»), и такой пакет сборке контракта 10 ядро не выдаёт (contract.required_contract)."""
    from oiltech_digest import contract

    client = _RecordingClient()
    monkeypatch.setattr(external_ai, "make_client", lambda offline: client)

    result = external_ai.process_payload(_external_payload())

    assert result["stats"]["summary"] == 1
    assert contract.required_contract("process_articles", {"articles": []}) == 11


def test_reasoning_effort_across_gpt5_generations():
    from oiltech_digest.processing.openai_client import _reasoning_effort

    # Исходный GPT-5: minimal валиден, none → minimal.
    assert _reasoning_effort("gpt-5-mini", "minimal") == "minimal"
    assert _reasoning_effort("gpt-5-mini", "none") == "minimal"
    assert _reasoning_effort("gpt-5-nano", "") == "minimal"
    # 5.1+ (включая 5.4/5.5 с датой): minimal невалиден → none; пустое → none.
    assert _reasoning_effort("gpt-5.5-2026-04-23", "minimal") == "none"
    assert _reasoning_effort("gpt-5.5", "") == "none"
    assert _reasoning_effort("gpt-5.4-mini", "") == "none"
    assert _reasoning_effort("gpt-5.1-codex", "minimal") == "none"
    # Явные low/medium/high проходят как есть на любом поколении.
    assert _reasoning_effort("gpt-5.5", "medium") == "medium"
    assert _reasoning_effort("gpt-5-mini", "low") == "low"


def test_extract_output_text_from_responses_shape():
    raw = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": '{"summary":"ok"}'}],
            }
        ]
    }
    assert _extract_output_text(raw) == '{"summary":"ok"}'


def test_ai_response_cost_uses_configured_rates():
    response = AIResponse(data={}, model="m", input_tokens=1_000_000, output_tokens=1_000_000)
    assert response.cost_usd > 0


def test_split_keywords_semicolon_and_newline():
    assert _split_keywords("ГРП; бурение\nцементирование") == ["ГРП", "бурение", "цементирование"]


def test_signal_enrichment_adds_chinese_hse_keywords():
    extra = _tag_signal_enrichment("Экология, промышленная безопасность, HSE и устойчивое развитие")

    assert "predictive HSE" in extra["keywords_en"]
    assert "电子作业票" in extra["keywords_cn"]
    assert "防碰撞系统" in extra["keywords_cn"]


def test_default_scoring_weights_equal_100_in_each_profile():
    from oiltech_digest.scoring_profiles import SCORING_PROFILES

    assert set(DEFAULT_SCORING_PROFILES) == set(SCORING_PROFILES)
    for criteria in DEFAULT_SCORING_PROFILES.values():
        assert sum(item["weight"] for item in criteria) == 100
        assert len({item["name"] for item in criteria}) == len(criteria)


def test_glossary_prompt_selects_relevant_oilfield_terms():
    article = {
        "title": "Electric frac fleet expands hydraulic fracturing operations",
        "raw_text": "The company uses electric frac units and proppant for hydraulic fracturing.",
    }

    block = glossary_prompt_block(article)

    assert "hydraulic fracturing" in block
    assert "preferred_ru: ГРП" in block
    assert "forbidden_ru: фракинг" in block
    assert "proppant" in block


def test_summary_enforces_oilfield_preferred_terms():
    class BadTranslatorClient:
        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
            assert "preferred_ru: ГРП" in user_input
            return AIResponse(
                data={"summary": "Компания расширила фракинг и закупила новые флоты."},
                model="fake",
            )

    article = {
        "title": "Electric frac fleet expands hydraulic fracturing operations",
        "raw_text": "Electric frac fleet expands hydraulic fracturing operations in oilfields.",
    }

    response = pipeline.summarize_article(article, BadTranslatorClient())

    assert "ГРП" in response.data["summary"]
    assert "фракинг" not in response.data["summary"].lower()
    assert terminology_warnings(response.data["summary"], article) == []


def test_glossary_enforces_inflected_bad_terms():
    class BadTranslatorClient:
        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
            return AIResponse(
                data={"summary": "После фракинга на оффшоре компания провела флоубэка анализ и ворковеров программу."},
                model="fake",
            )

    article = {
        "title": "Offshore fracking flowback and workover program",
        "raw_text": "Offshore hydraulic fracturing generated flowback and required workover operations.",
    }

    response = pipeline.summarize_article(article, BadTranslatorClient())
    summary = response.data["summary"].lower()

    assert "грп" in summary
    assert "на шельфе" in summary
    assert "анализ жидкости обратного притока" in summary
    assert "крс" in summary
    assert "фракинг" not in summary
    assert "оффшор" not in summary
    assert "флоубэк" not in summary
    assert "ворковер" not in summary


def test_glossary_catches_stimulation_and_only_flags_reservoir():
    """«Резервуар» без автозамены (решение владельца 25.09): в русском тексте это почти
    всегда ёмкость, а замена не держала падеж («с качественным пласт»). Модели: подсказка,
    аудиту: находка, если в исходнике reservoir и нет ёмкостей."""

    class BadTranslatorClient:
        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
            assert "preferred_ru: пласт" in user_input
            assert "резервуар — только ёмкость" in user_input
            assert "preferred_ru: интенсификация притока" in user_input
            return AIResponse(
                data={"summary": "Оператор провёл стимуляцию скважины для резервуара."},
                model="fake",
            )

    article = {
        "title": "Well stimulation improves reservoir output",
        "raw_text": "Well stimulation improved reservoir output in the producing formation.",
    }

    response = pipeline.summarize_article(article, BadTranslatorClient())

    assert response.data["summary"] == "Оператор провёл интенсификацию притока для резервуара."
    assert [w["forbidden_ru"] for w in terminology_warnings(response.data["summary"], article)] == [r"\bрезервуар\w*"]


def test_glossary_keeps_storage_tanks_as_reservoirs():
    lng = {"title": "Argent LNG selects CB&I", "raw_text": "Three full containment LNG storage tanks; gas from the reservoir."}
    refinery = {"title": "Пожар на нефтебазе", "raw_text": "Горят резервуары с топливом.", "language": "ru"}

    text = "CB&I станет подрядчиком EPC для трёх резервуаров полного ограждения."

    assert enforce_glossary_text(text, lng) == text
    assert terminology_warnings(text, lng) == []
    assert terminology_warnings("Горят резервуары с топливом.", refinery) == []


def test_normalize_scripts_fixes_twins_and_glue_but_keeps_brands():
    fixed = {
        "Карачаганакa": "Карачаганака",
        "вхoдит в 1-гo": "входит в 1-го",
        "Cарыарка": "Сарыарка",
        "подписала МoU, ОPEX и СCS": "подписала MoU, OPEX и CCS",
        "MГП": "МГП",
        "присутствиеHoneywell вPermian": "присутствие Honeywell в Permian",
        "СШАChina": "США China",
        "в Казаниhttps://example.ru": "в Казани https://example.ru",
    }
    for bad, good in fixed.items():
        assert normalize_scripts(bad) == good, bad
    # Бренды, единицы и настоящий полуперевод не трогаем. «Орinoco»: кириллическая «р» здесь:
    # звук «r», замена по виду дала бы «Opinoco».
    for kept in ("КазМунайГаз", "кВт", "ExxonMobil", "LNG-проект", "CO2", "Türkiye", "Орinoco", "электроэнergyю"):
        assert normalize_scripts(kept) == kept, kept
    assert mixed_script_words(normalize_scripts("Орinoco и электроэнergyю")) == ["Орinoco", "электроэнergyю"]


def test_normalize_scripts_splits_glue_before_fixing_twins():
    """Ревью 25.09: двойники раньше склейки делали «сExxonMobil» → «cExxonMobil»: стык пропадал,
    а с ним находка аудита и отбор на перегенерацию."""
    for glued, split in {
        "совместно сExxonMobil": "совместно с ExxonMobil",
        "а также аBP": "а также а BP",
        "торги наMOEX": "торги на MOEX",
        "проект оCCS": "проект о CCS",
        "кредит МТСBank": "кредит МТС Bank",
        "вхoдитHoneywell": "входит Honeywell",
    }.items():
        assert normalize_scripts(glued) == split, glued


def test_latin_twins_in_russian_words_are_fixed_by_look():
    # Латиница в русском слове, буква, взятая по виду: 9 из 12 таких замен на проде дали
    # известное слово («выводy», «Министp»). Обратно (кириллица в латинском): только вид+звук.
    for bad, good in {"Pоснефть": "Роснефть", "yправление": "управление", "Hовак": "Новак",
                      "выводy": "выводу", "Министp": "Министр", "ПAO": "ПАО", "Cпрос": "Спрос"}.items():
        assert normalize_scripts(bad) == good, bad


def test_allowed_mixed_script_brands_are_left_alone():
    # В другом падеже тоже: бренд узнаётся по началу до стыка алфавитов.
    for brand in ("PROНЕФТЬ", "PROНефть", "Farш", "PROНЕФТИ", "Dostaевского"):
        assert normalize_scripts(f"журнал {brand} пишет") == f"журнал {brand} пишет"
        assert mixed_script_words(brand) == []
    # Бренд узнаётся по слову без окончания, а не по первым буквам: «Dостаевский» ≠ «Dоставка».
    assert mixed_script_words("Dосрочно и Dоставка") == ["Dосрочно", "Dоставка"]


def test_single_twin_inside_a_word_is_fixed_not_cut():
    """Ревью 27.09: склейка раньше двойников резала слово по одиночной чужой букве:
    «КазMунайГаз» → «Каз МунайГаз», и детектор слово больше не видел."""
    for bad, good in {
        "КазMунайГаз": "КазМунайГаз",
        "ExxonМobil": "ExxonMobil",
        "ИнтерPАО": "ИнтерРАО",
        "мощность 5 MВт": "мощность 5 МВт",
        "4,9 kВт": "4,9 кВт",
        "4,9 кBт": "4,9 кВт",
    }.items():
        assert normalize_scripts(bad) == good, bad


def test_single_latin_letter_word_is_cut_from_a_long_russian_word():
    """Проверка правок 27.09: одна латинская буква перед русским словом: слово («B2B»,
    «Plan B»), если справа длинное слово; перед коротким: двойник в единице («MПа»)."""
    for glued, split in {
        "B2BПлатформа": "B2B Платформа",
        "E&PКомпании": "E&P Компании",
        "Plan BКомпания": "Plan B Компания",
        "планBКомпания": "план B Компания",
        "5GПрезидент": "5G Президент",
        "давление 20 MПа": "давление 20 МПа",
        "выработка 5 MВтч": "выработка 5 МВтч",
    }.items():
        assert normalize_scripts(glued) == split, glued


def test_latin_abbreviation_with_russian_ending_is_not_turned_cyrillic():
    """Ревью 27.09: P, H, B по виду делали из аббревиатуры кириллицу: «BOPы» → «ВОРы».
    Такое слово остаётся смешанным и уходит на повтор, а не молча портится. Слово капсом:
    двойники, по большинству: «СЕКТOP» не уходит в латиницу, «HOBATЭК» чинится."""
    for kept in ("BOPы", "EPCконтракт", "OPECстраны"):
        assert normalize_scripts(kept) == kept, kept
        assert mixed_script_words(kept) == [kept]
    assert normalize_scripts("ПAO и ОPEX") == "ПАО и OPEX"
    for bad, good in {"СЕКТOP": "СЕКТОР", "МАСТEP": "МАСТЕР", "ТОМСКHEФТЬ": "ТОМСКНЕФТЬ",
                      "HOBATЭК": "НОВАТЭК", "POCHEФТЬ": "РОСНЕФТЬ"}.items():
        assert normalize_scripts(bad) == good, bad


def test_replacements_see_every_article_term_not_only_the_prompt_dozen():
    """Проверка правок 27.09: словарь брал 12 первых терминов статьи (длина подсказки модели),
    а offshore в нём 16-й: в длинной статье замены «оффшор» не срабатывали."""
    article = {
        "title": "Offshore program",
        "raw_text": "completion, workover, spudded, ESP, drilling mud, MPD, MWD, LWD, coiled tubing, "
                    "subsea tieback, flowback, produced water, offshore",
    }

    assert enforce_glossary_text("Оффшорная установка работает на оффшоре.", article) == "Шельфовая установка работает на шельфе."


def test_completion_repair_needs_the_term_and_keeps_adjectives():
    """Ревью 27.09: «скважин[а-я]*» ловил прилагательное, а находка аудита включала замену
    в любой статье: «завершение скважины» в русской заметке не калька."""
    completion = {"title": "Well completion", "raw_text": "After well completion the field started."}
    lng = {"title": "LNG plant", "raw_text": "LNG capacity grows."}

    assert enforce_glossary_text("После завершения скважинных испытаний завершения скважин ждут.", completion) == (
        "После завершения скважинных испытаний заканчивания скважин ждут."
    )
    assert enforce_glossary_text("После завершения скважины началась добыча.", lng) == "После завершения скважины началась добыча."


def test_offshore_repairs_only_in_offshore_articles_and_never_for_tax_havens():
    """Ревью 27.09: замены «оффшор» были общими: «зарегистрирован в оффшоре на Кипре» в
    статье про СПГ становилось «в шельфе». Существительное многозначно: только аудит."""
    lng = {"title": "LNG plant", "raw_text": "LNG capacity grows."}
    offshore = {"title": "Offshore drilling", "raw_text": "Offshore drilling expands."}
    tax = "Компания зарегистрирована в оффшоре на Кипре, танкеры принадлежат оффшорным компаниям."

    assert enforce_glossary_text(tax, lng) == tax
    assert enforce_glossary_text(tax, offshore) == tax
    assert [w["forbidden_ru"] for w in terminology_warnings(tax, offshore)] == [r"\bоффшор\w*"]
    assert enforce_glossary_text("Оффшорная платформа работает на оффшоре.", offshore) == "Шельфовая платформа работает на шельфе."


def test_preposition_before_the_repaired_vowel_word():
    stimulation = {"title": "Well stimulation", "raw_text": "Well stimulation improved output."}

    assert enforce_glossary_text("Отчёт о стимуляции скважин. О стимуляции скважины сообщили.", stimulation) == (
        "Отчёт об интенсификации притока. Об интенсификации притока сообщили."
    )


def test_phrase_repairs_keep_case_and_capital():
    """Ревью 25.09: замены ставили именительный падеж и строчную букву."""
    context = {"title": "Offshore completion", "raw_text": "offshore well completion flowback stimulation LNG capacity"}
    for bad, good in {
        "После завершения скважины началась добыча.": "После заканчивания скважины началась добыча.",
        "Оффшорная платформа работает на оффшоре.": "Шельфовая платформа работает на шельфе.",
        "Компания провела стимуляцию скважины.": "Компания провела интенсификацию притока.",
        "Итоги. Провел испытания.": "Итоги. Провёл испытания.",
        "Измерили объём флоубэка.": "Измерили объём жидкости обратного притока.",
        "Компания работает с мощностями LNG.": "Компания работает с мощностями СПГ.",
    }.items():
        assert enforce_glossary_text(bad, context) == good, bad


def test_rop_is_flagged_not_replaced():
    context = {"title": "ROP record", "raw_text": "The rate of penetration (ROP) doubled."}

    text = "Рост ROP составил 20%."

    assert enforce_glossary_text(text, context) == text  # именительный «механическая скорость» сломал бы падеж
    assert [w["forbidden_ru"] for w in terminology_warnings(text, context)] == [r"\bROP\b"]


def test_reservoir_warning_survives_ccs_storage_and_plural():
    """Ревью 25.09: слово «storage» есть в любой статье про CCS и глушило находку."""
    ccs = {"title": "CCS", "raw_text": "Carbon capture and storage: CO2 is injected into depleted reservoirs."}

    warnings = terminology_warnings("CO2 закачают в истощённый резервуар.", ccs)

    assert [w["forbidden_ru"] for w in warnings] == [r"\bрезервуар\w*"]


def test_summary_is_asked_again_when_a_word_mixes_scripts():
    class HalfTranslatingClient:
        def __init__(self):
            self.prompts = []

        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
            self.prompts.append(user_input)
            text = "Цены на электроэнergyю выросли." if len(self.prompts) == 1 else "Цены на электроэнергию выросли."
            return AIResponse(data={"summary": text}, model="gpt-5-mini", input_tokens=100, output_tokens=10)

    client = HalfTranslatingClient()
    response = pipeline.summarize_article({"title": "Power prices", "raw_text": "Power prices rose."}, client)

    assert response.data["summary"] == "Цены на электроэнергию выросли."
    assert len(client.prompts) == 2
    assert "«электроэнergyю»" in client.prompts[1]
    # Расход обоих вызовов: в одном итоге, иначе ai_processing_runs недосчитает.
    assert (response.input_tokens, response.output_tokens) == (200, 20)


def test_failed_retry_keeps_the_paid_first_answer():
    """Ревью 25.09: сбой повтора (429, таймаут) ронял и первый, уже оплаченный ответ."""

    class Client:
        calls = 0

        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
            Client.calls += 1
            if Client.calls == 2:
                raise RuntimeError("429 Too Many Requests")
            return AIResponse(data={"summary": "Цены на электроэнergyю выросли."}, model="fake", input_tokens=7, output_tokens=3)

    response = pipeline.summarize_article({"title": "t", "raw_text": "x"}, Client())

    assert response.data["summary"] == "Цены на электроэнergyю выросли."
    assert (response.input_tokens, response.output_tokens) == (7, 3)


def test_summary_is_not_asked_again_when_clean_or_retry_is_no_better():
    class Client:
        def __init__(self, answers):
            self.answers = list(answers)
            self.calls = 0

        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
            self.calls += 1
            return AIResponse(data={"summary": self.answers.pop(0)}, model="fake", input_tokens=5, output_tokens=5)

    clean = Client(["Добыча выросла; вхoдит в топ."])
    assert pipeline.summarize_article({"title": "t", "raw_text": "x"}, clean).data["summary"] == "Добыча выросла; входит в топ."
    assert clean.calls == 1  # двойник словарь чинит сам: переспрашивать незачем

    stubborn = Client(["Пермian растёт.", "Пермian и наshore растут."])
    response = pipeline.summarize_article({"title": "t", "raw_text": "x"}, stubborn)
    assert response.data["summary"] == "Пермian растёт."  # повтор хуже: берём первый
    assert stubborn.calls == 2


def test_glossary_golden_cases_pass():
    for case in glossary_golden_cases():
        fixed = enforce_glossary_text(str(case["bad"]), case["article"])
        fixed_lower = fixed.lower()
        missing = [term for term in case["must_have"] if term.lower() not in fixed_lower]
        forbidden = [term for term in case["must_not"] if term.lower() in fixed_lower]

        assert not missing, f"{case['name']}: {fixed}; missing={missing}"
        assert not forbidden, f"{case['name']}: {fixed}; forbidden={forbidden}"


@pytest.mark.parametrize("text, preferred", [
    ("The field switched three wells to gas lift.", "газлифт"),
    ("The operator replaced the ESP after a short run life.", "ЭЦН"),
    ("An electric submersible pump was installed.", "ЭЦН"),
    ("The artificial lift program covers 300 wells.", "механизированная добыча"),
])
def test_gas_lift_and_esp_are_not_generalised_to_artificial_lift(text, preferred):
    """#150: эксперт пишет «газлифт» и «ЭЦН»; до правки gas lift и ESP стояли в строке artificial lift, и подсказка
    модели обобщала их до «механизированной добычи»."""
    terms = relevant_glossary_terms({"title": "Field update", "raw_text": text})

    assert [term.preferred_ru for term in terms] == [preferred]


def test_coiled_tubing_is_gnkt_not_koltyubing():
    """#150: эксперт пишет «ГНКТ», колтюбинга в его выпуске нет ни разу; до правки словарь подсказывал колтюбинг."""
    block = glossary_prompt_block({"title": "Coiled tubing intervention",
                                   "raw_text": "The coiled tubing unit cleaned out the well."})

    assert "preferred_ru: ГНКТ | full_ru: гибкая насосно-компрессорная труба" in block
    assert "колтюбинг" in block.split("forbidden_ru:", 1)[1]


@pytest.mark.parametrize("text, preferred", [
    ("Inspectors scanned the welds with an eddy current array probe.", "вихретоковый контроль матричными датчиками"),
    ("H2S levels at the wellhead fell.", "сероводород"),
    ("Crews talk over push-to-talk radios.", "голосовая связь"),
    ("A lithium-free battery powers the site.", "безлитиевый"),
    ("Permian Basin output rose.", "Пермский бассейн"),
    ("AI agents tune the setpoints.", "ИИ"),
    ("The sorbent is a metal-organic framework.", "металлорганические каркасы"),
])
def test_glossary_puts_the_russian_term_first(text, preferred):
    """#150: в выпуске эксперта русский термин, а сокращение или латиница в заголовке и сути не нужны."""
    block = glossary_prompt_block({"title": "Update", "raw_text": text})

    assert f"preferred_ru: {preferred}" in block


@pytest.mark.parametrize("text", [
    "The Ministry of Finance (MoF) cut the export duty.",
    "Tankers in the Emission Control Area (ECA) burn low-sulphur fuel.",
    "PTT Exploration and Production signed the contract.",
])
def test_ambiguous_abbreviations_do_not_pull_the_new_terms(text):
    """#150: MOF, ECA и PTT ключами словаря не взяты это ещё Министерство финансов, зона контроля выбросов и тайская
    PTT. Полное название (metal-organic framework, eddy current array, push-to-talk) в публикации о них и так есть."""
    assert relevant_glossary_terms({"title": "Update", "raw_text": text}) == []


@pytest.mark.parametrize("source, bad, good", [
    ("A lithium-free battery powers the site.",
     "Литий‑свободные накопители, литий–свободная химия, литий-свободных модулей.",
     "Безлитиевые накопители, безлитиевая химия, безлитиевых модулей."),
    ("A lithium-free battery powers the site.", "Электролит оставляет литий свободным для реакции.",
     "Электролит оставляет литий свободным для реакции."),
    ("The coiled tubing unit cleaned out the well.", "Колтюбинги простаивали, об этом писал журнал «Время колтюбинга».",
     "ГНКТ простаивали, об этом писал журнал «Время колтюбинга»."),
    ("The field switched three wells to gas lift.", "Газовые лифты заменили, газовых лифтов стало меньше.",
     "Газлифты заменили, газлифтов стало меньше."),
    ("The electric submersible pump (ESP) run life doubled.", "Погружной электрический насос дешевле.", "ЭЦН дешевле."),
])
def test_glossary_repairs_plural_word_order_and_leave_lookalikes(source, bad, good):
    """#150, ревью: множественное число и обратный порядок слов чинятся; «литий свободным» без дефиса: не калька, а
    название журнала «Время колтюбинга» не термин."""
    assert enforce_glossary_text(bad, {"title": "Update", "raw_text": source}) == good


@pytest.mark.parametrize("source, text", [
    ("H2S levels at the wellhead fell.", "Уровень H2S снизился."),
    ("Inspectors scanned the welds with an eddy current array probe.", "ECA-контроль ускорился."),
    ("Crews talk over push-to-talk radios.", "Связь PTT работает."),
    ("The sorbent is a metal-organic framework.", "Сорбент из MOF."),
])
def test_glossary_audit_flags_abbreviations_left_in_russian_text(source, text):
    """#150, ревью: «сокращение в заголовок и суть не переносить» видит аудит, как латиницу ROP и granular; замены
    нет."""
    article = {"title": "Update", "raw_text": source}

    assert terminology_warnings(text, article)
    assert enforce_glossary_text(text, article) == text


def test_glossary_audit_leaves_an_eca_of_another_meaning_alone():
    article = {"title": "Shipping", "raw_text": "Tankers in the Emission Control Area (ECA) burn low-sulphur fuel."}

    assert terminology_warnings("Танкеры в зоне ECA перешли на малосернистое топливо.", article) == []


def test_glossary_repairs_customer_spud_and_granular_remarks():
    """Замечания заказчика 22.09 по карточке радара: «спудрил вертикальную скважину», «granular».

    Радар прогоняет свой текст через enforce_glossary_text этого модуля, наши карточки:
    тоже; аудит до правки словаря таких мест не видел вовсе (0 из 1000 статей).
    """
    article = {
        "title": "Global Land Drilling Rigs Tracker",
        "raw_text": (
            "Silver City Drilling spudded the horizontal appraisal well Venus-2H. "
            "NDC 9 spudded the vertical well T-200. The tracker adds more granular field data."
        ),
        "language": "en",
    }
    radar = (
        "в Австралии Silver City Drilling спудрил горизонтальную оценочную скважину Venus-2H; "
        "в Египте NDC 9 спудрил вертикальную скважину T-200. "
        "Отчёт дополняет обзор более granularными полевыми данными."
    )

    assert len(terminology_warnings(radar, article)) >= 2
    fixed = enforce_glossary_text(radar, article)

    assert "Silver City Drilling забурил горизонтальную оценочную скважину Venus-2H" in fixed
    assert "NDC 9 забурил вертикальную скважину T-200" in fixed
    assert "более детальными полевыми данными" in fixed
    assert terminology_warnings(fixed, article) == []


def test_glossary_repairs_calque_without_english_term_in_context():
    """Ревью F: у доказательства радара может не быть «spudded»: только обзор Westwood.

    Калька из warn_patterns в самом тексте сама включает свой термин, иначе «спудрил»
    пережил бы и словарь радара, и аудит (relevant = []).
    """
    article = {"title": "Отчёт Westwood", "raw_text": "Westwood's onshore team provides a global update on rigs."}

    text = "NDC 9 спудрил вертикальную скважину T-200, данные более granularные"

    assert terminology_warnings(text, article)
    assert enforce_glossary_text(text, article) == "NDC 9 забурил вертикальную скважину T-200, данные более детальные"


def test_glossary_repairs_spud_in_without_breaking_the_hyphen():
    article = {"title": "Well spud", "raw_text": "The spud-in of the well took place on Monday."}

    fixed = enforce_glossary_text("Дата спуд-ина — понедельник.", article)

    assert fixed == "Дата забуривания — понедельник."


def test_glossary_keeps_russian_idiom_pod_spudom():
    article = {"title": "Well spudded", "raw_text": "The well was spudded in April.", "language": "en"}

    text = "Оператор держал под спудом данные о новой скважине."

    assert enforce_glossary_text(text, article) == text
    assert terminology_warnings(text, article) == []


def test_glossary_prompt_tells_model_how_to_render_spud():
    block = glossary_prompt_block({"title": "Operator spuds a well", "raw_text": "The well was spudded in April."})

    assert "preferred_ru: забуривание" in block
    assert "спудинг" in block


def test_terminology_warnings_flag_mixed_script_words():
    """Латиница вперемешку с кириллицей в одном слове: брак без всякого словаря."""
    article = {"title": "Brazil exploration", "raw_text": "The Tupinamba exploration well", "language": "en"}

    words = [item["forbidden_ru"] for item in terminology_warnings("бурение по Тупinамba и нефтесервиc", article)]

    assert words == ["Тупinамba", "нефтесервиc"]
    assert terminology_warnings("СПГ-проект, LNG-проект, CO2 и Türkiye", article) == []


def test_domain_glossary_definition_is_valid():
    assert validate_glossary() == []


def test_terminology_eval_passes_100_examples():
    report = run_terminology_eval(limit=100)

    assert report["total"] == 100
    assert report["failed"] == 0
    assert report["passed"] == 100


def test_terminology_eval_without_a_limit_passes_on_every_case():
    """Золотые примеры идут первыми, и с #150 часть случаев по запретным словам в первые 100 не попадает: без
    предела (так по умолчанию и у eval-terminology) оценка берёт все."""
    report = run_terminology_eval(limit=None)

    assert report["total"] > 100
    assert report["failed"] == 0


def test_title_translation_enforces_completion_and_workover_terms():
    class BadTitleClient:
        def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
            assert "preferred_ru: заканчивание скважины" in user_input
            assert "preferred_ru: КРС" in user_input
            return AIResponse(
                data={"title_ru": "Завершение скважины и ворковер увеличили добычу"},
                model="fake",
            )

    article = {
        "title": "Well completion and workover improve production",
        "raw_text": "The article describes well completion and workover operations.",
        "language": "en",
    }

    title_ru, response = pipeline.title_ru_for_article(article, BadTitleClient())

    assert response is not None
    assert "заканчивание скважины" in title_ru.lower()
    assert "КРС" in title_ru
    assert "ворковер" not in title_ru.lower()


def _one_line(text: str) -> str:
    return " ".join(text.split())


def test_summary_prompt_follows_the_expert_issue():
    """#150: суть по образцу итогового выпуска эксперта за октябрь 2026: событие с главным числом, что оно меняет
    для нефтесервиса, стадия и чьи данные, не длиннее 330 знаков. Первый абзац заменён, прежние пункты остались.
    Правка по проверке на эталоне (владелец 28.10): длина ещё и словами, «что впереди» вместо «чего ещё нет»: модель
    читала его как «чего нет в публикации» и писала «масштаб не указан»."""
    from oiltech_digest.processing.prompts import SUMMARY_INSTRUCTIONS

    text = _one_line(SUMMARY_INSTRUCTIONS)

    assert text.startswith("Ты отраслевой аналитик нефтесервиса. Составь суть публикации для эксперта нефтесервисной "
                           "компании: 2-3 коротких предложения по-русски, всего не длиннее 330 знаков (около 45 слов), "
                           "без маркетинга. Длиннее нельзя: выбери главное, остальное опусти. ")
    for rule in (
        "- Первое предложение: событие. Кто что сделал или делает, где и в каком масштабе (одно главное число из "
        "текста). "
        "Начинай с участника события или с технологии, а не с издания и не с даты публикации.",
        "- Второе: как это устроено или что меняет для нефтесервиса: какие работы, операции или заказчики затронуты.",
        "- Третье, если нужно: стадия. План, цель и прогноз пиши как план («планирует», «намечено»); результат, "
        "который назвала сама компания, со ссылкой на неё («по данным компании», «результаты представлены авторами "
        "компании»); что у самого проекта ещё впереди (испытания, поставки, результаты), назови одной фразой.",
        "- Пиши о событии, а не о публикации: не перечисляй, чего в ней нет («заказчик не назван», «масштаб не "
        "указан», «в статье не указано»), и не опровергай того, что в ней не утверждается («не является рекордом "
        "отрасли»).",
        "- Не обращайся к читателю и не советуй ему, что делать.",
        "- Внутренние номера установок, докладов и партий не пиши, если это не название продукта.",
        "- НЕ притягивай связь с нефтегазом, если её в статье нет; НЕ добавляй фактов, которых там нет.",
        "- Если во входе есть блок domain_glossary, он имеет приоритет над твоим обычным переводом.",
        "- Числа — только те, что есть в статье, и только значимые (суммы, объёмы, даты).",
        "- Если во входе есть category, oilfield_relevance и oilfield_application",
    ):
        assert rule in text, rule
    assert "Отрази, что произошло" not in text and "чего ещё нет" not in text


def test_title_prompt_names_the_event_without_dashes_or_brackets():
    """#150: заголовок как в выпуске эксперта: кто что сделал, до 90 знаков, без тире, скобок, внутренних номеров и
    названия издания. Длина в инструкции одна: прежние «до 200 символов» спорили бы с 90."""
    from oiltech_digest.processing.prompts import TRANSLATE_INSTRUCTIONS

    text = _one_line(TRANSLATE_INSTRUCTIONS)

    for rule in (
        "- Заголовок называет событие, а не публикацию: кто что сделал. Не длиннее 90 знаков (около 12 слов): длинные "
        "названия компаний и продуктов сокращай до узнаваемой части, подробности оставь для сути.",
        "- Без тире между частями заголовка и без скобок: вместо тире двоеточие («Предмет: событие») или другой "
        "порядок слов.",
        "- Без внутренних номеров установок и докладов и без названия издания, если оно не автор события.",
        "- Если во входе есть блок domain_glossary, он имеет приоритет над обычным переводом.",
    ):
        assert rule in text, rule
    assert "200 символов" not in text


class _StageClient:
    """Фейк-клиент: модель, уровень рассуждения и лимит ответа каждого вызова."""

    model = "fake"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None):
        self.calls.append({"name": schema["name"], "model": model, "reasoning": reasoning_effort,
                           "max_output_tokens": max_output_tokens})
        data = {"article_summary": {"summary": "Суть статьи."},
                "article_title_translation": {"title_ru": "Новости бурения"}}[schema["name"]]
        return AIResponse(data=data, model=model or self.model)


def test_summary_uses_its_own_model_reasoning_and_budget(monkeypatch):
    """#150: «Суть и перевод»: на своей модели (владелец 28.10: gpt-6.1-terra, low по проверке на эталоне), а не на
    общей OPENAI_MODEL тегов и судьи перепечаток; лимит ответа с запасом под рассуждение. Перенесено из ветки моста с
    gpt-5 (feat/explicit-stage-models) дословно: та же переменная и тот же запас."""
    monkeypatch.setattr(config, "OPENAI_SUMMARY_MODEL", "summary-model")
    monkeypatch.setattr(config, "OPENAI_SUMMARY_REASONING", "low")
    client = _StageClient()

    pipeline.summarize_article({"title": "Drilling", "raw_text": "Новый буровой станок."}, client)

    (call,) = client.calls
    assert (call["model"], call["reasoning"]) == ("summary-model", "low")
    assert call["max_output_tokens"] == output_budget(1200, "low")


def test_translation_budget_follows_its_reasoning(monkeypatch):
    """На low перевод не уместил бы рассуждение в прежние 300 токенов ответа."""
    monkeypatch.setattr(config, "OPENAI_TRANSLATE_MODEL", "translate-model")
    monkeypatch.setattr(config, "OPENAI_TRANSLATE_REASONING", "low")
    client = _StageClient()

    pipeline.translate_article({"title": "New drilling rig tested"}, client)

    assert client.calls[-1]["max_output_tokens"] == output_budget(300, "low")


def test_minimal_keeps_todays_summary_and_translation_limits(monkeypatch):
    """На нынешнем уровне (minimal) лимиты прежние: до новых строк в .env воркера поведение не меняется."""
    for name in ("OPENAI_SUMMARY_REASONING", "OPENAI_TRANSLATE_REASONING"):
        monkeypatch.setattr(config, name, "minimal")
    client = _StageClient()
    article = {"title": "New rig", "raw_text": "x"}

    pipeline.summarize_article(article, client)
    pipeline.translate_article(article, client)

    assert [call["max_output_tokens"] for call in client.calls] == [1200, 300]


def test_summary_model_falls_back_to_the_main_model():
    """Пусто, откат на OPENAI_MODEL / OPENAI_REASONING_EFFORT: без новых строк в .env воркер работает как раньше."""
    # Пустые, а не удалённые: config при импорте читает .env, и строка оттуда подменила бы отсутствующую переменную.
    env = {**os.environ, "OPENAI_MODEL": "main-model", "OPENAI_REASONING_EFFORT": "low", "OPENAI_SUMMARY_MODEL": "",
           "OPENAI_SUMMARY_REASONING": ""}
    probe = "from oiltech_digest import config as c; print(c.OPENAI_SUMMARY_MODEL, c.OPENAI_SUMMARY_REASONING)"

    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60,
                            cwd=Path(__file__).resolve().parents[1], env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["main-model", "low"]


def test_offline_summary_is_deterministic():
    client = OfflineAIClient()
    response = client.complete_json(
        "x",
        "title: Test title\ntext: First sentence. Second sentence. Third.",
        {"name": "article_summary"},
    )
    assert "Test title" in response.data["summary"]


def test_offline_pipeline_outputs_digest_ready_content(monkeypatch):
    """Местный конвейер ленты: принятой «Отбором» статье суть и перевод заголовка, отклонённой гейтом до «Отбора»
    (история) ничего; тег и балл статьи не считаются (сняты 08.11, тему и баллы ставит «Карточка»)."""
    article = {
        "id": 501,
        "title": "Electric frac fleet reduces diesel consumption",
        "source_name": "World Oil",
        "url": "https://example.com/electric-frac",
        "language": "en",
        "published_at": None,
        "raw_text": (
            "New electric frac technology improves hydraulic fracturing operations. "
            "The fleet reduces diesel consumption and lowers emissions for oilfield service crews."
        ),
        "text_truncated": False,
        "otbor_decision": "accept",
    }
    rejected = {**article, "id": 502, "otbor_decision": None, "relevant": False}
    state = {"runs": []}

    monkeypatch.setattr(
        pipeline.repository,
        "upsert_article_card",
        lambda article_id, summary, model=None, title_ru=None: state.update(
            {"article_id": article_id, "summary": summary, "summary_model": model, "title_ru": title_ru}
        ),
    )
    monkeypatch.setattr(
        pipeline.repository,
        "set_article_title_ru",
        lambda article_id, title_ru: state.update({"title_ru": title_ru}),
    )
    monkeypatch.setattr(pipeline.repository, "insert_ai_run", lambda rec: state["runs"].append(rec))

    stats = pipeline.process_pipeline_articles([article, rejected], OfflineAIClient(), fetch_full=False)

    assert stats == {"processed": 2, "fulltext": 0, "summary": 1, "translated": 1, "rejected": 1, "errors": 0}
    assert state["article_id"] == 501
    assert state["summary"].startswith(article["title"])
    assert state["title_ru"]  # иностранный заголовок переведён отдельной стадией
    # Перевод заголовка: отдельная стадия после сути; отклонённой статье ни одного вызова.
    assert [(run["article_id"], run["stage"]) for run in state["runs"]] == [(501, "summary"), (501, "translation")]
    assert all(run["provider"] == "offline" and run["status"] == "ok" for run in state["runs"])

    class PublishedAt:
        def date(self):
            return self

        def isoformat(self):
            return "2026-06-07"

    monkeypatch.setattr(
        digest.repository,
        "digest_candidates",
        lambda month=None, limit=20, min_score=60, **kwargs: [
            {
                "id": article["id"],
                "title": article["title"],
                "source_name": article["source_name"],
                "url": article["url"],
                "published_at": PublishedAt(),
                "tag_name": "ГРП",
                "parent_tag_name": "Технологии",
                "total_score": 72.0,
                "score_label": "Выше средней",
                "summary": state["summary"],
                "image_url": "",
            }
        ],
    )

    # Плашка «Технология / Бизнес» (23.10) ищет вид элемента в базе: тест без базы отвечает «вид неизвестен».
    monkeypatch.setattr(digest.repository, "digest_item_kinds", lambda article_ids, signal_ids: ({}, {}))
    # Поля материала PDF (#161) и имена критериев раздела тоже из базы: без неё полей нет.
    monkeypatch.setattr(digest.repository, "issue_material_fields", lambda article_ids, signal_ids: ({}, {}))
    # «Главное в выпуске», написанное человеком (#162), тоже из базы.
    monkeypatch.setattr("oiltech_digest.db.issue_edits_store.highlights", lambda month: {})
    monkeypatch.setattr(digest.repository, "list_enabled_scoring_criteria", lambda profile: [])

    content = digest.build_digest_content("2026-06", min_score=65)

    assert content["news"][0]["article_id"] == 501
    assert content["news"][0]["kind"] is None
    assert content["news"][0]["category"] == "Технологии / ГРП"
    assert content["news"][0]["score"] == 72.0
    assert content["news"][0]["summary"]
    assert not content["news"][0]["summary"].startswith(article["title"] + ":")


def test_ai_response_cost_is_per_model():
    """cost_usd должен считаться по прайсу КОНКРЕТНОЙ модели (тех-долг T4),
    а не единой ставкой nano для всех."""
    tok = dict(input_tokens=1_000_000, output_tokens=1_000_000)
    flagship = AIResponse(data={}, model="gpt-5.5-2026-04-23", **tok)
    mini = AIResponse(data={}, model="gpt-5-mini-2025-08-07", **tok)
    nano = AIResponse(data={}, model="gpt-5-nano-2025-08-07", **tok)

    # Порядок стоимости отражает реальность: флагман дороже mini дороже nano.
    assert flagship.cost_usd > mini.cost_usd > nano.cost_usd
    # gpt-5.5 = (5 + 30) USD за 1М+1М токенов; nano = (0.05 + 0.40).
    assert abs(flagship.cost_usd - 35.0) < 1e-9
    assert abs(nano.cost_usd - 0.45) < 1e-9


def test_ai_response_cost_unknown_model_falls_back():
    """Неизвестная модель → откат на дефолтные ставки config (env-независимо), а не падение."""
    from oiltech_digest import config

    response = AIResponse(data={}, model="some-future-model", input_tokens=1_000_000, output_tokens=1_000_000)
    expected = config.OPENAI_INPUT_USD_PER_MTOK + config.OPENAI_OUTPUT_USD_PER_MTOK
    assert abs(response.cost_usd - expected) < 1e-9


def test_external_refetch_rejects_substituted_body(monkeypatch):
    """Страж принадлежности обязателен и на внешнем пути: воркер отдаёт то, что выдал
    сайт, а сайт умеет отдавать пейвол или листинг на любой адрес (задача №24)."""
    from oiltech_digest.ingestion import external_fetch

    stored: list = []
    monkeypatch.setattr(external_fetch.repository, "get_article",
                        lambda aid: {"id": aid, "title": "Совсем про другое", "source_id": 1})
    monkeypatch.setattr(external_fetch.repository, "update_article_full_text",
                        lambda *a, **k: stored.append((a, k)))
    monkeypatch.setattr(
        "oiltech_digest.ingestion.article_fetcher._ownership_rejection",
        lambda article, title, text: "title does not match body",
    )

    out = external_fetch.apply_refetch_text_result(
        {"kind": "refetch_text", "results": [{"id": 5, "status": "ok", "text": "чужой текст " * 50}]}
    )
    assert out["mismatched"] == 1
    assert out["applied"] == 0


def test_reprint_primary_falls_back_to_longer_copy():
    """Модель вернула чужой id: берём копию длиннее, а не первую попавшуюся.

    В случае заказчика от 08.09 копии были 1026, 2327, 2366 и 3181 знак, и
    короткая оказалась обрывком с дефектом склейки заголовка.
    """
    from oiltech_digest.processing import reprints

    left = {"id": 10, "raw_text": "к" * 1026}
    right = {"id": 20, "raw_text": "д" * 3181}
    assert reprints._resolve_primary(999, left, right) == 20
    assert reprints._resolve_primary(None, left, right) == 20
    assert reprints._resolve_primary(10, left, right) == 10, "валидный id модели уважаем"


def test_reprint_review_dry_run_writes_nothing(monkeypatch):
    """Сухой прогон ничего не помечает: схлопывание убирает материал из ленты."""
    from oiltech_digest.processing import reprints

    written: list = []
    monkeypatch.setattr(reprints.repository, "get_article",
                        lambda aid: {"id": aid, "title": "t", "raw_text": "текст" * 50})
    monkeypatch.setattr(reprints.repository, "mark_article_reprint",
                        lambda **kw: written.append(kw))

    class _Client:
        model = "test-model"

        def complete_json(self, *a, **k):
            from oiltech_digest.processing.openai_client import AIResponse
            return AIResponse(data={"same_event": True, "primary_id": 1, "reason": "одно испытание"},
                              model="test-model")

    out = reprints.review_candidates(
        [{"a_id": 1, "b_id": 2, "a_title": "A", "b_title": "B", "overlap": 0.5}],
        _Client(), dry_run=True)
    assert out["stats"]["reprints"] == 1
    assert written == [], "сухой прогон записал пометку"


def test_reprint_apply_rejects_foreign_primary_id():
    """Модель вернула id не из пары: дублем пометили бы не ту статью.

    Падаем на длину: короткая копия обычно и есть обрывок (в случае заказчика
    копия на 1026 знаков была склейкой заголовка с лидом).
    """
    from oiltech_digest.processing import external_ai

    written: list = []
    import oiltech_digest.db.repository as repo
    orig = repo.mark_article_reprint
    repo.mark_article_reprint = lambda **kw: written.append(kw)
    try:
        external_ai.apply_reprint_review_result({
            "reprint_review": True,
            "verdicts": [{"a_id": 10, "b_id": 20, "same_event": True,
                          "primary_id": 999, "a_len": 1026, "b_len": 3181,
                          "reason": "одно испытание", "overlap": 0.5}],
        })
    finally:
        repo.mark_article_reprint = orig
    assert len(written) == 1
    assert written[0]["primary_id"] == 20, "главной должна стать длинная копия"
    assert written[0]["article_id"] == 10


def test_reprint_candidates_band_is_passed_through(monkeypatch):
    """Полосу задаёт вызывающий, а не запрос.

    Замер на живом корпусе 17.09: из 318 пар за 14 дней тривиальных (100%)
    только 57, а полоса случая заказчика (35-49%): 132 пары. Пока выборка
    резалась по `ORDER BY overlap DESC`, спорные пары до модели не доезжали,
    и судью нечем было проверить на том, ради чего он заведён.
    """
    from oiltech_digest.db import repository
    from oiltech_digest.processing import reprints

    seen = {}
    monkeypatch.setattr(repository, "reprint_candidates",
                        lambda **kw: seen.update(kw) or [])

    reprints.find_candidates(days=14, min_overlap=0.35, max_overlap=0.6, limit=40)

    assert seen["min_overlap"] == 0.35
    assert seen["max_overlap"] == 0.6, "верхняя граница полосы должна доезжать до запроса"


def test_reprint_candidate_order_is_not_by_overlap():
    """Порядок выборки не должен коррелировать с силой совпадения."""
    import inspect
    import re
    from oiltech_digest.db import repository

    # Комментарии выкидываем: объяснение, ПОЧЕМУ так нельзя, само содержит
    # запрещённую строку: на этом тест и споткнулся в первой редакции.
    sql = inspect.getsource(repository.reprint_candidates)
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    orders = re.findall(r"ORDER BY .*", code)

    assert orders, "в запросе должен быть явный порядок"
    assert not any("overlap" in o for o in orders), (
        f"срез по overlap отдаёт модели только тривиальные пары: {orders}"
    )
    assert any(o.startswith("ORDER BY md5(") for o in orders)


def test_find_reprints_waits_for_interval_since_last_applied_run(isolated_db, monkeypatch, capsys):
    """Срок, от прошлого прогона с записью в базе: перезапуск планировщика его не
    сбивает, а сухой прогон не считается."""
    import argparse

    from oiltech_digest import cli
    from oiltech_digest.db import repository
    from oiltech_digest.processing import reprints

    looked = []
    monkeypatch.setattr(reprints, "find_candidates", lambda **kwargs: looked.append(kwargs) or [])
    args = argparse.Namespace(min_overlap=0.35, max_overlap=1.0, days=7, limit=200, max_days_apart=5, show=0,
                              candidates_only=False, offline=False, apply=True, local=False, min_interval_hours=12)

    repository.create_background_job("reprint_review", {"pairs": [], "dry_run": True}, queue_name="external-ai")
    cli.cmd_find_reprints(args)
    assert len(looked) == 1  # сухой прогон не в счёт

    job = repository.create_background_job("reprint_review", {"pairs": [], "dry_run": False}, queue_name="external-ai")
    cli.cmd_find_reprints(args)
    assert len(looked) == 1 and "пропуск" in capsys.readouterr().out

    with repository.get_connection() as conn:
        conn.execute("UPDATE background_jobs SET created_at = now() - interval '13 hours' WHERE id = %s", (job["id"],))
        conn.commit()
    cli.cmd_find_reprints(args)
    assert len(looked) == 2  # прошло больше 12 часов: снова ищем
