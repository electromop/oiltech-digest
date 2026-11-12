"""«Убрано людьми» (#188, спека #187): администратор убрал карточку оценкой «Не релевантно», «Вернуть» её возвращает.

Уборка: решение человека «не брать» в полях «Отбора» с отметкой, что его поставил человек (card_set_by.otbor_decision),
поэтому повтор «Отбора» его не меняет (otbor_store._write_decision, own_article_ids, own_signal_ids). Карточка уходит из
витрины в раздел отклонённого «Убрано людьми» (rejected_sections); прежнее решение, кто, когда и почему лежат в
signals.human_removal, и «Вернуть» восстанавливает его как было. Выбранную «в дайджест» уборка не трогает: выбранное не
прячет ничто. Итог «Отбора» автору материала (material_signals) уборка не меняет.

ВНИМАНИЕ про тесты: соединение: `connection.get_connection()` атрибутом модуля (как в otbor_store): изоляция тестов
держится на подмене этого атрибута.
"""

from __future__ import annotations

from datetime import datetime, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest.db import connection, otbor_store
from oiltech_digest.db.repository import CARD_SET_BY_HUMAN, refresh_platform_decisions, signal_digest_sql
from oiltech_digest.feed_window import OTBOR_REJECT

# Итог «Вернуть»: восстановлено прежнее решение; людьми не убрана, возвращать нечего.
RESTORED, NOT_REMOVED = "restored", "not_removed"
# Поля, которые уборка меняет, а «Вернуть» восстанавливает: решение «Отбора» и «Решение платформы». Последнее у
# карточки до «Отбора» код не пересчитывает (refresh_platform_decisions берёт только решённые «Отбором»).
_FIELDS = ("otbor_decision", "reject_reason", "verdict_reason", "otbor_disputed", "otbor_failure",
           "otbor_failure_reason", "maturity")


def remove(signal_id: int, *, author: str | None, reason: str) -> str | None:
    """Убрать карточку в «Убрано людьми». None: убрана; иначе почему нет, словами для ответа администратору."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"SELECT s.id, s.origin, s.origin_article_id, s.card_set_by, s.human_removal,"
            f" {', '.join(f's.{field}' for field in _FIELDS)}, {signal_digest_sql('s')} AS chosen"
            " FROM signals s WHERE s.id = %s FOR UPDATE OF s",
            (int(signal_id),),
        )
        row = cur.fetchone()
        if row is None:
            return f"карточки №{int(signal_id)} нет"
        if row["human_removal"] is not None:
            return "карточка уже в «Убрано людьми»"
        if row["chosen"]:
            return "карточка выбрана «в дайджест»: выбранную не убирает ничто; сначала уберите её из дайджеста"
        set_by = dict(row["card_set_by"] or {})
        previous = {**{field: row[field] for field in _FIELDS}, "set_by": set_by.get("otbor_decision")}
        removal = {"previous": previous, "author": author, "reason": reason or None,
                   "at": datetime.now(timezone.utc).isoformat()}
        step = {"stage": "Человек", "text": f"«Не релевантно» ({author or 'без имени'}): убрано людьми"
                                            + (f": {reason}" if reason else "")}
        cur.execute(
            f"""
            UPDATE signals
            SET otbor_decision = '{OTBOR_REJECT}', reject_reason = NULL, verdict_reason = %s, otbor_disputed = NULL,
                otbor_failure = NULL, otbor_failure_reason = NULL, card_set_by = %s, human_removal = %s,
                decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now()
            WHERE id = %s
            """,
            (f"Убрано людьми: {reason}" if reason else "Убрано людьми: не релевантно",
             Json({**set_by, "otbor_decision": CARD_SET_BY_HUMAN}), Json(removal), Json([step]), row["id"]),
        )
        refresh_platform_decisions([row["id"]], conn=conn)
        conn.commit()
    otbor_store.sync_feed_article(row)
    return None


def restore(signal_id: int, *, author: str | None) -> str | None:
    """«Вернуть»: прежнее решение «Отбора» и «Решение платформы», каким они были до уборки, и прежняя отметка, кто
    поставил решение; карточка снова там, где была. RESTORED; NOT_REMOVED: людьми не убрана; None: сигнала нет."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id, origin, origin_article_id, card_set_by, human_removal FROM signals WHERE id = %s"
                    " FOR UPDATE", (int(signal_id),))
        row = cur.fetchone()
        if row is None:
            return None
        if row["human_removal"] is None:
            return NOT_REMOVED
        previous = dict(row["human_removal"].get("previous") or {})
        set_by = dict(row["card_set_by"] or {})
        if previous.get("set_by"):
            set_by["otbor_decision"] = previous["set_by"]
        else:
            set_by.pop("otbor_decision", None)
        step = {"stage": "Человек", "text": f"Вернул из «Убрано людьми» ({author or 'без имени'}): прежнее решение"}
        cur.execute(
            f"""
            UPDATE signals
            SET {', '.join(f'{field} = %s' for field in _FIELDS)}, card_set_by = %s, human_removal = NULL,
                decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now()
            WHERE id = %s
            """,
            (*(previous.get(field) for field in _FIELDS), Json(set_by), Json([step]), row["id"]),
        )
        refresh_platform_decisions([row["id"]], conn=conn)
        conn.commit()
    otbor_store.sync_feed_article(row)
    return RESTORED
