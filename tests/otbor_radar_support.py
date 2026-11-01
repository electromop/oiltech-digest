"""Общее для тестов радара и материалов коллег через «Отбор» (#131, спека #128): прогон радара через настоящий клиент
воркера NL с подставным поиском GPT и страницами издателей, «Суть и перевод» сигналов на подставной модели. Шаг
«Отбора» и его подставная модель — tests/otbor_support.py. Сами тесты — test_otbor_radar.py (находки поиска) и
test_otbor_materials.py (материалы коллег).
"""

from oiltech_digest import background_jobs, cli, signal_discovery, signal_research, signal_summary
from oiltech_digest.processing.openai_client import AIResponse
from tests.signal_card_support import DRILLING, run_worker, tag

# Черновые данные модели поиска: в решения агентов они не идут (спека #128).
DRAFT_SUMMARY = "Сводка модели поиска: событие найдено поиском"
DRAFT_WHY = "Почему важно по версии модели поиска"
# Текст страницы издателя длиннее прежних 1 500 знаков: вход «Отбора» — до 6 000, как у статьи сбора.
PAGE_TEXT = " ".join(f"Абзац {number}: ADNOC Drilling will drill 40 wells for the Hail and Ghasha project with SLB "
                     f"automation, the operator said." for number in range(1, 41))
NO_PAGE = {"ok": False, "error": "http_403", "raw_text": "", "title": "", "published_at": None}


class SearchModel:
    """Подставной поиск GPT: события темы — заготовка; адреса из найденного — все события, если не сказано иначе.
    Вызовы запоминает."""

    def __init__(self, events, cited=None):
        self.events = events
        self.cited = cited if cited is not None else [item["source_url"] for item in events]
        self.calls = []

    def research_json(self, instructions, user_input, schema, **kwargs):
        self.calls.append({"instructions": instructions, "input": user_input, **kwargs})
        return AIResponse(data={"events": self.events, "_cited_urls": self.cited, "_web_search_calls": 3},
                          model="gpt-research", input_tokens=4000, output_tokens=800)


def event(title, url, **overrides):
    """Событие из ответа поиска GPT — с черновыми данными: тип, компания, суть, «почему важно»."""
    return {"title": title, "company": "ADNOC", "event_date": "2026-10-20", "summary": DRAFT_SUMMARY,
            "why_important": DRAFT_WHY, "source_url": url, "publisher": "ADNOC", "source_kind": "primary",
            "technology": "", "event_kind": "technology_contract", **overrides}


def page(title, text=PAGE_TEXT):
    """Страница издателя, которую докачал поиск."""
    return {"ok": True, "error": None, "raw_text": text, "title": title, "published_at": None}


class _DedupJudge:
    """Подставной судья «Дедупа»: любая пара — одно событие (same) или разные."""

    def __init__(self, same):
        self.same = same

    def complete_json(self, instructions, user_input, schema, **kwargs):
        return AIResponse(data={"same_event": self.same, "reason": "одно событие" if self.same else "разные"},
                          model="gpt-dedup", input_tokens=100, output_tokens=20)


def run_radar(monkeypatch, events, pages, *, cited=None, same_event=False):
    """Ежедневная задача радара (тема — тематика бурения) → воркер NL полосы агентов с подставным поиском и
    страницами → ядро. Судья, ревью пачки и проверка темы при «Отборе» не зовутся: их вызовы копятся в
    отдельном списке. Поиск и список вызовов прежней цепочки."""
    tag(DRILLING)
    search = SearchModel(events, cited)
    old_chain = []
    monkeypatch.setattr(signal_research, "make_client", lambda offline=False: search)
    monkeypatch.setattr(signal_discovery, "_fetch_full_text",
                        lambda url, fallback_title="", **kwargs: pages.get(url, NO_PAGE))
    monkeypatch.setattr(signal_discovery, "judge_signal_snapshot",
                        lambda *args, **kwargs: old_chain.append("судья") or ({}, {}))
    monkeypatch.setattr(signal_discovery, "_batch_review_candidates",
                        lambda *args, **kwargs: old_chain.append("ревью пачки") or {})
    monkeypatch.setattr(signal_discovery, "_second_opinion_theme",
                        lambda *args, **kwargs: old_chain.append("проверка темы"))
    monkeypatch.setattr(signal_discovery, "_search_web_evidence",
                        lambda *args, **kwargs: old_chain.append("Brave") or {"evidence": []})
    monkeypatch.setattr(signal_discovery, "_radar_client", lambda: _DedupJudge(same_event))
    background_jobs.enqueue_daily_signal_discovery(force=True)
    run_worker("external-agents")
    return search, old_chain


class SummaryModel:
    """Подставная модель «Суть и перевод»: по заголовку публикации — (русский заголовок, суть). Вызовы запоминает."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def complete_json(self, instructions, user_input, schema, max_output_tokens=900, model=None,
                      reasoning_effort=None):
        self.calls.append({"instructions": instructions, "input": user_input, "schema": schema["name"]})
        title = next(line.split(":", 1)[1].strip() for line in user_input.splitlines() if line.startswith("title:"))
        title_ru, summary = self.answers[title]
        data = {"summary": summary} if schema["name"] == "article_summary" else {"title_ru": title_ru}
        return AIResponse(data=data, model=f"{model or 'gpt-5-mini'}-2026", input_tokens=900, output_tokens=150)


def summarize(monkeypatch, answers):
    """Шаг планировщика «Суть и перевод» сигналов и воркер NL с подставной моделью; подставная модель."""
    model = SummaryModel(answers)
    monkeypatch.setattr(signal_summary, "make_client", lambda offline=False: model)
    cli.main(["enqueue-signal-summary"])
    run_worker("external-ai")
    return model
