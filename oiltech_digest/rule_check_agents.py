"""Агенты в автопроверке правил эксперта (#190, спека #187): вход строки эталона (ядро, при выдаче), вызов агента с
блоком правил (воркер, без базы) и ответ агента как значение для сверки с эталоном (ядро, при записи).

Вызов тот же, что в задаче агента: та же инструкция с правилами и тот же вход (otbor.instructions_with_rules,
signal_card.instructions_with_rules, signal_scores.instructions, pipeline.summarize_article и title_ru_for_article,
signal_dedup.instructions, signal_research.research_topic); ответ проходит ту же проверку кодом ядра (otbor_answer.outcome,
signal_card.card_values, signal_scores.score_values): мерится то, что дошло бы до сигнала. Модели: у «Отбора»,
«Карточки», баллов и поиска из настроек ядра (едут в задаче), у «Сути и перевода» и «Дедупа» из .env воркера, как в их
задачах.
"""

from __future__ import annotations

from typing import Any

from oiltech_digest import config, expert_rules
from oiltech_digest.processing.openai_client import output_budget

_CARD_TOKENS, _SCORES_TOKENS, _DEDUP_TOKENS = 1200, 900, 700


class CheckSetupError(ValueError):
    """Проверку нельзя выдать: не задана модель агента или нет входа."""


# --- Ядро, при выдаче: общее и вход строк --------------------------------------------------------


