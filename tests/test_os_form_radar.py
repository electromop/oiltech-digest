"""Контур ОС (#188, спека #187), шов 2: прогон радара через настоящий клиент воркера NL с подставным поиском и
«Суть и перевод» на подставной модели (tests/otbor_radar_support.py).

Старая память агента снята: словарь замен, который разбор «Рекомендаций AI-агенту» заводил из каждой стрелки «А → Б»,
больше не правит заголовки найденных страниц, и снимок памяти на внешний воркер не едет. Переводы терминов, которые
эксперт задавал намеренно, перешли в словарь «Сути и перевода»: перевод получает их во входе.
"""

from oiltech_digest import signal_discovery
from oiltech_digest.db import repository
from oiltech_digest.processing import domain_glossary
from tests.otbor_radar_support import event, page, run_radar, summarize
from tests.otbor_support import answer, decide
from tests.otbor_support import contour  # noqa: F401: фикстура
from tests.signal_card_support import api_get, user  # noqa: F401: фикстура

# Заголовок страницы, по которому прошлись бы правила замен «1 → 91,3%…» (из «58,1 → 91,3%», 10.11) и «patent →
# пилот» (из цепочки стадий «патент → пилот → …», 15.09).
PHASE_TITLE = "Phase 1 of the patent-protected drilling pilot starts on Nov. 1"
PHASE_URL = "https://operator.example/phase-1"


def _replacement_rule(term, replacement):
    """Правило замены, как его заводил разбор комментария до #188: строка словаря в памяти агента."""
    repository.upsert_signal_agent_memory(
        memory_key=f"signal_glossary:{term}", memory_type="signal_glossary", subject=term, score=85,
        facts={"preferred_ru": replacement, "source_terms": [term]},
    )


def test_radar_run_keeps_page_titles_and_takes_no_feedback_memory_to_the_worker(user, contour, monkeypatch):
    _replacement_rule("1", "91,3%, не рост добычи")
    _replacement_rule("patent-protected", "пилот")
    taken = []
    run = signal_discovery.process_external_payload
    monkeypatch.setattr(signal_discovery, "process_external_payload",
                        lambda payload, **kwargs: taken.append(payload) or run(payload, **kwargs))

    run_radar(monkeypatch, [event(PHASE_TITLE, PHASE_URL)], {PHASE_URL: page(PHASE_TITLE)})
    decide(monkeypatch, {PHASE_TITLE: answer(category="technology")})

    [payload] = taken
    assert not payload["snapshot"].get("memory")
    [card] = api_get("/api/signals", user)
    assert [(link["title"], link["title_ru"]) for link in card["evidence"]] == [(PHASE_TITLE, PHASE_TITLE)]


# 27 переводов терминов из правил замен (разбор 53 правил 12.11, замечания эксперта 13.09) с чисткой пяти огрехов
# разбора: пояснение внутри перевода, обрывок термина, хвост следующего пункта. Перевод: один термин, как у остальных
# строк словаря; второй вариант эксперта и сокращение лежат в пояснении (note): скобок, косой черты и «по контексту» в
# переводе нет, их бракует правило русского заголовка.
EXPERT_TERMS = {
    "within-stage adjustment": "корректировка параметров непосредственно в ходе стадии",
    "dynamic design optimization": "динамическая корректировка дизайна ГРП",
    "treatment execution": "выполнение обработки",
    "fleet execution": "управление исполнением операций флотом ГРП",
    "fracture outcomes": "результаты проведения ГРП",
    "subsurface feedback": "обратная связь по фактическому отклику пласта",
    "autonomous pumping": "автономное управление закачкой",
    "closed feedback loop": "замкнутый контур обратной связи",
    "closed-loop fracturing": "ГРП с замкнутым контуром управления",
    "narrow operating margin": "узкое окно допустимых давлений",
    "drill floor control": "управление оборудованием буровой площадки",
    "tripping": "спуско-подъёмные операции",
    "downhole hydraulics": "внутрискважинная гидравлика",
    "automated well placement": "автоматизированное управление проводкой скважины",
    "closed-loop rig automation": "автоматизация буровой с замкнутым контуром управления",
    "well delivery efficiency": "эффективность строительства скважин",
    "predictive maintenance": "предиктивное техническое обслуживание",
    "integrated data systems": "интегрированные системы сбора и анализа данных",
    "ai-enabled monitoring": "мониторинг с применением ИИ",
    "automated pipe handling": "автоматизированные спуско-подъёмные операции",
    "walking capability": "возможность перемещения между скважинами без демонтажа",
    "walking island rig": "шагающая буровая для искусственных островов",
    "well control": "контроль скважины",
    "full rig automation": "комплексная автоматизация буровой",
    "managed pressure drilling": "бурение с управляемым давлением",
    "closed-loop control": "управление с замкнутым контуром",
    "fully automated rig": "полностью автоматизированная буровая",
}

RIG_TITLE = "Operator automates tripping and well control on a North Sea rig"
RIG_URL = "https://operator.example/rig"
RIG_TEXT = " ".join(["The operator automated tripping on the rig, and the new well control system closed the loop."] * 20)


def test_expert_term_translations_are_in_the_dictionary_of_summary_and_translation():
    found = {source: term.preferred_ru for term in domain_glossary.GLOSSARY for source in term.source_terms}

    assert {source: found.get(source) for source in EXPERT_TERMS} == EXPERT_TERMS
    # Пояснения эксперта: запрет, оговорка, второй вариант и сокращение, а не текст перевода.
    notes = {source: term.note for term in domain_glossary.GLOSSARY for source in term.source_terms}
    [well_control] = [term for term in domain_glossary.GLOSSARY if "well control" in term.source_terms]
    assert well_control.forbidden_ru == ("управление скважиной",)
    assert "автономная буровая" in notes["fully automated rig"]
    assert "параметры проведения ГРП" in notes["fracture outcomes"]
    assert "замкнутый контур управления" in notes["closed-loop control"]
    assert "СПО" in notes["tripping"] and "MPD" in notes["managed pressure drilling"]
    assert domain_glossary.validate_glossary() == []


def test_translation_of_a_finding_gets_the_expert_terms_in_its_input(user, contour, monkeypatch):
    run_radar(monkeypatch, [event(RIG_TITLE, RIG_URL)], {RIG_URL: page(RIG_TITLE, RIG_TEXT)})
    decide(monkeypatch, {RIG_TITLE: answer(category="technology")})

    model = summarize(monkeypatch, {RIG_TITLE: ("Оператор автоматизировал СПО на буровой", "Суть")})

    [translation] = [call for call in model.calls if call["schema"] == "article_title_translation"]
    assert ("- source: tripping -> preferred_ru: спуско-подъёмные операции | note: Сокращение СПО"
            in translation["input"])
    assert ("- source: well control -> preferred_ru: контроль скважины | forbidden_ru: управление скважиной"
            in translation["input"])
