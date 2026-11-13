"""Кнопка «Вернуть» в «Отклонённых»: решение человека «брать» и его отмена из журнала ОС.

С #132 человек возвращал отклонённое «Отбором», вписав то, что ставит «Отбор»: отрасль, связь и применение, категорию
(проверены: otbor_answer.return_problems). С #189 (решение владельца 13.11: «если карточка уходит, администратор
возвращает её кнопкой») так возвращается карточка из любого раздела «Отклонённых»: «Отбор», «Сбой», «Поиск → …»,
«До „Отбора“», «Другое»; «Убрано людьми» возвращает своя кнопка прежним решением (removed_by_people.restore).
Решение: «брать», источник всех полей: человек, пометка «спорное» с тем, откуда вернули; «Решение платформы»: по
ступени, когда будут баллы. Дальше публикация идёт по остатку конвейера: суть и перевод, оба вызова «Карточки»,
«Дедуп»; находка радара, спрятанная до «Отбора», встаёт на его путь (selector), её непереведённый заголовок переведёт
«Суть и перевод». Возврат и строка журнала ОС «сразу» с прежними полями пишутся одной транзакцией; отмена из журнала
возвращает прежние поля, и карточка снова в своём разделе, если с тех пор решение по ней не меняли и её не выбрали «в
дайджест» (выбранное не прячет ничто).

ВНИМАНИЕ про тесты: соединение: `connection.get_connection()` атрибутом модуля: изоляция тестов держится на подмене
этого атрибута.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest.db import connection, feedback_store, otbor_store
from oiltech_digest.db.repository import (_BUSINESS_VISIBLE_SQL, _RADAR_VISIBLE_SQL, CARD_SET_BY_HUMAN,
                                          SIGNAL_ORIGIN_RADAR, SIGNAL_SELECTOR_OTBOR, refresh_platform_decisions,
                                          signal_digest_sql)
from oiltech_digest.feed_window import OTBOR_ACCEPT, OTBOR_REJECT

# Итог возврата: возвращено; карточки нет в «Отклонённых»: возвращать нечего; убрано людьми (#188): его возвращает
# «Вернуть» раздела «Убрано людьми» прежним решением; не видно бы было ни в одной витрине (категория «другое», статья
# ленты вне открытого месяца): вернуть некуда, ничего не изменено.
RETURNED, NOT_REJECTED, REMOVED_BY_PEOPLE, NOT_SHOWABLE = "returned", "not_rejected", "removed_by_people", "not_showable"
# Что возврат меняет в сигнале: поля «Отбора», путь, заголовок и архив; и что конвейер посчитает после него (баллы,
# карточка): всё это вернёт отмена, карточка снова в своём разделе «без баллов», как была. И кто поставил значения.
COLUMNS = ("industry", "oilfield_relevance", "oilfield_application", "signal_category", "otbor_decision",
           "accepted_at", "reject_reason", "otbor_disputed", "otbor_failure", "otbor_failure_reason", "selector",
           "title_ru", "maturity", "archived_at", "archive_reason", "score", "score_profile", "score_items_json",
           "criteria_snapshot", "score_band", "scores_recipe", "scores_stale_since", "card_recipe")
SET_BY = ("industry", "oilfield_relevance", "oilfield_application", "signal_category", "otbor_decision")
_JSON_COLUMNS = frozenset({"score_items_json", "criteria_snapshot", "scores_recipe", "card_recipe"})
_TIME_COLUMNS = frozenset({"accepted_at", "archived_at", "scores_stale_since"})


def human_return(signal_id: int, values: dict[str, Any], *, author: str | None,
                 user_id: int | None = None) -> str | None:
    """Вернуть карточку из «Отклонённых» решением человека и записать строку журнала ОС «сразу» с прежними полями.
    RETURNED; NOT_REJECTED: карточки нет в «Отклонённых»; REMOVED_BY_PEOPLE: её возвращает «Вернуть» прежним решением;
    None: сигнала нет."""
    from oiltech_digest.db.rejected_sections import everything
    from oiltech_digest.otbor_answer import code_label
    from oiltech_digest.otbor_pack_format import load_dictionaries
    from oiltech_digest.processing.pipeline import _needs_translation

    label = partial(code_label, load_dictionaries())
    section = everything()
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(f"SELECT s.id, s.origin, s.origin_article_id, s.card_set_by, s.human_removal, s.title,"
                    f" {', '.join(f's.{column}' for column in COLUMNS)}, ({section.condition}) AS rejected,"
                    f" ({section.short_reason}) AS section_reason FROM signals s WHERE s.id = %s FOR UPDATE OF s",
                    (int(signal_id),))
        row = cur.fetchone()
        if row is None:
            return None
        if row["human_removal"] is not None:
            return REMOVED_BY_PEOPLE
        if row["otbor_decision"] != OTBOR_REJECT and not row["rejected"]:
            return NOT_REJECTED
        disputed = (f"«Отбор» не взял бы: {label('reject_reason', row['reject_reason']) or 'без причины'}"
                    if row["otbor_decision"] == OTBOR_REJECT else f"Было в «Отклонённых»: {row['section_reason']}")
        # Находка радара, спрятанная до «Отбора», встаёт на его путь; её непереведённый заголовок (поиск копирует
        # исходный) переведёт «Суть и перевод». Сигнал ленты суть и перевод получает обработкой статьи: путь прежний.
        radar_before = row["origin"] == SIGNAL_ORIGIN_RADAR and row["selector"] is None
        title_ru = row["title_ru"]
        if radar_before and title_ru == row["title"] and _needs_translation(row["title"] or ""):
            title_ru = None
        application = values.get("oilfield_application") if values["oilfield_relevance"] == "transferable" else None
        relevance = label("oilfield_relevance", values["oilfield_relevance"])
        step = {"stage": "Человек", "text": (
            f"Вернул{f' {author}' if author else ''}: отрасль «{label('industry', values['industry'])}», связь "
            f"«{relevance}»{f' ({application})' if application else ''}, категория "
            f"«{label('category', values['category'])}». {disputed}")}
        set_by = {**(row["card_set_by"] or {}), **{key: CARD_SET_BY_HUMAN for key in SET_BY}}
        cur.execute(
            f"""
            UPDATE signals
            SET industry = %s, oilfield_relevance = %s, oilfield_application = %s, signal_category = %s,
                otbor_decision = '{OTBOR_ACCEPT}', accepted_at = now(), reject_reason = NULL, otbor_disputed = %s,
                otbor_failure = NULL, otbor_failure_reason = NULL, card_set_by = %s,
                selector = CASE WHEN %s THEN '{SIGNAL_SELECTOR_OTBOR}' ELSE selector END, title_ru = %s,
                archived_at = NULL, archive_reason = NULL,
                decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now()
            WHERE id = %s
            """,
            (values["industry"], values["oilfield_relevance"], application, values["category"], disputed,
             Json(set_by), radar_before, title_ru, Json([step]), row["id"]),
        )
        # Вернуть значит показать: не видна бы была ни в одной витрине (категория «другое», статья ленты вне открытого
        # месяца), и ничего не меняем: иначе карточка пропала бы и из «Отклонённых», и из витрин.
        shown = cur.execute(f"SELECT ({_RADAR_VISIBLE_SQL}) OR ({_BUSINESS_VISIBLE_SQL}) AS shown FROM signals s"
                            " WHERE s.id = %s", (row["id"],)).fetchone()
        if not shown["shown"]:
            conn.rollback()
            return NOT_SHOWABLE
        refresh_platform_decisions([row["id"]], conn=conn)
        before = {"columns": {column: _plain(row[column]) for column in COLUMNS},
                  "set_by": {key: (row["card_set_by"] or {}).get(key) for key in SET_BY},
                  "section": row["section_reason"]}
        change = {"field": "returned", "before": before, "section": row["section_reason"],
                  "after": {"otbor_decision": OTBOR_ACCEPT, "signal_category": values["category"]}}
        feedback_store.create_action(feedback_store.IMMEDIATE, feedback_store.DONE, signal_id=row["id"],
                                     agents=["otbor"], details={"changes": [change]}, author=author,
                                     user_id=user_id, conn=conn)
        conn.commit()
    # Принятая статья: снова в ленте и в обработке; мост допишет сигналу суть, когда её запишет обработка.
    otbor_store.sync_feed_article(row)
    return RETURNED


def undo_problem(signal_id: int, row: dict[str, Any], change: dict[str, Any]) -> str | None:
    """Почему возврат не отменить: решение по карточке с тех пор поменяли (человек мог), или её выбрали «в дайджест»
    (выбранное не прячет ничто, отмена увела бы её из выпуска молча)."""
    from oiltech_digest.db.repository import signal_selected_for_digest

    after = change.get("after") or {}
    human = (row["card_set_by"] or {}).get("otbor_decision") == CARD_SET_BY_HUMAN
    if row["otbor_decision"] != after.get("otbor_decision") or not human \
            or row["signal_category"] != after.get("signal_category"):
        return "решение по карточке с тех пор поменяли"
    chosen = row["chosen"] if "chosen" in row else signal_selected_for_digest(signal_id)
    if chosen:
        return "карточку выбрали «в дайджест»: выбранную не прячет ничто; сначала уберите её из дайджеста"
    return None


def undo_return(signal_id: int, change: dict[str, Any], *, author: str | None) -> str | None:
    """Отмена «Вернуть»: прежние поля «Отбора», путь, заголовок и кто их ставил; карточка снова в своём разделе
    «Отклонённых». None: отменено; иначе почему нет (проверка под замком)."""
    before = change.get("before") or {}
    columns = before.get("columns") or {}
    with connection.get_connection() as conn:
        row = conn.cursor(row_factory=dict_row).execute(
            f"SELECT s.id, s.origin, s.origin_article_id, s.otbor_decision, s.signal_category, s.card_set_by,"
            f" {signal_digest_sql('s')} AS chosen FROM signals s WHERE s.id = %s FOR UPDATE OF s",
            (int(signal_id),)).fetchone()
        if row is None:
            return f"карточки №{int(signal_id)} нет"
        problem = undo_problem(signal_id, row, change)
        if problem:
            return problem
        set_by = dict(row["card_set_by"] or {})
        for key in SET_BY:
            previous = (before.get("set_by") or {}).get(key)
            if previous:
                set_by[key] = previous
            else:
                set_by.pop(key, None)
        step = {"stage": "Человек", "text": f"Отмена «Вернуть» из журнала ОС ({author or 'без имени'}): снова в "
                                            "«Отклонённых»" + (f" ({before['section']})" if before.get("section") else "")}
        values = [_restored(column, columns.get(column)) for column in COLUMNS]
        conn.execute(
            f"UPDATE signals SET {', '.join(f'{column} = %s' for column in COLUMNS)}, card_set_by = %s,"
            " decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now() WHERE id = %s",
            (*values, Json(set_by), Json([step]), int(signal_id)))
        refresh_platform_decisions([int(signal_id)], conn=conn)
        conn.commit()
    otbor_store.sync_feed_article(row)
    return None


def _plain(value: Any) -> Any:
    """Значение колонки в снимке журнала (JSON): время строкой, число из numeric числом."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return float(value) if isinstance(value, Decimal) else value


def _restored(column: str, value: Any) -> Any:
    if value is None:
        return 0 if column == "score" else None
    if column in _JSON_COLUMNS:
        return Json(value)
    return datetime.fromisoformat(value) if column in _TIME_COLUMNS else value
