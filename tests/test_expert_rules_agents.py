"""Правила эксперта (#190, спека #187, ADR 0007), шов 2: задачи шести агентов через настоящий клиент воркера NL с
подставной моделью.

У каждого агента во входе блок «Правила эксперта, версия N» только своего шага и категории: «Отбор» и «Дедуп» видят все
свои правила с пометкой категории (категорию решает сам «Отбор», у находки её ещё нет), поиск радара: подсказки только
своей темы. Версия набора пишется в рецепт решения («Отбор», «Карточка», баллы), где рецепта нет, в запись расхода
вызова («Суть и перевод», «Дедуп», поиск). Без правил инструкция прежняя, и задача годится любой сборке; с правилами
сборке контракта 12 ядро задачу не выдаёт.
"""

from __future__ import annotations

from oiltech_digest import background_jobs, cli, contract, otbor, signal_discovery, signal_research
from oiltech_digest.db import connection, expert_rules_store, repository
from oiltech_digest.otbor_pack_format import load_dictionaries
from oiltech_digest.processing import external_ai
from tests.card_pipeline_support import (DIGITAL, DRILLING, card_answer, even, fill_cards,  # noqa: F401
                                         pipeline, run_scores, scores)
from tests.expert_rules_support import RecordingModel, approved
from tests.otbor_radar_support import SearchModel, event, page
from tests.otbor_support import PUBLISHED, OtborModel, answer, article, decide
from tests.otbor_support import contour  # noqa: F401: фикстура
from tests.signal_card_support import run_worker, tag, user, worker  # noqa: F401: фикстуры

TITLE = "Weatherford wins drilling contract for 274 wells in Oman"
TEXT = "Weatherford won a contract to drill 274 wells in Oman. The contract was signed on 24 September 2026."
HINTS = "«PoC» не промышленное внедрение."


def _rules(text: str) -> list[str]:
    """Строки правил блока «Правила эксперта» в инструкции: «[шаг] текст»."""
    head, _, tail = text.partition("Правила эксперта, версия ")
    return [line.split(". ", 1)[1] for line in tail.splitlines()[1:] if line[:1].isdigit()] if tail else []


def _version(text: str) -> int | None:
    _, _, tail = text.partition("Правила эксперта, версия ")
    return int(tail.split(".", 1)[0]) if tail else None


def _recipe(column: str, article_id: int) -> dict:
    with connection.get_connection() as conn:
        return conn.execute(f"SELECT {column} FROM signals WHERE origin = 'feed' AND origin_article_id = %s",
                            (article_id,)).fetchone()[0]


def _run_recipes(stage_like: str) -> list[tuple[str, dict | None]]:
    with connection.get_connection() as conn:
        return conn.execute("SELECT stage, recipe FROM ai_processing_runs WHERE stage LIKE %s ORDER BY id",
                            (stage_like,)).fetchall()


def test_otbor_summary_card_and_scores_get_rules_of_their_step_and_category_and_keep_the_version(
        user, pipeline, monkeypatch):
    approved("otbor", "decision", "Отчёт оператора о программе бурения брать.")
    approved("otbor", "category", "Контракт сервисной компании относится к бизнесу.", category="business")
    approved("summary", "summary", "Суть начинай с заказчика работ.")
    approved("summary", "title", "В заголовке называй заказчика.")
    approved("summary", "terms", "Переводить well control как контроль скважины.")
    approved("summary", "summary", "Суть технологии: что она меняет в операции.", category="technology")
    approved("card", "maturity", HINTS)
    approved("card", "theme", "Обзор рынка одной технологии относится к теме рынка.", category="technology")
    approved("scores", "criteria", "Сумма договора не эффект для заказчика.", category="business")
    approved("scores", "criteria", "Рекорд компании не новизна для отрасли.", category="technology")

    article_id = article(TITLE, TEXT, language="en", published=PUBLISHED)
    chosen = decide(monkeypatch, {TITLE: answer(industry="oilfield_services")})
    [otbor_call] = chosen.calls
    assert _version(otbor_call["instructions"]) == 2
    assert _rules(otbor_call["instructions"]) == [
        "[решение и причина] Отчёт оператора о программе бурения брать.",
        "[категория; только для категории «бизнес»] Контракт сервисной компании относится к бизнесу."]
    assert _recipe("otbor_recipe", article_id)["expert_rules_version"] == 2

    feed = RecordingModel({"article_summary": {"summary": "Weatherford получила контракт на 274 скважины в Омане."},
                           "article_title_translation": {"title_ru": "Weatherford получила контракт в Омане"}})
    monkeypatch.setattr(external_ai, "make_client", lambda offline=False: feed)
    cli.main(["enqueue-process", "--limit", "10"])
    run_worker("external-ai")
    [summary_call] = feed.instructions("article_summary")
    assert _rules(summary_call) == ["[суть] Суть начинай с заказчика работ.",
                                    "[термины] Переводить well control как контроль скважины."]
    [title_call] = feed.instructions("article_title_translation")
    assert _rules(title_call) == ["[заголовок] В заголовке называй заказчика.",
                                  "[термины] Переводить well control как контроль скважины."]
    assert _version(summary_call) == _version(title_call) == 4
    assert {(stage, (recipe or {}).get("expert_rules_version")) for stage, recipe in _run_recipes("%")
            if stage in ("summary", "translation")} == {("summary", 4), ("translation", 4)}

    card = fill_cards(monkeypatch, {TITLE: card_answer()})
    [card_call] = card.calls
    assert _rules(card_call["instructions"]) == [f"[стадия] {HINTS}"]
    assert _recipe("card_recipe", article_id)["expert_rules_version"] == 2

    scored = run_scores(monkeypatch, {TITLE: scores(even("business", 70))})
    [scores_call] = scored.calls
    assert _rules(scores_call["instructions"]) == [
        "[по критериям; только для категории «бизнес»] Сумма договора не эффект для заказчика."]
    assert _recipe("scores_recipe", article_id)["expert_rules_version"] == 2


