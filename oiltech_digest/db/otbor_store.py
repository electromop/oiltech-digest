"""Хранилище решений агента «Отбор» (#130, #131, ADR 0005): кого решает агент и как писать решение у сигнала. Что
модель видит о публикации — otbor_inputs (с #132; прежние имена отсюда остаются).

Решение живёт в сигнале публикации. У статьи ленты сигнал заводится здесь, при первом решении или сбое: отклонённая
и не получившая решения статья тоже хранится сигналом (решение владельца 22.10), как отсеянные находки радара; суть,
перевод, тему и балл принятой допишет мост «лента → сигнал» (feed_signals), когда их запишет обработка ленты.
Находка поиска и материал коллеги (#131) — уже сигналы с меткой пути (signals.selector): решение пишется в них.

ВНИМАНИЕ про тесты: соединение — `connection.get_connection()` атрибутом модуля (как в signal_card_store):
изоляция тестов держится на подмене этого атрибута.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest import config
from oiltech_digest.db import connection, feed_signals, marks
from oiltech_digest.db.otbor_inputs import LINKS_LIMIT, article_inputs, links_in_input, signal_inputs  # noqa: F401
from oiltech_digest.db.repository import (ARTICLE_SELECTOR_OTBOR, CARD_SET_BY_HUMAN, CARD_SET_BY_OTBOR,
                                          SIGNAL_ORIGIN_FEED, SIGNAL_SELECTOR_OTBOR, SYSTEM_TAG_UNCLASSIFIED,
                                          refresh_platform_decisions)
from oiltech_digest.feed_window import OTBOR_ACCEPT, OTBOR_REJECT

# Вид задачи агента и стадия его расхода в ai_processing_runs.
KIND = "otbor"
# Сбой без решения: технический (модель не ответила) — шаг планировщика повторит; ответ не по правилам (не по
# схеме, не из справочника) — нет (решение владельца 23.10).
FAILURE_TECHNICAL = "technical"
FAILURE_INVALID = "invalid_answer"
# Итог записи решения: записано; не изменено — публикацию выбрали «в дайджест», а повтор «Отбора» решения людей не
# перезаписывает (спека #128: «выбранное «в дайджест» не прячет ничто»); не изменено — решение человека (#132).
DECIDED, KEPT_IN_DIGEST, KEPT_BY_HUMAN = "decided", "kept_in_digest", "kept_by_human"
# Итог возврата отклонённого человеком (#132): возвращено; не отклонено «Отбором» — возвращать нечего.
RETURNED, NOT_REJECTED = "returned", "not_rejected"
# Поля решения «Отбора» в прежних колонках сигнала: кто поставил значение — в card_set_by.
_SET_BY_COLUMNS = ("industry", "oilfield_relevance", "oilfield_application", "signal_category")


def _tried_sql(key: str) -> str:
    """Задачи агента с публикацией: строка на номер из списка задачи (article_ids или signal_ids) и идёт ли такая
    задача сейчас."""
    return (
        f"SELECT (jsonb_array_elements_text(j.payload_json->'{key}'))::bigint AS id,"
        " j.status IN ('queued', 'running', 'finalizing') AS pending"
        f" FROM background_jobs j WHERE j.kind = '{KIND}' AND jsonb_typeof(j.payload_json->'{key}') = 'array'"
    )


_TRIED_SQL = _tried_sql("article_ids")
_TRIED_SIGNALS_SQL = _tried_sql("signal_ids")
# Статья, которую решает «Отбор»: вставлена при включённом флаге, и гейт её не решал (флаг выключали — её решил он).
_OTBOR_ARTICLE_SQL = (
    f"a.selector = '{ARTICLE_SELECTOR_OTBOR}'"
    " AND NOT EXISTS (SELECT 1 FROM article_cards c WHERE c.article_id = a.id AND c.relevant IS NOT NULL)"
)
# Сигнал, который решает «Отбор» (#131): находка поиска или материал коллеги, заведённые при включённом флаге. Дубль
# не в счёт: его ссылки — у главной карточки.
_OTBOR_SIGNAL_SQL = f"s.selector = '{SIGNAL_SELECTOR_OTBOR}' AND s.merged_into_signal_id IS NULL"
# Задачи «Отбора» у находки или материала: сколько их было и идёт ли одна сейчас — строка на сигнал (LEFT JOIN по s.id).
SIGNAL_TRIES_SQL = (f"SELECT id, count(*) AS tries, bool_or(pending) AS pending FROM ({_TRIED_SIGNALS_SQL}) tried"
                    " GROUP BY id")


def signal_tries_over_sql(tries: str) -> str:
    """Шаг больше не возьмёт сигнал (#131): задача о нём не идёт, а попытки кончились (%(attempts)s) или сигнал старше
    окна шага (%(days)s). Одно правило для раздела «„Отбор“ не ответил» (unanswered_signals) и итога автору материала
    (material_signals). tries — алиас строки SIGNAL_TRIES_SQL."""
    return (f"(NOT COALESCE({tries}.pending, FALSE) AND (COALESCE({tries}.tries, 0) >= %(attempts)s"
            " OR s.created_at < now() - make_interval(days => %(days)s)))")


# Перепечатка не в счёт: её публикация — у сигнала главной.
_NOT_REPRINT_SQL = "NOT EXISTS (SELECT 1 FROM article_reprints ar WHERE ar.article_id = a.id)"


def _signal_has(condition: str) -> str:
    """У сигнала статьи (os) есть условие."""
    return (f"EXISTS (SELECT 1 FROM signals os WHERE os.origin_article_id = a.id AND os.origin = '{SIGNAL_ORIGIN_FEED}'"
            f" AND ({condition}))")


# Решение человека (#132): он вернул отклонённое, вписав поля «Отбора»; повтор «Отбора» его не пересматривает.
def _human_decided_sql(alias: str) -> str:
    return f"{alias}.card_set_by->>'otbor_decision' = '{CARD_SET_BY_HUMAN}'"


# Статья, уже решённая «Отбором» и выбранная «в дайджест»: её решение повтор не пересматривает.
_DECIDED_IN_DIGEST_SQL = (
    f"({marks.article_marked_sql('a.id', 'digest')} AND {_signal_has('os.otbor_decision IS NOT NULL')})"
)
# То же у сигнала (#131): отметка «в дайджест» у находки и материала — у самого сигнала.
_SIGNAL_DECIDED_IN_DIGEST_SQL = f"(s.otbor_decision IS NOT NULL AND {marks.signal_marked_sql('s.id', 'digest')})"


def new_article_ids(*, days: int, limit: int, attempts: int) -> list[int]:
    """Статьи для шага планировщика: их решает «Отбор» (вставлены при включённом флаге), собраны за `days` дней,
    решения нет ни у «Отбора», ни у гейта, ответ не был «не по правилам», не в задаче агента, которая ещё идёт,
    и попыток меньше `attempts`. Перепечатка не в счёт: её публикация — у сигнала главной."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            WITH tried AS ({_TRIED_SQL} AND j.created_at >= now() - make_interval(days => %(window)s)),
            taken AS (
              SELECT id FROM tried GROUP BY id HAVING bool_or(pending) OR count(*) >= %(attempts)s
            )
            SELECT a.id
            FROM articles a
            WHERE {_OTBOR_ARTICLE_SQL} AND {_NOT_REPRINT_SQL}
              AND a.collected_at >= now() - make_interval(days => %(days)s)
              AND NOT a.pending_deletion
              AND NOT {_signal_has(f"os.otbor_decision IS NOT NULL OR os.otbor_failure = '{FAILURE_INVALID}'")}
              AND NOT EXISTS (SELECT 1 FROM taken t WHERE t.id = a.id)
            ORDER BY a.id
            LIMIT %(limit)s
            """,
            {"days": int(days), "window": int(days) + 1, "limit": int(limit), "attempts": int(attempts)},
        ).fetchall()
    return [int(row[0]) for row in rows]


def new_signal_ids(*, days: int, limit: int, attempts: int) -> list[int]:
    """Находки и материалы для шага планировщика (#131) — то же правило, что у статей (new_article_ids): заведены
    при включённом флаге за `days` дней, решения нет, ответ не был «не по правилам», не в идущей задаче агента,
    попыток меньше `attempts`."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            WITH tried AS ({_TRIED_SIGNALS_SQL} AND j.created_at >= now() - make_interval(days => %(window)s)),
            taken AS (
              SELECT id FROM tried GROUP BY id HAVING bool_or(pending) OR count(*) >= %(attempts)s
            )
            SELECT s.id
            FROM signals s
            WHERE {_OTBOR_SIGNAL_SQL}
              AND s.created_at >= now() - make_interval(days => %(days)s)
              AND s.otbor_decision IS NULL AND s.otbor_failure IS DISTINCT FROM '{FAILURE_INVALID}'
              AND NOT EXISTS (SELECT 1 FROM taken t WHERE t.id = s.id)
            ORDER BY s.id
            LIMIT %(limit)s
            """,
            {"days": int(days), "window": int(days) + 1, "limit": int(limit), "attempts": int(attempts)},
        ).fetchall()
    return [int(row[0]) for row in rows]


def _unanswered_reason(tries: int, attempts: int, days: int) -> str:
    return (f"задачи «Отбора» с ней падали целиком: {tries} из {attempts}, решения нет" if tries >= attempts
            else f"не дошла до «Отбора» за {days} дн.")


def unanswered_articles(*, days: int, attempts: int, limit: int) -> list[tuple[int, str]]:
    """Статьи «Отбора», которые шаг больше не возьмёт, а решения и записанного сбоя у них нет: задачи с ними
    `attempts` раз упали целиком (сборка воркера, настройка, сбой воркера — итога нет, писать нечего) или статья
    вышла из окна шага (`days` дней), так и не дождавшись решения. Без пометки её не видно нигде: после попыток
    статья лежит в разделе «„Отбор“ не ответил» (решение владельца 23.10). Пары (статья, короткая причина)."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            WITH counted AS (
              SELECT id, count(*) AS tries, bool_or(pending) AS pending FROM ({_TRIED_SQL}) tried GROUP BY id
            )
            SELECT a.id, COALESCE(t.tries, 0) AS tries
            FROM articles a
            LEFT JOIN counted t ON t.id = a.id
            WHERE {_OTBOR_ARTICLE_SQL} AND {_NOT_REPRINT_SQL}
              AND NOT a.pending_deletion
              AND NOT COALESCE(t.pending, FALSE)
              AND (COALESCE(t.tries, 0) >= %(attempts)s
                   OR a.collected_at < now() - make_interval(days => %(days)s))
              AND NOT {_signal_has("os.otbor_decision IS NOT NULL OR os.otbor_failure IS NOT NULL")}
            ORDER BY a.id
            LIMIT %(limit)s
            """,
            {"days": int(days), "attempts": int(attempts), "limit": int(limit)},
        ).fetchall()
    return [(int(row[0]), _unanswered_reason(int(row[1]), int(attempts), int(days))) for row in rows]


def unanswered_signals(*, days: int, attempts: int, limit: int) -> list[tuple[int, str]]:
    """То же у находок и материалов (#131): шаг их больше не возьмёт, а решения и записанного сбоя нет — после попыток
    сигнал лежит в разделе «„Отбор“ не ответил», и автор материала узнаёт итог."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            WITH counted AS ({SIGNAL_TRIES_SQL})
            SELECT s.id, COALESCE(t.tries, 0) AS tries
            FROM signals s
            LEFT JOIN counted t ON t.id = s.id
            WHERE {_OTBOR_SIGNAL_SQL} AND {signal_tries_over_sql('t')}
              AND s.otbor_decision IS NULL AND s.otbor_failure IS NULL
            ORDER BY s.id
            LIMIT %(limit)s
            """,
            {"days": int(days), "attempts": int(attempts), "limit": int(limit)},
        ).fetchall()
    return [(int(row[0]), _unanswered_reason(int(row[1]), int(attempts), int(days))) for row in rows]


def own_article_ids(article_ids: Sequence[int]) -> list[int]:
    """Из названных статей — те, что решает «Отбор»: решённое гейтом он не пересматривает (проверка на реальных
    данных, #134, пишет свои решения отдельно), решённое им и выбранное «в дайджест» тоже (chosen_for_digest), и
    решённое человеком (#132)."""
    ids = sorted({int(value) for value in article_ids})
    if not ids:
        return []
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT a.id FROM articles a
            WHERE a.id = ANY(%s) AND {_OTBOR_ARTICLE_SQL} AND NOT {_DECIDED_IN_DIGEST_SQL}
              AND NOT {_signal_has(_human_decided_sql("os"))}
            ORDER BY a.id
            """,
            (ids,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def own_signal_ids(signal_ids: Sequence[int]) -> list[int]:
    """Из названных сигналов — находки и материалы, которые решает «Отбор» (#131), кроме решённых им и выбранных
    «в дайджест» и решённых человеком (#132): прежние находки радара он не пересматривает."""
    ids = sorted({int(value) for value in signal_ids})
    if not ids:
        return []
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"SELECT s.id FROM signals s WHERE s.id = ANY(%s) AND {_OTBOR_SIGNAL_SQL}"
            f" AND NOT {_SIGNAL_DECIDED_IN_DIGEST_SQL} AND NOT {_human_decided_sql('s')} ORDER BY s.id",
            (ids,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def chosen_for_digest(article_ids: Sequence[int]) -> list[int]:
    """Из названных статей — решённые «Отбором» и выбранные «в дайджест»: повтор их решение не пересматривает."""
    ids = sorted({int(value) for value in article_ids})
    if not ids:
        return []
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"SELECT a.id FROM articles a WHERE a.id = ANY(%s) AND {_DECIDED_IN_DIGEST_SQL} ORDER BY a.id", (ids,),
        ).fetchall()
    return [int(row[0]) for row in rows]


# Сигнал под замком для записи решения: прежнее решение, кто ставил поля, возвраты «Карточки» (#132).
_DECISION_ROW = "id, maturity, card_set_by, otbor_decision, otbor_returns"
# Пометка «спорное» у публикации, которую вернула «Карточка» и «Отбор» снова принял (спека #128).
RETURN_DISPUTED = "«Карточка» нашла в тексте дату события старше {days} дней, «Отбор» снова принял"


def _signal_for_update(cur, article_id: int) -> dict[str, Any] | None:
    """Сигнал статьи под замком; нет — его заводит мост «лента → сигнал» (feed_signals.BASE_SIGNAL_INSERT): суть,
    тему и балл принятой он допишет позже. None — статьи нет."""
    cur.execute(feed_signals.BASE_SIGNAL_INSERT, {"id": int(article_id), "unclassified": SYSTEM_TAG_UNCLASSIFIED})
    cur.execute(
        f"SELECT {_DECISION_ROW} FROM signals WHERE signal_key = %s FOR UPDATE",
        (f"{feed_signals.FEED_KEY_PREFIX}{int(article_id)}",),
    )
    return cur.fetchone()


def _own_signal_for_update(cur, signal_id: int) -> dict[str, Any] | None:
    """Находка или материал (#131) под замком: сигнал уже есть. None — его нет."""
    cur.execute(f"SELECT {_DECISION_ROW} FROM signals WHERE id = %s FOR UPDATE", (int(signal_id),))
    return cur.fetchone()


def _write_decision(cur, row: dict[str, Any], values: dict[str, Any], *, disputed: str | None,
                    log: list[dict[str, str]], recipe: dict[str, Any], chosen_sql: str) -> str:
    """Решение в сигнал под замком (row). chosen_sql — выбрана ли публикация «в дайджест» (SQL, один параметр %s):
    тогда другое решение повтора не пишется, шаги ложатся в «Ход решения» (KEPT_IN_DIGEST).

    Отрасль, связь и применение, категория — в прежние колонки, источник «otbor» (card_set_by); значение
    человека не перезаписывается никогда. «Решение платформы» (maturity) пересчитывает код
    (repository.platform_decision_sql): у отклонённой — «отклонено», у «другого» его нет, у принятой — по ступени
    баллов, пока их нет — «наблюдать». Сбой прежней попытки снимается, шаги — в «Ход решения». Принял снова
    публикацию, которую вернула «Карточка» (#132), — пометка «спорное»."""
    if (row["card_set_by"] or {}).get("otbor_decision") == CARD_SET_BY_HUMAN:
        # Решение человека главнее (#132): повтор «Отбора» и запоздалый итог его не меняют, шаги — в «Ход решения».
        kept = {"stage": "«Отбор»", "text": "Решение человека: не изменено"}
        cur.execute(
            "UPDATE signals SET decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb,"
            " updated_at = now() WHERE id = %s",
            (Json([*log, kept]), row["id"]),
        )
        return KEPT_BY_HUMAN
    if row["otbor_decision"] not in (None, values["otbor_decision"]):
        cur.execute(f"SELECT {chosen_sql} AS chosen", (row["chosen_id"],))
        if cur.fetchone()["chosen"]:
            kept = {"stage": "«Отбор»", "text": "Публикация выбрана «в дайджест»: решение не изменено"}
            cur.execute(
                "UPDATE signals SET decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb,"
                " updated_at = now() WHERE id = %s",
                (Json([*log, kept]), row["id"]),
            )
            return KEPT_IN_DIGEST
    set_by = dict(row["card_set_by"] or {})
    updates: dict[str, Any] = {}
    for column in _SET_BY_COLUMNS:
        if set_by.get(column) == CARD_SET_BY_HUMAN:
            continue
        updates[column] = values.get(column)
        if values.get(column):
            set_by[column] = CARD_SET_BY_OTBOR
        elif set_by.get(column) == CARD_SET_BY_OTBOR:
            set_by.pop(column)
    rejected = values["otbor_decision"] == OTBOR_REJECT
    if not rejected and row["otbor_returns"]:
        again = RETURN_DISPUTED.format(days=config.OTBOR_OLD_EVENT_DAYS)
        disputed = f"{disputed}; {again}" if disputed else again
        log = [*log, {"stage": "«Отбор»", "text": f"Спорное: {again}"}]
    if not rejected and row["otbor_decision"] != OTBOR_ACCEPT:
        # Пошла дальше «Отбора» сейчас: окна шагов конвейера считают от этого (signal_card_store.PIPELINE_SINCE_SQL).
        updates["accepted_at"] = datetime.now(timezone.utc)
    updates.update({
        "otbor_decision": values["otbor_decision"], "reject_reason": values.get("reject_reason"),
        "verdict_reason": values.get("verdict_reason"), "otbor_disputed": disputed,
        "otbor_failure": None, "otbor_failure_reason": None,
        "otbor_recipe": Json(recipe), "card_set_by": Json(set_by),
    })
    assignments = ", ".join(f"{column} = %s" for column in updates)
    cur.execute(
        f"UPDATE signals SET {assignments}, decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb,"
        " updated_at = now() WHERE id = %s",
        [*updates.values(), Json(log), row["id"]],
    )
    refresh_platform_decisions([row["id"]], conn=cur.connection)
    return DECIDED


def write_decision(article_id: int, values: dict[str, Any], *, disputed: str | None,
                   log: list[dict[str, str]], recipe: dict[str, Any]) -> str | None:
    """Записать проверенное решение «Отбора» в сигнал статьи: DECIDED, KEPT_IN_DIGEST (статья выбрана «в дайджест»,
    повтор её решение не меняет, шаги ложатся в «Ход решения»); None — статьи нет."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        row = _signal_for_update(cur, article_id)
        if row is None:
            return None
        written = _write_decision(cur, {**row, "chosen_id": int(article_id)}, values, disputed=disputed, log=log,
                                  recipe=recipe, chosen_sql=marks.article_marked_sql("%s", "digest"))
        conn.commit()
    if written == DECIDED:
        # Публикация статьи — к её сигналу, у принятой — и суть, тема, балл, когда они есть.
        feed_signals.sync_quietly([int(article_id)])
    return written


def write_signal_decision(signal_id: int, values: dict[str, Any], *, disputed: str | None,
                          log: list[dict[str, str]], recipe: dict[str, Any]) -> str | None:
    """То же у находки и материала (#131): решение — в сам сигнал; выбранный «в дайджест» повтор не меняет. Поля
    автора материала (заголовок, описание, компании) «Отбор» не пишет вовсе. None — сигнала нет."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        row = _own_signal_for_update(cur, signal_id)
        if row is None:
            return None
        written = _write_decision(cur, {**row, "chosen_id": int(signal_id)}, values, disputed=disputed, log=log,
                                  recipe=recipe, chosen_sql=marks.signal_marked_sql("%s", "digest"))
        conn.commit()
    return written


def human_return(signal_id: int, values: dict[str, Any], *, author: str | None) -> str | None:
    """Вернуть отклонённое «Отбором» решением человека (#132, спека #128): человек вписал то, что ставит «Отбор» —
    отрасль, связь и применение, категорию (проверены: otbor_answer.return_problems). Решение — «брать», источник всех
    полей — человек, пометка «спорное: „Отбор“ не взял бы» с причиной отказа; «Решение платформы» — по ступени, когда
    будут баллы. Дальше публикация идёт по остатку конвейера: суть и перевод, оба вызова «Карточки», «Дедуп». RETURNED;
    NOT_REJECTED — «Отбор» её не отклонял (вернуть можно только отклонённое); None — сигнала нет."""
    from oiltech_digest.otbor_pack_format import load_dictionaries

    dictionaries = load_dictionaries()

    def label(name: str, code: str | None) -> str:
        found = (dictionaries.get(name) or {}).get("values") or []
        return next((value["label"] for value in found if value["code"] == code), code or "без причины")

    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id, origin, origin_article_id, otbor_decision, reject_reason, card_set_by FROM signals"
                    " WHERE id = %s FOR UPDATE", (int(signal_id),))
        row = cur.fetchone()
        if row is None:
            return None
        if row["otbor_decision"] != OTBOR_REJECT:
            return NOT_REJECTED
        disputed = f"«Отбор» не взял бы: {label('reject_reason', row['reject_reason'])}"
        application = values.get("oilfield_application") if values["oilfield_relevance"] == "transferable" else None
        relevance = label("oilfield_relevance", values["oilfield_relevance"])
        step = {"stage": "Человек", "text": (
            f"Вернул{f' {author}' if author else ''}: отрасль «{label('industry', values['industry'])}», связь "
            f"«{relevance}»{f' ({application})' if application else ''}, категория "
            f"«{label('category', values['category'])}». {disputed}")}
        set_by = {**(row["card_set_by"] or {}),
                  **{column: CARD_SET_BY_HUMAN for column in (*_SET_BY_COLUMNS, "otbor_decision")}}
        cur.execute(
            f"""
            UPDATE signals
            SET industry = %s, oilfield_relevance = %s, oilfield_application = %s, signal_category = %s,
                otbor_decision = '{OTBOR_ACCEPT}', accepted_at = now(), reject_reason = NULL, otbor_disputed = %s,
                otbor_failure = NULL, otbor_failure_reason = NULL, card_set_by = %s,
                decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now()
            WHERE id = %s
            """,
            (values["industry"], values["oilfield_relevance"], application, values["category"], disputed,
             Json(set_by), Json([step]), row["id"]),
        )
        refresh_platform_decisions([row["id"]], conn=conn)
        conn.commit()
    if row["origin"] == SIGNAL_ORIGIN_FEED and row["origin_article_id"]:
        # Принятая статья — снова в ленте и в обработке; мост допишет сигналу суть, когда её запишет обработка.
        feed_signals.sync_quietly([int(row["origin_article_id"])])
    return RETURNED


def _write_failure(cur, row: dict[str, Any], kind: str, reason: str, log: list[dict[str, str]]) -> None:
    undecided = row["otbor_decision"] is None
    cur.execute(
        "UPDATE signals SET otbor_failure = %s, otbor_failure_reason = %s,"
        " decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now() WHERE id = %s",
        (kind if undecided else None, reason if undecided else None, Json(log), row["id"]),
    )


def write_failure(article_id: int, kind: str, reason: str, *, log: list[dict[str, str]]) -> bool:
    """Сбой без решения: сигнал статьи — в раздел сбоя (технический или ответ не по правилам), шаг — в «Ход
    решения». Решение прежней попытки сбой не снимает: оно остаётся в силе. False — статьи нет."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        row = _signal_for_update(cur, article_id)
        if row is None:
            return False
        _write_failure(cur, row, kind, reason, log)
        conn.commit()
    feed_signals.sync_quietly([int(article_id)])
    return True


def write_signal_failure(signal_id: int, kind: str, reason: str, *, log: list[dict[str, str]]) -> bool:
    """То же у находки и материала (#131). False — сигнала нет."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        row = _own_signal_for_update(cur, signal_id)
        if row is None:
            return False
        _write_failure(cur, row, kind, reason, log)
        conn.commit()
    return True


def record_usage(job_id: int | None, rows: list[dict[str, Any]]) -> None:
    """Расход агента — в ai_processing_runs под его именем, строка на (задача, публикация): статья — article_id,
    находка и материал — signal_id. Повторное применение итога той же задачи счёт не удваивает (уникальные индексы
    по job_id, article_id или signal_id, stage)."""
    if not rows:
        return
    with connection.get_connection() as conn:
        for row in rows:
            key = "signal_id" if row.get("signal_id") is not None else "article_id"
            conflict = ("(job_id, signal_id, stage) WHERE signal_id IS NOT NULL" if key == "signal_id"
                        else "(job_id, article_id, stage)")
            conn.execute(
                f"""
                INSERT INTO ai_processing_runs
                  (job_id, {key}, stage, provider, model, input_tokens, output_tokens, total_tokens, cost_usd, status)
                VALUES (%s, %s, %s, 'openai', %s, %s, %s, %s, %s, 'ok')
                ON CONFLICT {conflict} DO NOTHING
                """,
                (job_id, row[key], KIND, row.get("model"), row["input_tokens"], row["output_tokens"],
                 row["input_tokens"] + row["output_tokens"], row["cost_usd"]),
            )
        conn.commit()
