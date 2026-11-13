"""Отметка ОС (#188, спека #187): одна форма у любой карточки обеих витрин и в «Отклонённых», один путь записи.

Отметка ложится в журнал ОС (signal_feedback_events) с автором, у любой карточки: находки поиска, сигнала ленты,
материала коллеги, любой категории. Явные действия применяет сразу только отметка администратора: правка заголовка и
сути (запись журнала поверх текста модели, как в #162), «Не тот блок» (категория и баллы только этой карточки), «Дубль»
(склейка с корнем группы; выбранную «в дайджест» не склеивает и говорит почему), «Не релевантно» (решение человека «не
брать» в полях «Отбора», раздел «Убрано людьми»). Отметка остальных участников только записывается: её видно блоком на
карточке, а карточку она не меняет. Оценка карточку не двигает: место и «Решение платформы» по баллу платформы.

Старая память агента (signal_agent_memory) отметкой не пишется и агентами не читается (#188): её читатель, судья прежней
цепочки радара, снят 08.11, а словарь замен, который разбор «Рекомендаций AI-агенту» заводил из каждой стрелки «А → Б»,
портил заголовки найденных страниц. Переводы терминов эксперта перешли в словарь «Сути и перевода».
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from oiltech_digest.db import repository

# Виды прежней памяти: по ним админ смотрит её историю (/api/signals/memory); новых строк отметка не пишет.
FEEDBACK_MEMORY_TYPES = {
    "signal_glossary",
    "signal_quality_rule",
    "signal_query_hint",
    "signal_source_preference",
    "signal_title_correction",
    "signal_verdict",
    "signal_duplicate",
}

# Оценки, которые у администратора меняют карточку сразу (явные действия). Остальные оценки карточку не меняют.
WRONG_BLOCK = "wrong_block"
DUPLICATE = "merge_duplicate"
NOT_RELEVANT = "wrong_domain"
# Почему «Не тот блок» администратора не сменил категорию (signal_scores.wrong_block, #132): как сейчас, только у
# принятого «Отбором» бизнеса или технологии.
NOT_RECATEGORIZED = ("«Не тот блок» сразу меняет категорию только у карточки, принятой «Отбором» в бизнес-ленту или "
                     "технологический радар")


def record_feedback(fields: dict[str, Any], *, user_id: int | None, author: str | None,
                    applies: bool) -> dict[str, Any]:
    """Записать отметку ОС одним путём для любой карточки. applies: у отметки есть право на явные действия (отметка
    администратора); без него отметка только записывается, её правки заголовка и сути лежат в её блоке.

    Возвращает номер записи журнала, applied и итог явного действия, если оно было: recategorized (новая категория
    после «Не тот блок») или recategorize_refused (почему категория прежняя), merged и merge_refused (почему не
    склеено), removed и remove_refused (почему не убрано).
    ValueError: пустая отметка или нет карточки, к которой она."""
    comment = str(fields.get("comment") or "").strip()
    verdict = _normalize_verdict(fields.get("verdict"))
    reason = str(fields.get("reason") or "").strip()
    corrected_title = " ".join(str(fields.get("corrected_title") or "").split())
    corrected_thesis = str(fields.get("corrected_thesis") or "").strip()
    duplicate_of = _optional_int(fields.get("duplicate_of_signal_id"))
    article_id = _optional_int(fields.get("article_id"))
    signal_id = _optional_int(fields.get("signal_id"))
    source_url = str(fields.get("source_url") or "").strip()
    if not any([comment, verdict, reason, corrected_title, corrected_thesis, duplicate_of]):
        raise ValueError("comment or structured feedback is required")
    if article_id is None and signal_id is None and not source_url:
        raise ValueError("article_id, signal_id or source_url is required")
    event_id = repository.record_signal_feedback_event(
        article_id,
        "comment_added",
        signal_id=signal_id,
        signal_evidence_id=_optional_int(fields.get("signal_evidence_id")),
        source_url=source_url or None,
        signal_title=str(fields.get("signal_title") or "").strip() or None,
        user_id=user_id,
        comment=comment,
        verdict=verdict,
        reason=reason or None,
        corrected_title=corrected_title or None,
        corrected_thesis=corrected_thesis or None,
        duplicate_of_signal_id=duplicate_of,
        applied=applies,
    )
    repository.attach_feedback_to_signal_training_examples(event_id, signal_id=signal_id)
    result: dict[str, Any] = {"event_id": event_id, "applied": applies}
    if applies and signal_id is not None:
        result.update(_explicit_action(signal_id, verdict, duplicate_of, author=author, reason=reason or comment))
    return result


def _explicit_action(signal_id: int, verdict: str | None, duplicate_of: int | None, *, author: str | None,
                     reason: str) -> dict[str, Any]:
    """Явное действие отметки администратора по её оценке. Правки заголовка и сути отдельного действия не требуют:
    применённая запись журнала ложится поверх текста модели при чтении."""
    if verdict == WRONG_BLOCK:
        from oiltech_digest import signal_scores

        category = signal_scores.wrong_block(signal_id, author)
        return {"recategorized": category} if category else {"recategorize_refused": NOT_RECATEGORIZED}
    if verdict == DUPLICATE and duplicate_of is not None:
        refusal = repository.merge_refusal(signal_id, duplicate_of)
        merge_reason = f"обратная связь: дубль #{duplicate_of}" + (f": {reason}" if reason else "")
        if refusal is None and not repository.mark_signal_merged(signal_id, duplicate_of, reason=merge_reason,
                                                                 respect_review=False):
            refusal = "карточка изменилась, пока сохранялась отметка: обновите витрину"
        return {"merged": refusal is None, **({"merge_refused": refusal} if refusal else {})}
    if verdict == NOT_RELEVANT:
        from oiltech_digest.db import removed_by_people

        refusal = removed_by_people.remove(signal_id, author=author, reason=reason)
        return {"removed": refusal is None, **({"remove_refused": refusal} if refusal else {})}
    return {}


def import_signal_feedback_csv(path: str | Path, *, user_id: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """ОС из таблицы заказчика (CSV): строка с комментарием ложится в журнал ОС тем же путём, что форма."""
    rows = _read_feedback_csv(path)
    events = 0
    for row in rows:
        comment = str(row.get("Комментарий") or "").strip()
        if not comment:
            continue
        events += 1
        if dry_run:
            continue
        record_feedback({"comment": comment, "signal_title": row.get("Сигнал"), "source_url": row.get("URL")},
                        user_id=user_id, author=None, applies=True)
    return {"rows": len(rows), "feedback_events": events, "dry_run": dry_run}


def _normalize_verdict(value: Any) -> str | None:
    verdict = str(value or "").strip().lower()
    if not verdict:
        return None
    aliases = {
        "good": "approved",
        "useful": "approved",
        "полезно": "approved",
        "ok": "approved",
        "bad": "reject",
        "noise": "reject",
        "шум": "reject",
        "дубль": "merge_duplicate",
        "duplicate": "merge_duplicate",
        "merge": "merge_duplicate",
        "не тот блок": "wrong_block",
        "бизнес": "wrong_block",
        "завышено": "overrated",
        "оценка завышена": "overrated",
    }
    verdict = aliases.get(verdict, verdict)
    allowed = {
        # Шкала заказчика (список эксперта заказчика от 13.09): оценка человека, а не модели.
        "strong_signal",
        "approved",
        "watch_later",
        "background_material",
        "reject",
        "wrong_domain",
        "merge_duplicate",
        # «Не тот блок»: бизнес-сигнал в технологическом радаре (встреча 21.09, решение 5).
        "wrong_block",
        # «Оценка завышена» (03.10): находка годная, но балл выше, чем она стоит.
        "overrated",
        # Ниже: вердикты прежней шкалы. С экрана убраны (заказчик их не просил),
        # но приём оставлен: в signal_feedback_events уже лежат строки с ними,
        # и запрет сделал бы прошлую разметку невалидной задним числом.
        "needs_better_source",
        "bad_translation",
        "too_generic",
    }
    return verdict if verdict in allowed else None


# Карточки темы за период поиска (30 дней), а не за 3 дня: 17.10 в теме было до 32 таких.
RESEARCH_KNOWN_TITLES = 40


def known_titles_block(known_titles: list[str] | None) -> str:
    """Уже найденное по теме, чтобы поиск искал новое, а не повторы. Это единственное из прошлого, что получает
    поиск (#138, граница данных О-7, решение владельца 03.11): ОС экспертов в его промпт не идёт."""
    known = _dedupe([re.sub(r"\s+", " ", str(title)).strip()[:140] for title in known_titles or [] if str(title).strip()])
    if not known:
        return ""
    return ("Уже есть на радаре по этой теме — не повторяй, ищи новое:\n"
            + "\n".join(f"- {title}" for title in known[:RESEARCH_KNOWN_TITLES]))


def _read_feedback_csv(path: str | Path) -> list[dict[str, str]]:
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = [line for line in text.splitlines() if not line.startswith(",,,,")]
    reader = csv.DictReader(lines)
    return [dict(row) for row in reader if row.get("Комментарий")]


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        normalized = re.sub(r"\s+", " ", value).strip()
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)
