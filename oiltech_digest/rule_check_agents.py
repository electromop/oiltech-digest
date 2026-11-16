"""Агенты в автопроверке правил эксперта (#190, спека #187): по адаптеру на агента. Адаптер знает строки эталона ОС
своего агента (db/rule_check_rows), вход строки (ядро, при выдаче), вызов агента с набором правил (воркер, без базы) и
ответ агента как значение для сверки с эталоном (ядро, при записи).

Вызов тот же, что в задаче агента: та же инструкция с правилами и тот же вход (otbor.instructions_with_rules,
signal_card.instructions_with_rules, signal_scores.instructions, pipeline.summarize_article и title_ru_for_article,
signal_dedup.instructions, signal_research.research_topic); ответ проходит ту же проверку кодом ядра (otbor_answer.outcome,
signal_card.card_values, signal_scores.score_values): мерится то, что дошло бы до сигнала. Модели: у «Отбора»,
«Карточки», баллов и поиска из настроек ядра (едут в задаче), у «Сути и перевода» и «Дедупа» из .env воркера, как в их
задачах. Вход и общее агента берутся функциями самих агентов: проверка мерит то, что агент видит в работе.
"""

from __future__ import annotations

from typing import Any

from oiltech_digest import config, expert_rules
from oiltech_digest.db import rule_check_rows
from oiltech_digest.processing.openai_client import output_budget

_CARD_TOKENS, _SCORES_TOKENS, _DEDUP_TOKENS = 1200, 900, 700
# Текст ответа воркера (недоверенный ввод) в итоге проверки: не длиннее этого.
TEXT_CHARS = 2000


class CheckSetupError(ValueError):
    """Проверку нельзя выдать: не задана модель агента."""


