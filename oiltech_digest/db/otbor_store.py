"""Хранилище решений агента «Отбор» (#130, ADR 0005): что агенту читать и как писать решение у сигнала ленты.

Решение живёт в сигнале статьи. Отклонённая и не получившая решения статья тоже хранится сигналом (решение владельца
22.10), как отсеянные находки радара: разделы отклонённого, «Ход решения» и пометки у всех одни. Сигнал заводится
здесь, при первом решении или сбое; суть, перевод, тему и балл принятой допишет мост «лента → сигнал»
(feed_signals), когда их запишет обработка ленты.

ВНИМАНИЕ про тесты: соединение — `connection.get_connection()` атрибутом модуля (как в signal_card_store):
изоляция тестов держится на подмене этого атрибута.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest.db import connection, feed_signals, marks
from oiltech_digest.db.repository import (ARTICLE_SELECTOR_OTBOR, CARD_SET_BY_HUMAN, CARD_SET_BY_OTBOR,
                                          SIGNAL_ORIGIN_FEED, SYSTEM_TAG_UNCLASSIFIED)

# Вид задачи агента и стадия его расхода в ai_processing_runs.
KIND = "otbor"
# Сбой без решения: технический (модель не ответила) — шаг планировщика повторит; ответ не по правилам (не по
# схеме, не из справочника) — нет (решение владельца 23.10).
FAILURE_TECHNICAL = "technical"
FAILURE_INVALID = "invalid_answer"
# Итог записи решения: записано; не изменено — статью выбрали «в дайджест», а повтор «Отбора» решения людей не
# перезаписывает (спека #128: «выбранное «в дайджест» не прячет ничто»).
DECIDED, KEPT_IN_DIGEST = "decided", "kept_in_digest"
# Статья, уже решённая «Отбором» и выбранная «в дайджест»: её решение повтор не пересматривает.
_DECIDED_IN_DIGEST_SQL = (
    f"({marks.article_marked_sql('a.id', 'digest')} AND EXISTS (SELECT 1 FROM signals os"
    f" WHERE os.origin_article_id = a.id AND os.origin = '{SIGNAL_ORIGIN_FEED}' AND os.otbor_decision IS NOT NULL))"
)
# Поля решения «Отбора» в прежних колонках сигнала: кто поставил значение — в card_set_by.
_SET_BY_COLUMNS = ("industry", "oilfield_relevance", "oilfield_application", "signal_category")
_KEY_SQL = f"'{feed_signals.FEED_KEY_PREFIX}' || a.id"


def article_inputs(article_ids: Sequence[int]) -> list[dict[str, Any]]:
    """Статьи так, как их видит модель, — те же поля, что у статьи ленты в пакете эксперту (otbor_pack_store
    .feed_candidates): экзамен (#133) подаст агенту манифест пакета без переделки."""
    ids = sorted({int(value) for value in article_ids})
    if not ids:
        return []
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.id AS article_id, a.title, a.raw_text AS text, a.language,
                   COALESCE(a.published_at, a.collected_at) AS published, a.collected_at AS received,
                   src.name AS publisher
            FROM articles a
            JOIN sources src ON src.id = a.source_id
            WHERE a.id = ANY(%(ids)s)
            ORDER BY a.id
            """,
            {"ids": ids},
        )
        return cur.fetchall()


def new_article_ids(*, days: int, limit: int, attempts: int) -> list[int]:
    """Статьи для шага планировщика: их решает «Отбор» (вставлены при включённом флаге), собраны за `days` дней,
    решения нет ни у «Отбора», ни у гейта, ответ не был «не по правилам», не в задаче агента, которая ещё идёт,
    и попыток меньше `attempts`. Перепечатка не в счёт: её публикация — у сигнала главной."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            WITH tried AS (
              SELECT (jsonb_array_elements_text(j.payload_json->'article_ids'))::bigint AS id,
                     j.status IN ('queued', 'running', 'finalizing') AS pending
              FROM background_jobs j
              WHERE j.kind = '{KIND}' AND j.created_at >= now() - make_interval(days => %(window)s)
                AND jsonb_typeof(j.payload_json->'article_ids') = 'array'
            ),
            taken AS (
              SELECT id FROM tried GROUP BY id HAVING bool_or(pending) OR count(*) >= %(attempts)s
            )
            SELECT a.id
            FROM articles a
            WHERE a.selector = '{ARTICLE_SELECTOR_OTBOR}'
              AND a.collected_at >= now() - make_interval(days => %(days)s)
              AND NOT a.pending_deletion
              AND NOT EXISTS (SELECT 1 FROM article_reprints ar WHERE ar.article_id = a.id)
              AND NOT EXISTS (SELECT 1 FROM article_cards c WHERE c.article_id = a.id AND c.relevant IS NOT NULL)
              AND NOT EXISTS (SELECT 1 FROM signals s
                              WHERE s.origin_article_id = a.id AND s.origin = '{SIGNAL_ORIGIN_FEED}'
                                AND (s.otbor_decision IS NOT NULL OR s.otbor_failure = '{FAILURE_INVALID}'))
              AND NOT EXISTS (SELECT 1 FROM taken t WHERE t.id = a.id)
            ORDER BY a.id
            LIMIT %(limit)s
            """,
            {"days": int(days), "window": int(days) + 1, "limit": int(limit), "attempts": int(attempts)},
        ).fetchall()
    return [int(row[0]) for row in rows]


def unanswered_articles(*, days: int, attempts: int, limit: int) -> list[tuple[int, str]]:
    """Статьи «Отбора», которые шаг больше не возьмёт, а решения и записанного сбоя у них нет: задачи с ними
    `attempts` раз упали целиком (сборка воркера, настройка, сбой воркера — итога нет, писать нечего) или статья
    вышла из окна шага (`days` дней), так и не дождавшись решения. Без пометки её не видно нигде: после попыток
    статья лежит в разделе «„Отбор“ не ответил» (решение владельца 23.10). Пары (статья, короткая причина)."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            WITH tried AS (
              SELECT (jsonb_array_elements_text(j.payload_json->'article_ids'))::bigint AS id,
                     j.status IN ('queued', 'running', 'finalizing') AS pending
              FROM background_jobs j
              WHERE j.kind = '{KIND}' AND jsonb_typeof(j.payload_json->'article_ids') = 'array'
            ),
            counted AS (SELECT id, count(*) AS tries, bool_or(pending) AS pending FROM tried GROUP BY id)
            SELECT a.id, COALESCE(t.tries, 0) AS tries
            FROM articles a
            LEFT JOIN counted t ON t.id = a.id
            WHERE a.selector = '{ARTICLE_SELECTOR_OTBOR}'
              AND NOT a.pending_deletion
              AND NOT COALESCE(t.pending, FALSE)
              AND (COALESCE(t.tries, 0) >= %(attempts)s
                   OR a.collected_at < now() - make_interval(days => %(days)s))
              AND NOT EXISTS (SELECT 1 FROM article_reprints ar WHERE ar.article_id = a.id)
              AND NOT EXISTS (SELECT 1 FROM article_cards c WHERE c.article_id = a.id AND c.relevant IS NOT NULL)
              AND NOT EXISTS (SELECT 1 FROM signals s
                              WHERE s.origin_article_id = a.id AND s.origin = '{SIGNAL_ORIGIN_FEED}'
                                AND (s.otbor_decision IS NOT NULL OR s.otbor_failure IS NOT NULL))
            ORDER BY a.id
            LIMIT %(limit)s
            """,
            {"days": int(days), "attempts": int(attempts), "limit": int(limit)},
        ).fetchall()
    return [(int(row[0]), f"задачи «Отбора» с ней падали целиком: {int(row[1])} из {int(attempts)}, решения нет"
             if int(row[1]) >= int(attempts) else f"не дошла до «Отбора» за {int(days)} дн.") for row in rows]


def own_article_ids(article_ids: Sequence[int]) -> list[int]:
    """Из названных статей — те, что решает «Отбор»: решённое гейтом он не пересматривает (проверка на реальных
    данных, #134, пишет свои решения отдельно), решённое им и выбранное «в дайджест» тоже (chosen_for_digest)."""
    ids = sorted({int(value) for value in article_ids})
    if not ids:
        return []
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT a.id FROM articles a
            WHERE a.id = ANY(%s) AND a.selector = '{ARTICLE_SELECTOR_OTBOR}'
              AND NOT EXISTS (SELECT 1 FROM article_cards c WHERE c.article_id = a.id AND c.relevant IS NOT NULL)
              AND NOT {_DECIDED_IN_DIGEST_SQL}
            ORDER BY a.id
            """,
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


def _signal_for_update(cur, article_id: int) -> dict[str, Any] | None:
    """Сигнал статьи под замком; нет — заводится: заголовок статьи, тема «не классифицировано» (тема у сигнала
    обязательна, тег придёт позже), прочее — как у моста «лента → сигнал». None — статьи нет."""
    cur.execute(
        f"""
        INSERT INTO signals (signal_key, origin, origin_article_id, signal_category, title, theme, maturity,
                             first_seen_at, last_seen_at)
        SELECT {_KEY_SQL}, '{SIGNAL_ORIGIN_FEED}', a.id, '{feed_signals.FEED_CATEGORY}', a.title, %(unclassified)s,
               'watch', COALESCE(a.collected_at, now()), COALESCE(a.collected_at, now())
        FROM articles a WHERE a.id = %(id)s
        ON CONFLICT (signal_key) DO NOTHING
        """,
        {"id": int(article_id), "unclassified": SYSTEM_TAG_UNCLASSIFIED},
    )
    cur.execute(
        "SELECT id, maturity, card_set_by, otbor_decision FROM signals WHERE signal_key = %s FOR UPDATE",
        (f"{feed_signals.FEED_KEY_PREFIX}{int(article_id)}",),
    )
    return cur.fetchone()


def write_decision(article_id: int, values: dict[str, Any], *, disputed: str | None,
                   log: list[dict[str, str]], recipe: dict[str, Any]) -> str | None:
    """Записать проверенное решение «Отбора» в сигнал статьи: DECIDED, KEPT_IN_DIGEST (статья выбрана «в дайджест»,
    повтор её решение не меняет, шаги ложатся в «Ход решения»); None — статьи нет.

    Отрасль, связь и применение, категория — в прежние колонки, источник «otbor» (card_set_by); значение
    человека не перезаписывается никогда. У отклонённой «Решение платформы» (maturity) — «отклонено», у принятой
    после повторного решения — снова «наблюдать». Сбой прежней попытки снимается, шаги — в «Ход решения»."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        row = _signal_for_update(cur, article_id)
        if row is None:
            return None
        if row["otbor_decision"] not in (None, values["otbor_decision"]):
            cur.execute(f"SELECT {marks.article_marked_sql('%(id)s', 'digest')} AS chosen", {"id": int(article_id)})
            if cur.fetchone()["chosen"]:
                kept = {"stage": "«Отбор»", "text": "Статья выбрана «в дайджест»: решение не изменено"}
                cur.execute(
                    "UPDATE signals SET decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb,"
                    " updated_at = now() WHERE id = %s",
                    (Json([*log, kept]), row["id"]),
                )
                conn.commit()
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
        rejected = values["otbor_decision"] == "reject"
        maturity = "reject" if rejected else ("watch" if row["maturity"] == "reject" else row["maturity"])
        updates.update({
            "otbor_decision": values["otbor_decision"], "reject_reason": values.get("reject_reason"),
            "verdict_reason": values.get("verdict_reason"), "otbor_disputed": disputed, "maturity": maturity,
            "otbor_failure": None, "otbor_failure_reason": None,
            "otbor_recipe": Json(recipe), "card_set_by": Json(set_by),
        })
        assignments = ", ".join(f"{column} = %s" for column in updates)
        cur.execute(
            f"UPDATE signals SET {assignments}, decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb,"
            " updated_at = now() WHERE id = %s",
            [*updates.values(), Json(log), row["id"]],
        )
        conn.commit()
    # Публикация статьи — к её сигналу, у принятой — и суть, тема, балл, когда они есть.
    feed_signals.sync_quietly([int(article_id)])
    return DECIDED


def write_failure(article_id: int, kind: str, reason: str, *, log: list[dict[str, str]]) -> bool:
    """Сбой без решения: сигнал статьи — в раздел сбоя (технический или ответ не по правилам), шаг — в «Ход
    решения». Решение прежней попытки сбой не снимает: оно остаётся в силе. False — статьи нет."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        row = _signal_for_update(cur, article_id)
        if row is None:
            return False
        undecided = row["otbor_decision"] is None
        cur.execute(
            "UPDATE signals SET otbor_failure = %s, otbor_failure_reason = %s,"
            " decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now() WHERE id = %s",
            (kind if undecided else None, reason if undecided else None, Json(log), row["id"]),
        )
        conn.commit()
    feed_signals.sync_quietly([int(article_id)])
    return True


def record_usage(job_id: int | None, rows: list[dict[str, Any]]) -> None:
    """Расход агента — в ai_processing_runs под его именем, строка на (задача, статья): повторное применение
    итога той же задачи счёт не удваивает (уникальный индекс по job_id, article_id, stage)."""
    if not rows:
        return
    with connection.get_connection() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO ai_processing_runs
                  (job_id, article_id, stage, provider, model, input_tokens, output_tokens, total_tokens, cost_usd,
                   status)
                VALUES (%s, %s, %s, 'openai', %s, %s, %s, %s, %s, 'ok')
                ON CONFLICT (job_id, article_id, stage) DO NOTHING
                """,
                (job_id, row["article_id"], KIND, row.get("model"), row["input_tokens"], row["output_tokens"],
                 row["input_tokens"] + row["output_tokens"], row["cost_usd"]),
            )
        conn.commit()