def shared(agent: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Общее для всех строк проверки: справочники, тематики, критерии профилей, модель и уровень."""
    if agent == "otbor":
        from oiltech_digest.otbor_pack_format import load_dictionaries

        if not config.OTBOR_MODEL:
            raise CheckSetupError("OTBOR_MODEL не задан: проверить правило «Отбора» нечем")
        return {"dictionaries": load_dictionaries(), "model": config.OTBOR_MODEL, "reasoning": config.OTBOR_REASONING}
    if agent == "card":
        from oiltech_digest import signal_themes
        from oiltech_digest.db import repository
        from oiltech_digest.signal_card_pack import load_dictionaries

        if not config.SIGNAL_CARD_MODEL:
            raise CheckSetupError("SIGNAL_CARD_MODEL не задан: проверить правило «Карточки» нечем")
        return {"dictionaries": load_dictionaries(), "themes": signal_themes.names(repository.list_enabled_tags()),
                "model": config.SIGNAL_CARD_MODEL, "reasoning": config.SIGNAL_CARD_REASONING}
    if agent == "scores":
        from oiltech_digest import signal_scores
        from oiltech_digest.db import repository
        from oiltech_digest.db.signal_scores_store import PROFILES

        if not config.SIGNAL_SCORES_MODEL:
            raise CheckSetupError("SIGNAL_SCORES_MODEL не задан: проверить правило баллов нечем")
        rubrics = signal_scores.load_rubrics()
        profiles = {profile: signal_scores.model_criteria(repository.list_enabled_scoring_criteria(profile), rubrics,
                                                          profile) for profile in PROFILES.values()}
        return {"profiles": profiles, "rubrics_version": rubrics["version"], "model": config.SIGNAL_SCORES_MODEL,
                "reasoning": config.SIGNAL_SCORES_REASONING}
    if agent == "search":
        from oiltech_digest import signal_discovery

        settings = signal_discovery.research_settings(days=int(spec.get("days") or 7))
        return {"topic": spec.get("topic"), "days": int(spec.get("days") or 7), "model": settings["research_model"],
                "reasoning": settings["research_reasoning"], "limit": settings["research_limit"]}
    return {}


def inputs(agent: str, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Вход агента по строкам эталона: ключ строки → вход, как в задаче агента. Строки без входа (сигнал ушёл из
    пути агента) не проверяются."""
    if agent == "otbor":
        return _otbor_inputs(rows)
    if agent == "card":
        return _card_inputs(rows)
    if agent == "scores":
        return _scores_inputs(rows)
    if agent == "summary":
        return _summary_inputs(rows)
    if agent == "dedup":
        return _dedup_inputs(rows)
    return {row["key"]: {} for row in rows}


def _otbor_inputs(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    from oiltech_digest import otbor
    from oiltech_digest.db import otbor_inputs

    refs = {row["ref"] for row in rows}
    articles = [int(ref.split(":")[1]) for ref in refs if ref.startswith("article:")]
    signals = [int(ref.split(":")[1]) for ref in refs if ref.startswith("signal:")]
    items = {f"article:{int(one['article_id'])}": otbor._item(one) for one in otbor_inputs.article_inputs(articles)}
    items |= {f"signal:{int(one['signal_id'])}": otbor._signal_item(one) for one in otbor_inputs.signal_inputs(signals)}
    return {row["key"]: items[row["ref"]] for row in rows if row["ref"] in items}


def _card_inputs(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    from oiltech_digest import signal_card
    from oiltech_digest.db import repository, signal_card_store

    tags = repository.list_enabled_tags()
    found = {int(one["signal_id"]): signal_card.payload_signal(one, tags)
             for one in signal_card_store.card_inputs([row["signal_id"] for row in rows])}
    return {row["key"]: found[row["signal_id"]] for row in rows if row["signal_id"] in found}


def _scores_inputs(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    from oiltech_digest import signal_scores
    from oiltech_digest.db import signal_scores_store

    found = {int(one["signal_id"]): signal_scores.payload_item(signal_scores.context(one))
             for one in signal_scores_store.score_inputs([row["signal_id"] for row in rows])}
    return {row["key"]: found[row["signal_id"]] for row in rows if row["signal_id"] in found}


def _summary_inputs(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Публикация сигнала, как её видит «Суть и перевод» (signal_summary._item): главная публикация «Карточки» с
    полями «Отбора»; у материала заголовок не правится."""
    from psycopg.rows import dict_row

    from oiltech_digest.db import connection, signal_card_store
    from oiltech_digest.db.repository import SIGNAL_ORIGIN_MATERIAL
    from oiltech_digest.signal_card_pack import publication_day, publication_text

    ids = sorted({row["signal_id"] for row in rows})
    publications = {int(one["signal_id"]): one for one in signal_card_store.card_inputs(ids)}
    with connection.get_connection() as conn:
        fields = {int(one["id"]): one for one in conn.cursor(row_factory=dict_row).execute(
            "SELECT s.id, s.origin, s.signal_category, s.oilfield_relevance, s.oilfield_application, a.language"
            " FROM signals s LEFT JOIN articles a ON a.id = s.origin_article_id WHERE s.id = ANY(%s)",
            (ids,)).fetchall()}
    found = {}
    for signal_id, publication in publications.items():
        own = fields.get(signal_id) or {}
        found[signal_id] = {
            "signal_id": signal_id, "edit_title": own.get("origin") != SIGNAL_ORIGIN_MATERIAL,
            "title": publication_text(publication.get("title"))[:500], "source_name": publication.get("publisher") or "",
            "url": "", "language": own.get("language") or "", "published_at": publication_day(
                publication.get("published")) or "", "summary": None, "raw_text": publication_text(publication.get("text")),
            "signal_category": own.get("signal_category"), "oilfield_relevance": own.get("oilfield_relevance"),
            "oilfield_application": own.get("oilfield_application"),
        }
    return {row["key"]: {**found[row["signal_id"]], "field": row["field"]} for row in rows if row["signal_id"] in found}


def _dedup_inputs(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Две карточки пары, как их видит судья «Дедупа» (signal_dedup.pair_prompt): заголовки, компании-участники, тема,
    суть, сайты ссылок."""
    from psycopg.rows import dict_row

    from oiltech_digest.db import connection

    ids = sorted({one for row in rows for one in row["pair"]})
    with connection.get_connection() as conn:
        found = {int(one["id"]): one for one in conn.cursor(row_factory=dict_row).execute(
            "SELECT s.id, s.title, s.title_ru, s.theme, s.summary, s.participants_json,"
            " ARRAY(SELECT e.source_url FROM signal_evidence e WHERE e.signal_id = s.id AND e.source_url IS NOT NULL"
            "       UNION SELECT a.url FROM articles a WHERE a.id = s.origin_article_id) AS urls"
            " FROM signals s WHERE s.id = ANY(%s)", (ids,)).fetchall()}

    def node(signal_id: int) -> dict[str, Any]:
        row = found[signal_id]
        companies = [str(one.get("name")) for one in row["participants_json"] or [] if isinstance(one, dict)
                     and one.get("name")]
        return {"signal": {"title": row["title"], "title_ru": row["title_ru"], "theme": row["theme"],
                           "summary": row["summary"], "companies": companies},
                "urls": [url for url in row["urls"] or [] if url]}

    return {row["key"]: {"a": node(row["pair"][0]), "b": node(row["pair"][1])} for row in rows
            if all(one in found for one in row["pair"])}


# --- Воркер, без базы: вызов агента с блоком правил ----------------------------------------------


def run(agent: str, client: Any, common: dict[str, Any], item: dict[str, Any], rules_block: Any) -> dict[str, Any]:
    """Ответ агента на вход строки с этим набором правил: как в его задаче; ответ и токены, без проверки (её делает
    ядро)."""
    if agent == "otbor":
        from oiltech_digest import otbor
        from oiltech_digest.otbor_answer import answer_schema

        dictionaries = common.get("dictionaries") or {}
        return otbor._decide(client, item, otbor.instructions_with_rules(dictionaries, rules_block),
                             answer_schema(dictionaries), dictionaries, common["model"], common.get("reasoning"))
    if agent == "card":
        from oiltech_digest import signal_card

        dictionaries, themes = common.get("dictionaries") or {}, list(common.get("themes") or [])
        response = client.complete_json(
            signal_card.instructions_with_rules(dictionaries, themes, rules_block,
                                                category=signal_card.item_category(item)),
            signal_card._model_input(item), signal_card._answer_schema(dictionaries, themes),
            max_output_tokens=output_budget(_CARD_TOKENS, common.get("reasoning")), model=common["model"],
            reasoning_effort=common.get("reasoning"))
        return {"signal_id": item["signal_id"], "publication": item.get("publication"), **_answer(response)}
    if agent == "scores":
        from oiltech_digest import signal_scores

        criteria = (common.get("profiles") or {}).get(item.get("profile")) or []
        response = client.complete_json(
            signal_scores.instructions(criteria, rules_block,
                                       category=signal_scores.profile_category(item.get("profile"))),
            signal_scores.model_input(item), signal_scores.answer_schema(criteria),
            max_output_tokens=output_budget(_SCORES_TOKENS, common.get("reasoning")), model=common["model"],
            reasoning_effort=common.get("reasoning"))
        return {"signal_id": item["signal_id"], "profile": item.get("profile"), "publication": item.get("publication"),
                **_answer(response)}
    if agent == "summary":
        from oiltech_digest.processing.pipeline import summarize_article, title_ru_for_article

        article = {**item, "expert_rules": rules_block}
        if item.get("field") == "title":
            title, response = title_ru_for_article(article, client, edit_russian=item.get("edit_title") is True)
            return {"text": title or "", **(_tokens(response) if response is not None else {})}
        response = summarize_article(article, client)
        return {"text": str(response.data.get("summary") or ""), **_tokens(response)}
    if agent == "dedup":
        from oiltech_digest import signal_dedup

        response = client.complete_json(
            signal_dedup.instructions(rules_block), signal_dedup.pair_prompt(item["a"], item["b"]),
            signal_dedup.SIGNAL_DUPLICATE_SCHEMA,
            max_output_tokens=output_budget(_DEDUP_TOKENS, config.SIGNAL_DEDUP_REASONING),
            model=signal_dedup.configured_model(), reasoning_effort=config.SIGNAL_DEDUP_REASONING)
        return {"same_event": (response.data or {}).get("same_event") is True, **_tokens(response)}
    raise ValueError(f"автопроверка: агент {agent} так не проверяется")


def run_search(common: dict[str, Any], rules_block: Any, heartbeat: Any = None) -> dict[str, Any]:
    """Повторный поиск по теме правила за дни проверки с подсказками этого набора: ссылки и заголовки найденного (и
    отсеянного техническими проверками: поиск его нашёл), токены и вызовы поиска."""
    from oiltech_digest import signal_research

    topic = str(common.get("topic") or "")
    found = signal_research.research_topic(topic, days=int(common.get("days") or 7), limit=common.get("limit"),
                                           hints=expert_rules.search_hints(rules_block, topic), heartbeat=heartbeat,
                                           model=common.get("model"), reasoning=common.get("reasoning"))
    items = [*(found.get("evidence") or []), *(found.get("dropped") or [])]
    return {"status": found.get("status"), "error": found.get("error"),
            "items": [{"url": str(one.get("source_url") or one.get("url") or ""), "title": str(one.get("title") or "")}
                      for one in items],
            "model": found.get("model"), "input_tokens": int(found.get("input_tokens") or 0),
            "output_tokens": int(found.get("output_tokens") or 0),
            "web_search_calls": int(found.get("web_search_calls") or 0)}


def _answer(response: Any) -> dict[str, Any]:
    return {"answer": response.data, **_tokens(response)}


def _tokens(response: Any) -> dict[str, Any]:
    return {"model": response.model, "input_tokens": int(response.input_tokens or 0),
            "output_tokens": int(response.output_tokens or 0)}


# --- Ядро, при записи: ответ агента как значение для сверки --------------------------------------


class Judge:
    """Проверка ответов кодом ядра для одной проверки: то, что дошло бы до сигнала. Данные для проверки (справочники,
    публикации, критерии) читаются один раз."""

    def __init__(self, agent: str, rows: list[dict[str, Any]]):
        self.agent = agent
        ids = sorted({row["signal_id"] for row in rows})
        if agent == "otbor":
            from oiltech_digest.db import otbor_inputs
            from oiltech_digest.otbor_pack_format import load_dictionaries

            self.dictionaries = load_dictionaries()
            self.links = otbor_inputs.links_in_input([row["signal_id"] for row in rows
                                                      if str(row.get("ref", "")).startswith("signal:")])
        elif agent == "card":
            from oiltech_digest import signal_card, signal_themes
            from oiltech_digest.db import repository, signal_card_store
            from oiltech_digest.signal_card_pack import load_dictionaries

            self.dictionaries = load_dictionaries()
            self.themes = signal_themes.names(repository.list_enabled_tags())
            self.publications = {int(one["signal_id"]): signal_card._publication(one)
                                 for one in signal_card_store.card_inputs(ids)}
        elif agent == "scores":
            from oiltech_digest import signal_scores
            from oiltech_digest.db import repository, signal_scores_store

            self.contexts = {int(one["signal_id"]): signal_scores.context(one)
                             for one in signal_scores_store.score_inputs(ids)}
            self.criteria = {profile: repository.list_enabled_scoring_criteria(profile)
                             for profile in signal_scores_store.PROFILES.values()}

    def value(self, row: dict[str, Any], answer: dict[str, Any] | None) -> Any:
        """Значение ответа для сверки с эталоном строки; None: ответа нет или он не прошёл проверку кодом."""
        if not isinstance(answer, dict) or answer.get("error"):
            return None
        if self.agent == "otbor":
            from oiltech_digest.otbor_answer import outcome

            checked = outcome(answer, self.dictionaries, links=self.links.get(row["signal_id"], 0))
            if checked.values is None:
                return None
            return checked.values["signal_category" if row["kind"] == "category" else "otbor_decision"]
        if self.agent == "card":
            from oiltech_digest import signal_card
            from oiltech_digest.rule_check_rows import card_field_value

            publication = self.publications.get(row["signal_id"])
            if publication is None:
                return None
            values, _problem = signal_card.card_values(answer, publication, self.dictionaries, self.themes)
            return None if values is None else card_field_value(row["field"], values)
        if self.agent == "scores":
            from oiltech_digest import signal_scores

            context = self.contexts.get(row["signal_id"])
            if context is None or answer.get("profile") != context["profile"]:
                return None
            values, _problem = signal_scores.score_values(answer, context, self.criteria[context["profile"]])
            return None if values is None else values["band"]
        if self.agent == "dedup":
            return bool(answer.get("same_event"))
        if self.agent == "summary":
            return " ".join(str(answer.get("text") or "").split()) or None
        return None

    def expected(self, row: dict[str, Any]) -> Any:
        """Ожидаемое значение строки в том же виде, что value."""
        if self.agent == "card":
            from oiltech_digest.rule_check_rows import card_field_value

            return card_field_value(row["field"], row["expected"])
        return row["expected"]

    def accepted(self, row: dict[str, Any]) -> Any:
        """Принятый ответ агента в том же виде, что value."""
        if row.get("accepted") is None:
            return None
        if self.agent == "otbor":
            return row["accepted"]["category" if row["kind"] == "category" else "decision"]
        if self.agent == "card":
            from oiltech_digest.rule_check_rows import card_field_value

            return card_field_value(row["field"], row["accepted"])
        return row["accepted"]