class Adapter:
    """Агент в автопроверке. Ядро: rows (строки эталона), shared (общее для всех строк), inputs (вход строки),
    prepare и value / expected / accepted (сверка). Воркер: run (вызов агента с набором правил)."""

    def rows(self, steps: list[str], category: str | None, spec: dict[str, Any],
             settings: dict[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError

    def shared(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {}

    def inputs(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {row["key"]: {} for row in rows}

    def run(self, client: Any, common: dict[str, Any], item: dict[str, Any], rules_block: Any) -> dict[str, Any]:
        raise NotImplementedError

    def prepare(self, rows: list[dict[str, Any]]) -> None:
        """Данные для сверки (справочники, публикации, критерии): один раз на проверку."""

    def value(self, row: dict[str, Any], answer: dict[str, Any]) -> Any:
        """Значение ответа для сверки с эталоном строки; None: ответ не прошёл проверку кодом."""
        raise NotImplementedError

    def expected(self, row: dict[str, Any]) -> Any:
        return row["expected"]

    def accepted(self, row: dict[str, Any]) -> Any:
        return row.get("accepted")


def _tokens(response: Any) -> dict[str, Any]:
    return {"model": response.model, "input_tokens": int(response.input_tokens or 0),
            "output_tokens": int(response.output_tokens or 0)}


def _model(name: str, setting: str) -> str:
    if not name:
        raise CheckSetupError(f"{setting} не задан: проверить правило этого агента нечем")
    return name


class Otbor(Adapter):
    """«Отбор»: решение (с причиной отказа, если эталон её называет) и категория."""

    def rows(self, steps, category, spec, settings):
        from oiltech_digest.otbor_pack_format import load_dictionaries

        reasons = {value["code"]: value["label"] for value in (load_dictionaries().get("reject_reason") or {}).get(
            "values") or []}
        return rule_check_rows.otbor_rows(steps, category, int(settings["autocheck_rows"]), reasons=reasons)

    def shared(self, spec):
        from oiltech_digest.otbor_pack_format import load_dictionaries

        return {"dictionaries": load_dictionaries(), "model": _model(config.OTBOR_MODEL, "OTBOR_MODEL"),
                "reasoning": config.OTBOR_REASONING}

    def inputs(self, rows):
        from oiltech_digest import otbor
        from oiltech_digest.db import otbor_inputs

        refs = {row["ref"] for row in rows}
        articles = [int(ref.split(":")[1]) for ref in refs if ref.startswith("article:")]
        signals = [int(ref.split(":")[1]) for ref in refs if ref.startswith("signal:")]
        items = {f"article:{int(one['article_id'])}": otbor._item(one) for one in otbor_inputs.article_inputs(articles)}
        items |= {f"signal:{int(one['signal_id'])}": otbor._signal_item(one)
                  for one in otbor_inputs.signal_inputs(signals)}
        return {row["key"]: items[row["ref"]] for row in rows if row["ref"] in items}

    def run(self, client, common, item, rules_block):
        from oiltech_digest import otbor
        from oiltech_digest.otbor_answer import answer_schema

        dictionaries = common.get("dictionaries") or {}
        return otbor._decide(client, item, otbor.instructions_with_rules(dictionaries, rules_block),
                             answer_schema(dictionaries), dictionaries, common["model"], common.get("reasoning"))

    def prepare(self, rows):
        from oiltech_digest.db import otbor_inputs
        from oiltech_digest.otbor_pack_format import load_dictionaries

        self.dictionaries = load_dictionaries()
        self.links = otbor_inputs.links_in_input([row["signal_id"] for row in rows
                                                  if str(row.get("ref", "")).startswith("signal:")])

    def value(self, row, answer):
        from oiltech_digest.otbor_answer import outcome

        checked = outcome(answer, self.dictionaries, links=self.links.get(row["signal_id"], 0))
        if checked.values is None:
            return None
        return self._decision(row, checked.values["otbor_decision"], checked.values["signal_category"],
                              checked.values["reject_reason"])

    def expected(self, row):
        return row["expected"] if row["kind"] == "category" or not row.get("reason") else \
            f"{row['expected']}:{row['reason']}"

    def accepted(self, row):
        found = row.get("accepted")
        return None if found is None else self._decision(row, found["decision"], found["category"], found.get("reason"))

    @staticmethod
    def _decision(row: dict[str, Any], decision: Any, category: Any, reason: Any) -> Any:
        if row["kind"] == "category":
            return category
        # Эталон называет причину отказа: сверяются решение и причина («решение и причина», как в экзамене #133).
        return f"{decision}:{reason}" if row.get("reason") else decision


class Card(Adapter):
    """«Карточка»: поле шага правила (тема, дата события, компании и страны, тип события, регион, класс, стадия)."""

    def rows(self, steps, category, spec, settings):
        return rule_check_rows.card_rows(steps, category, int(settings["autocheck_rows"]))

    def shared(self, spec):
        from oiltech_digest import signal_themes
        from oiltech_digest.db import repository
        from oiltech_digest.signal_card_pack import load_dictionaries

        return {"dictionaries": load_dictionaries(), "themes": signal_themes.names(repository.list_enabled_tags()),
                "model": _model(config.SIGNAL_CARD_MODEL, "SIGNAL_CARD_MODEL"),
                "reasoning": config.SIGNAL_CARD_REASONING}

    def inputs(self, rows):
        from oiltech_digest import signal_card
        from oiltech_digest.db import repository, signal_card_store

        tags = repository.list_enabled_tags()
        found = {int(one["signal_id"]): signal_card.payload_signal(one, tags)
                 for one in signal_card_store.card_inputs([row["signal_id"] for row in rows])}
        return {row["key"]: found[row["signal_id"]] for row in rows if row["signal_id"] in found}

    def run(self, client, common, item, rules_block):
        from oiltech_digest import signal_card

        dictionaries, themes = common.get("dictionaries") or {}, list(common.get("themes") or [])
        response = client.complete_json(
            signal_card.instructions_with_rules(dictionaries, themes, rules_block, item),
            signal_card._model_input(item), signal_card._answer_schema(dictionaries, themes),
            max_output_tokens=output_budget(_CARD_TOKENS, common.get("reasoning")), model=common["model"],
            reasoning_effort=common.get("reasoning"))
        return {"signal_id": item["signal_id"], "publication": item.get("publication"), "answer": response.data,
                **_tokens(response)}

    def prepare(self, rows):
        from oiltech_digest import signal_card, signal_themes
        from oiltech_digest.db import repository, signal_card_store
        from oiltech_digest.signal_card_pack import load_dictionaries

        self.dictionaries = load_dictionaries()
        self.themes = signal_themes.names(repository.list_enabled_tags())
        self.publications = {int(one["signal_id"]): signal_card._publication(one)
                             for one in signal_card_store.card_inputs([row["signal_id"] for row in rows])}

    def value(self, row, answer):
        from oiltech_digest import signal_card

        publication = self.publications.get(row["signal_id"])
        if publication is None:
            return None
        values, _problem = signal_card.card_values(answer, publication, self.dictionaries, self.themes)
        return None if values is None else rule_check_rows.card_field_value(row["field"], values)

    def expected(self, row):
        return rule_check_rows.card_field_value(row["field"], row["expected"])

    def accepted(self, row):
        found = row.get("accepted")
        return None if found is None else rule_check_rows.card_field_value(row["field"], found)


class Scores(Adapter):
    """Баллы: ступень итога (сильный, полезный, наблюдать, слабый)."""

    def rows(self, steps, category, spec, settings):
        return rule_check_rows.scores_rows(category, int(settings["autocheck_rows"]))

    def shared(self, spec):
        from oiltech_digest import signal_scores
        from oiltech_digest.db import repository
        from oiltech_digest.db.signal_scores_store import PROFILES

        rubrics = signal_scores.load_rubrics()
        return {"profiles": {profile: signal_scores.model_criteria(repository.list_enabled_scoring_criteria(profile),
                                                                   rubrics, profile) for profile in PROFILES.values()},
                "model": _model(config.SIGNAL_SCORES_MODEL, "SIGNAL_SCORES_MODEL"),
                "reasoning": config.SIGNAL_SCORES_REASONING}

    def inputs(self, rows):
        from oiltech_digest import signal_scores
        from oiltech_digest.db import signal_scores_store

        found = {int(one["signal_id"]): signal_scores.payload_item(signal_scores.context(one))
                 for one in signal_scores_store.score_inputs([row["signal_id"] for row in rows])}
        return {row["key"]: found[row["signal_id"]] for row in rows if row["signal_id"] in found}

    def run(self, client, common, item, rules_block):
        from oiltech_digest import signal_scores

        criteria = (common.get("profiles") or {}).get(item.get("profile")) or []
        response = client.complete_json(
            signal_scores.instructions(criteria, rules_block,
                                       category=signal_scores.profile_category(item.get("profile"))),
            signal_scores.model_input(item), signal_scores.answer_schema(criteria),
            max_output_tokens=output_budget(_SCORES_TOKENS, common.get("reasoning")), model=common["model"],
            reasoning_effort=common.get("reasoning"))
        return {"signal_id": item["signal_id"], "profile": item.get("profile"), "publication": item.get("publication"),
                "answer": response.data, **_tokens(response)}

    def prepare(self, rows):
        from oiltech_digest import signal_scores
        from oiltech_digest.db import repository, signal_scores_store

        self.contexts = {int(one["signal_id"]): signal_scores.context(one)
                         for one in signal_scores_store.score_inputs([row["signal_id"] for row in rows])}
        self.criteria = {profile: repository.list_enabled_scoring_criteria(profile)
                         for profile in signal_scores_store.PROFILES.values()}

    def value(self, row, answer):
        from oiltech_digest import signal_scores

        context = self.contexts.get(row["signal_id"])
        if context is None or answer.get("profile") != context["profile"]:
            return None
        values, _problem = signal_scores.score_values(answer, context, self.criteria[context["profile"]])
        return None if values is None else values["band"]


class Summary(Adapter):
    """«Суть и перевод»: текст сути или заголовка, без числа: «было / стало» рядом с текстом эксперта."""

    def rows(self, steps, category, spec, settings):
        return rule_check_rows.summary_rows(steps, category, int(settings["summary_check_edits"]))

    def inputs(self, rows):
        """Вход тот же, что в задаче агента: у находки и материала signal_summary._item, у сигнала ленты его статья, как
        её видит обработка ленты (суть пересчитывается: прежней сути во входе нет)."""
        from oiltech_digest import signal_summary
        from oiltech_digest.db import repository, signal_summary_store

        ids = sorted({row["signal_id"] for row in rows})
        found = {int(one["signal_id"]): signal_summary._item(one) for one in signal_summary_store.summary_inputs(ids)}
        feed = rule_check_rows.feed_articles([one for one in ids if one not in found])
        for article in repository.get_articles_by_ids(list(feed.values()), include_summary=True):
            signal_id = next(key for key, value in feed.items() if value == int(article["id"]))
            found[signal_id] = {**dict(article), "summary": None, "edit_title": True}
        return {row["key"]: {**found[row["signal_id"]], "field": row["field"]} for row in rows if row["signal_id"] in found}

    def run(self, client, common, item, rules_block):
        from oiltech_digest.processing.pipeline import summarize_article, title_ru_for_article

        article = {**item, "expert_rules": rules_block}
        if item.get("field") == "title":
            title, response = title_ru_for_article(article, client, edit_russian=item.get("edit_title") is True)
            return {"text": title or "", **(_tokens(response) if response is not None else {})}
        response = summarize_article(article, client)
        return {"text": str(response.data.get("summary") or ""), **_tokens(response)}

    def value(self, row, answer):
        return " ".join(str(answer.get("text") or "").split())[:TEXT_CHARS] or None


class Dedup(Adapter):
    """«Дедуп»: одно ли событие у пары карточек (судья пар прогона радара)."""

    def rows(self, steps, category, spec, settings):
        return rule_check_rows.dedup_rows(int(settings["autocheck_rows"]))

    def inputs(self, rows):
        from oiltech_digest.db import rule_check_store

        nodes = rule_check_store.pair_nodes([one for row in rows for one in row["pair"]])
        return {row["key"]: {"a": nodes[row["pair"][0]], "b": nodes[row["pair"][1]]} for row in rows
                if all(one in nodes for one in row["pair"])}

    def run(self, client, common, item, rules_block):
        from oiltech_digest import signal_dedup

        response = client.complete_json(
            signal_dedup.instructions(rules_block), signal_dedup.pair_prompt(item["a"], item["b"]),
            signal_dedup.SIGNAL_DUPLICATE_SCHEMA,
            max_output_tokens=output_budget(_DEDUP_TOKENS, config.SIGNAL_DEDUP_REASONING),
            model=signal_dedup.configured_model(), reasoning_effort=config.SIGNAL_DEDUP_REASONING)
        return {"same_event": (response.data or {}).get("same_event") is True, **_tokens(response)}

    def value(self, row, answer):
        return bool(answer.get("same_event"))


class Search(Adapter):
    """Поиск радара: повторный поиск по теме за дни проверки с подсказками и без против ТОП-списка темы."""

    def rows(self, steps, category, spec, settings):
        return [{**row, "topic": topic} for topic in spec.get("topics") or []
                for row in rule_check_rows.search_rows(topic, int(spec["days"]))]

    def shared(self, spec):
        from oiltech_digest import signal_discovery

        settings = signal_discovery.research_settings(days=int(spec.get("days") or 7))
        return {"days": int(spec.get("days") or 7), "model": settings["research_model"],
                "reasoning": settings["research_reasoning"], "limit": settings["research_limit"]}

    def search(self, common: dict[str, Any], topic: str, rules_block: Any, heartbeat: Any) -> dict[str, Any]:
        """Поиск по теме с подсказками этого набора: ссылки и заголовки найденного (и отсеянного техническими
        проверками: поиск его нашёл), токены и вызовы поиска."""
        from oiltech_digest import signal_research

        found = signal_research.research_topic(topic, days=int(common.get("days") or 7), limit=common.get("limit"),
                                               hints=expert_rules.search_hints(rules_block, topic),
                                               heartbeat=heartbeat, model=common.get("model"),
                                               reasoning=common.get("reasoning"))
        items = [*(found.get("evidence") or []), *(found.get("dropped") or [])]
        return {"status": found.get("status"), "error": found.get("error"),
                "items": [{"url": str(one.get("source_url") or one.get("url") or "")[:TEXT_CHARS],
                           "title": str(one.get("title") or "")[:500]} for one in items],
                "model": found.get("model"), "input_tokens": int(found.get("input_tokens") or 0),
                "output_tokens": int(found.get("output_tokens") or 0),
                "web_search_calls": int(found.get("web_search_calls") or 0)}

    def value(self, row, answer):
        return None


ADAPTERS: dict[str, type[Adapter]] = {"otbor": Otbor, "card": Card, "scores": Scores, "summary": Summary,
                                      "dedup": Dedup, "search": Search}


def adapter(agent: str) -> Adapter:
    """Адаптер агента: новый на каждую проверку (prepare держит её данные)."""
    return ADAPTERS[agent]()