class _Judge(RecordingModel):
    def __init__(self):
        super().__init__({"signal_duplicate_verdict": {"same_event": False, "reason": "разные события"}},
                         model="gpt-dedup")


def test_radar_run_gets_search_hints_of_its_topic_and_dedup_rules_and_keeps_the_versions(user, contour, monkeypatch):
    tag(DRILLING)
    tag(DIGITAL)
    approved("search", "topics", "интеллектуальное заканчивание; бурение на обсадной колонне", topic=DRILLING)
    approved("search", "topics", "цифровой двойник", topic=DIGITAL)
    approved("dedup", "same_event", "Пилот и промышленный запуск: разные события.")
    approved("dedup", "same_event", "Два контракта одного оператора: разные события.", category="business")

    first, second = "https://operator.example/a", "https://operator.example/b"
    search = SearchModel([event("ADNOC Drilling will drill 40 wells for Hail and Ghasha", first),
                          event("ADNOC Drilling to drill 40 wells for Hail and Ghasha project", second)])
    monkeypatch.setattr(signal_research, "make_client", lambda offline=False: search)
    monkeypatch.setattr(signal_discovery, "_fetch_full_text",
                        lambda url, fallback_title="", **kwargs: page("ADNOC Drilling will drill 40 wells"))
    judge = _Judge()
    monkeypatch.setattr(signal_discovery, "_radar_client", lambda: judge)
    background_jobs.enqueue_daily_signal_discovery(force=True)
    run_worker("external-agents")

    prompts = {call["input"].splitlines()[0].removeprefix("Тема: "): call["input"] for call in search.calls}
    assert set(prompts) == {DRILLING, DIGITAL}
    assert "ищи также материалы про: интеллектуальное заканчивание; бурение на обсадной колонне." in prompts[DRILLING]
    assert "цифровой двойник" not in prompts[DRILLING]
    assert "ищи также материалы про: цифровой двойник." in prompts[DIGITAL]
    assert "заканчивание" not in prompts[DIGITAL]
    judged = judge.instructions("signal_duplicate_verdict")
    assert judged and {tuple(_rules(one)) for one in judged} == {(
        "[одно ли событие] Пилот и промышленный запуск: разные события.",
        "[одно ли событие; только для категории «бизнес»] Два контракта одного оператора: разные события.")}
    recipes = {stage: (recipe or {}).get("expert_rules_version") for stage, recipe in _run_recipes("radar\\_%")}
    assert recipes == {"radar_research": 2, "radar_dedup": 2}


def test_reprint_judge_gets_dedup_rules_and_keeps_the_version_in_usage(user, contour, monkeypatch):
    approved("dedup", "same_event", "Пилот и промышленный запуск: разные события.")
    payload = {"pairs": [{"overlap": 0.5,
                          "a": {"id": 1, "title": "A", "source_name": "A", "published_at": "", "raw_text": "a"},
                          "b": {"id": 2, "title": "B", "source_name": "B", "published_at": "", "raw_text": "b"}}],
               "expert_rules": expert_rules_store.payload_block("dedup")}
    judge = RecordingModel({"article_reprint_verdict": {"same_event": False, "primary_id": 1, "reason": "разные"}},
                           model="test-reprint-model")
    monkeypatch.setattr(external_ai, "make_client", lambda *args, **kwargs: judge)
    result = external_ai.process_reprint_review_payload(payload)
    assert _rules(judge.calls[0]["instructions"]) == ["[одно ли событие] Пилот и промышленный запуск: разные события."]
    external_ai.apply_reprint_review_result(result, job_id=77)
    assert [(recipe or {}).get("expert_rules_version") for _, recipe in _run_recipes("reprint_review")] == [1]


def test_without_rules_instructions_stay_as_they_were_and_old_builds_get_jobs_until_a_rule_is_approved(
        user, contour, monkeypatch):
    dictionaries = load_dictionaries()
    assert otbor.instructions_with_rules(dictionaries, {"agent": "otbor", "version": 0, "rules": []}) == \
        otbor._instructions(dictionaries)
    first = article("Программа бурения Equinor", "Equinor начнёт бурение 12 скважин на шельфе Норвегии в 2027 году.")
    model = OtborModel({"Программа бурения Equinor": answer()})
    monkeypatch.setattr(otbor, "make_client", lambda: model)
    cli.main(["enqueue-otbor"])
    old = worker("external-ai", contract_number=12)
    job = old.claim()
    assert job is not None and job["payload"]["expert_rules"]["rules"] == []
    assert "Правила эксперта" not in otbor.instructions_with_rules(dictionaries, job["payload"]["expert_rules"])

    approved("otbor", "decision", "Отчёт оператора о программе бурения брать.")
    article("Программа бурения Aker BP", "Aker BP начнёт бурение 8 скважин на шельфе Норвегии в 2027 году.")
    cli.main(["enqueue-otbor"])
    assert worker("external-ai", contract_number=12).claim() is None
    failed = [one for one in repository.list_background_jobs(limit=20) if one["kind"] == "otbor"
              and one["status"] == "failed"]
    assert failed and "нужен контракт 13" in failed[0]["error_message"]
    assert contract.CONTRACT >= 13
    assert first
