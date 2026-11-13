"""Материал коллеги, сигнал третьего происхождения (#131, спека #128): тот же конвейер, что у статей и находок.

- Материал при записи сразу становится сигналом происхождения material с меткой пути «Отбора» (флаг «Отбора» снят
  08.11: он решает всегда). Поля автора (заголовок, описание как суть, компании как участники) с отметкой «человек»:
  агенты их не переписывают и заполняют только недостающее. Оценка автора хранится блоком ОС с его именем из
  Telegram: на решение «Отбора» и показ она не влияет (решение владельца 22.10).
- Ссылка материала, его публикация, только если адрес ещё ничей: прикрепления к существующему сигналу нет,
  материал: свой сигнал и виден в витрине и без ссылки.
- Накопленные материалы (присланы до включения, статус «принят») проходят конвейер один раз: convert_accumulated
  (команда otbor-materials, сухой прогон по умолчанию). С --silent итог их авторам не сообщается (решение владельца
  04.11 для присланных до включения).
- Итог «Отбора» сообщается автору: ядро считает итог по сигналу (pending_notices), бот на NL отвечает в чате
  материала на его карточку и отмечает доставленное (mark_notified). Новый итог (человек вернул отклонённое): новое
  уведомление.

ВНИМАНИЕ про тесты: соединение: `connection.get_connection()` атрибутом модуля (как в colleague_materials).
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest import colleague_materials, config, feed_window
from oiltech_digest.db import connection, marks, otbor_store
from oiltech_digest.db.repository import (CARD_SET_BY_HUMAN, SIGNAL_ORIGIN_MATERIAL, SIGNAL_SELECTOR_OTBOR,
                                          SYSTEM_TAG_UNCLASSIFIED, refresh_platform_decisions,
                                          signal_publication_count_sql)
from oiltech_digest.feed_window import OTBOR_ACCEPT, OTBOR_REJECT
from oiltech_digest.otbor_pack_format import load_dictionaries

KEY_PREFIX = "material:"
# Витрина по категории «Отбора»: как её называет уведомление автору.
SHOWCASES = {"business": "бизнес-лента", "technology": "технологический радар",
             "other": "раздел «Другое», он виден администратору"}
# Пометка в notified_outcome: итог «Отбора» автору не сообщается, ни первый, ни следующий (otbor-materials --silent,
# решение владельца 04.11: накопленные присланы до включения, ответа автор не ждёт).
SILENT = "silent"
# Оценка цены накопленных до вызова: русский текст: около трёх знаков на токен, ответ «Отбора»: около 300 токенов.
_CHARS_PER_TOKEN, _OUTPUT_TOKENS = 3, 300


def to_signal(material: dict[str, Any], *, silent: bool = False) -> int:
    """Сигнал материала: заголовок, суть: описание, участники: компании автора (всё: «человек»), блок ОС с оценкой
    автора, ссылка: если адрес ничей. Повтор: тот же сигнал (ключ по номеру материала). Номер сигнала. silent: итог
    «Отбора» автору не сообщается (SILENT), пометка ставится в той же транзакции, что и связь материала с сигналом."""
    companies = list(material.get("companies_json") or [])
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            INSERT INTO signals (signal_key, origin, selector, title, theme, summary, maturity, companies_json,
                                 participants_json, card_set_by, decision_log, first_seen_at, last_seen_at)
            VALUES (%s, '{SIGNAL_ORIGIN_MATERIAL}', '{SIGNAL_SELECTOR_OTBOR}', %s, %s, %s, 'watch', %s, %s, %s, %s,
                    %s, %s)
            ON CONFLICT (signal_key) DO NOTHING
            RETURNING id
            """,
            (f"{KEY_PREFIX}{int(material['id'])}", material["title"], SYSTEM_TAG_UNCLASSIFIED, material["description"],
             Json(companies), Json([{"name": name, "quote": "", "verified": False} for name in companies]),
             Json({"summary": CARD_SET_BY_HUMAN, "participants": CARD_SET_BY_HUMAN}),
             Json([{"stage": "материал коллеги",
                    "text": f"Прислал {material['tg_author']}: «{material['title']}»"}]),
             material["created_at"], material["created_at"]),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute("SELECT id FROM signals WHERE signal_key = %s", (f"{KEY_PREFIX}{int(material['id'])}",))
            signal_id = int(cur.fetchone()["id"])
        else:
            signal_id = int(row["id"])
            _author_block(cur, signal_id, material)
            _link(cur, signal_id, material)
        cur.execute("UPDATE colleague_materials SET signal_id = %s WHERE id = %s AND signal_id IS NULL",
                    (signal_id, int(material["id"])))
        if silent:
            cur.execute("UPDATE colleague_materials SET notified_outcome = %s WHERE id = %s",
                        (SILENT, int(material["id"])))
        conn.commit()
    return signal_id


def _link(cur, signal_id: int, material: dict[str, Any]) -> None:
    """Ссылка материала, его публикация, если адрес ничей: адрес, который уже держит другая карточка, к материалу не
    переходит (он свой сигнал). Число публикаций сигнала пересчитывается."""
    if material.get("url"):
        cur.execute(
            """
            INSERT INTO signal_evidence (signal_id, source_url, title, publisher, evidence_type, extracted_fact,
                                         strength, raw_payload_json)
            VALUES (%s, %s, %s, NULL, 'material', %s, 1, %s)
            ON CONFLICT (source_url) DO NOTHING
            """,
            (signal_id, material["url"], material["title"], material["description"],
             Json({"evidence_source": "material", "material_id": int(material["id"])})),
        )
    cur.execute(f"UPDATE signals s SET evidence_count = {signal_publication_count_sql('s')} WHERE s.id = %s",
                (signal_id,))


def apply_edit(cur, material: dict[str, Any], fields: set[str], *, user_id: int | None = None) -> None:
    """Правка материала на платформе, его сигналу, в транзакции правки (cur, ревью PR #155): заголовок и русский
    заголовок, описание как суть, компании как участники (всё «человек»: агенты их не перепишут), оценка и комментарий
    в блок ОС автора, ссылка как публикация материала. fields: колонки материала, которые правка поменяла. Решение
    «Отбора» и карточка, принятые по прежнему тексту, не пересчитываются. Сигнал в выпуске закрытого месяца правка не
    меняет: тот выпуск только на просмотр. Материала-сигнала нет: ничего. user_id: кто правил (журнал ОС)."""
    if not material.get("signal_id"):
        return
    signal_id = int(material["signal_id"])
    month = feed_window.signal_month_sql("s")
    cur.execute(
        f"SELECT s.card_set_by, ({feed_window.current().closed_sql(month)}"
        f" AND {marks.signal_marked_sql('s.id', 'digest')}) AS frozen FROM signals s WHERE s.id = %s FOR UPDATE",
        (signal_id,),
    )
    row = cur.fetchone()
    if row is None:
        return
    if row["frozen"]:
        # Выпуск закрытого месяца только на просмотр, и его текст тоже (решение владельца 30.09).
        cur.execute(
            "UPDATE signals SET decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now()"
            " WHERE id = %s",
            (Json([{"stage": "Человек", "text": "Правка материала не перенесена: сигнал в выпуске закрытого месяца"}]),
             signal_id),
        )
        return
    set_by = dict(row["card_set_by"] or {})
    updates: dict[str, Any] = {}
    changed: list[str] = []
    if "title" in fields:
        # Русский заголовок тоже: экран показывает его, а «Суть и перевод» писала его по прежнему заголовку.
        updates.update(title=material["title"], title_ru=material["title"])
        set_by["title_ru"] = CARD_SET_BY_HUMAN
        changed.append("заголовок")
    if "description" in fields:
        updates["summary"] = material["description"]
        set_by["summary"] = CARD_SET_BY_HUMAN
        changed.append("описание")
    if "companies_json" in fields:
        companies = list(material.get("companies_json") or [])
        updates.update(companies_json=Json(companies),
                       participants_json=Json([{"name": name, "quote": "", "verified": False} for name in companies]))
        set_by["participants"] = CARD_SET_BY_HUMAN
        changed.append("компании")
    if fields & {"verdict", "verdict_comment"}:
        cur.execute(
            """
            UPDATE signal_feedback_events SET verdict = %s, comment = %s, signal_title = %s
            WHERE id = (SELECT id FROM signal_feedback_events WHERE signal_id = %s AND author = %s
                          AND event_type = 'comment_added' ORDER BY id LIMIT 1)
            RETURNING id
            """,
            (material["verdict"], material.get("verdict_comment"), material["title"], signal_id, material["tg_author"]),
        )
        if cur.fetchone() is None:
            _author_block(cur, signal_id, material)
        changed.append("оценка")
    if "url" in fields:
        cur.execute("DELETE FROM signal_evidence WHERE signal_id = %s AND raw_payload_json->>'evidence_source' = 'material'",
                    (signal_id,))
        _link(cur, signal_id, material)
        changed.append("ссылка")
    if not changed:
        return
    if fields & {"title", "description"}:
        # Правка заголовка и сути с экрана «Дайджест» лежит в журнале ОС поверх текста сигнала (#162): правка материала
        # ложится туда же, иначе прежняя правка с «Дайджеста» прятала бы её. У сигнала один текст, последний по времени.
        cur.execute(
            "INSERT INTO signal_feedback_events (signal_id, user_id, event_type, corrected_title, corrected_thesis)"
            " VALUES (%s, %s, 'comment_added', %s, %s)",
            (signal_id, user_id, material["title"] if "title" in fields else None,
             (material.get("description") or None) if "description" in fields else None),
        )
    updates["card_set_by"] = Json(set_by)
    step = {"stage": "Человек", "text": "Материал исправлен на платформе: " + ", ".join(changed)}
    assignments = ", ".join(f"{column} = %s" for column in updates)
    cur.execute(
        f"UPDATE signals SET {assignments}, decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb,"
        " updated_at = now() WHERE id = %s",
        [*updates.values(), Json([step]), signal_id],
    )
    if "url" in fields:
        # Число публикаций, причина «Решения платформы» (подтверждено): пересчёт в той же транзакции (#132).
        refresh_platform_decisions([signal_id], conn=cur.connection)


# Причина архива сигнала, чей материал удалили на платформе.
RETIRED_REASON = "материал удалён"


def retire(cur, signal_id: int) -> None:
    """Сигнал удаляемого материала, в архив, в транзакции удаления (cur, ревью PR #155): витрины его не показывают,
    «Отбор» не берёт, вернуть можно unarchive-signals. Выбранный «в дайджест» не трогается: MaterialInDigest."""
    cur.execute(f"SELECT {marks.signal_marked_sql('s.id', 'digest')} AS chosen FROM signals s WHERE s.id = %s"
                " FOR UPDATE", (int(signal_id),))
    found = cur.fetchone()
    if found and found["chosen"]:
        raise colleague_materials.MaterialInDigest(
            "Материал выбран «в дайджест»: сначала снимите отметку «в дайджест» на его карточке, затем удалите")
    cur.execute(
        "UPDATE signals SET archived_at = now(), archive_reason = %s,"
        " decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb, updated_at = now()"
        " WHERE id = %s AND archived_at IS NULL",
        (RETIRED_REASON, Json([{"stage": "Человек", "text": "Материал удалён на платформе: сигнал в архиве"}]),
         int(signal_id)),
    )


def _author_block(cur, signal_id: int, material: dict[str, Any]) -> None:
    """Оценка автора: блок ОС с его именем из Telegram (учётки платформы у него нет)."""
    cur.execute(
        """
        INSERT INTO signal_feedback_events (signal_id, signal_title, event_type, verdict, comment, author, created_at)
        VALUES (%s, %s, 'comment_added', %s, %s, %s, %s)
        """,
        (signal_id, material["title"], material["verdict"], material.get("verdict_comment"),
         material["tg_author"], material["created_at"]),
    )


def accumulated() -> list[dict[str, Any]]:
    """Материалы со статусом «принят», ещё не ставшие сигналом, по порядку поступления."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM colleague_materials WHERE signal_id IS NULL ORDER BY id")
        return [dict(row) for row in cur.fetchall()]


def convert_accumulated(*, apply: bool, silent: bool = False) -> dict[str, Any]:
    """Накопленные материалы: один раз через конвейер (#131). Сухой прогон: сколько их и сколько стоит «Отбор» по
    ставкам ядра, ничего не пишет. apply: сигналы и задачи «Отбора» по полосе пересчётов; silent: итог авторам не
    сообщается (SILENT). Повторный запуск находит только присланное после."""
    from oiltech_digest import otbor

    rows = accumulated()
    chars = sum(len(colleague_materials.publication_text(row)[0]) + len(row["title"]) for row in rows)
    model = config.OTBOR_MODEL or None
    price_in, price_out = config.price_for_model(model)
    tokens_in = -(-chars // _CHARS_PER_TOKEN)
    plan = {"materials": len(rows), "model": model, "input_tokens": tokens_in,
            "cost_usd": round((tokens_in * price_in + _OUTPUT_TOKENS * len(rows) * price_out) / 1_000_000, 4),
            "applied": False}
    if not apply:
        return plan
    otbor.configured_model()
    signal_ids = [to_signal(row, silent=silent) for row in rows]
    jobs = otbor.enqueue([], signal_ids=signal_ids, trigger="materials", bulk=True) if signal_ids else []
    return {**plan, "applied": True, "signal_ids": signal_ids, "jobs": len(jobs)}


# --- Итог «Отбора» автору (бот на NL) ------------------------------------------------------------

# Итог сигнала материала ключом: принят и куда, отклонён и почему, решения нет (ответ не по правилам; технический
# сбой, только когда попытки кончились или срок шага вышел: до того «Отбор» ещё повторит). Уборка людьми (#188) не итог
# «Отбора»: пока карточка в «Убрано людьми», итога нет, и автору ничего не уходит; «Вернуть» возвращает прежний итог,
# уже доставленный автору.
_OUTCOME_SQL = f"""
CASE
  WHEN s.human_removal IS NOT NULL THEN NULL
  WHEN s.otbor_decision = '{OTBOR_ACCEPT}' THEN 'accept:' || COALESCE(s.signal_category, '')
  WHEN s.otbor_decision = '{OTBOR_REJECT}' THEN 'reject:' || COALESCE(s.reject_reason, '')
  WHEN s.otbor_failure = '{otbor_store.FAILURE_INVALID}' THEN 'failed:{otbor_store.FAILURE_INVALID}'
  WHEN s.otbor_failure = '{otbor_store.FAILURE_TECHNICAL}' AND {otbor_store.signal_tries_over_sql('t')}
    THEN 'failed:{otbor_store.FAILURE_TECHNICAL}'
END"""


def pending_notices(*, limit: int = 20) -> list[dict[str, Any]]:
    """Итоги «Отбора», которых автор материала ещё не получил: куда и в ответ на что писать (чат и карточка
    материала), кому (автор) и текст. Текст: простой, разметку и упоминание добавляет бот."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            WITH counted AS ({otbor_store.SIGNAL_TRIES_SQL}),
            outcomes AS (
              SELECT m.id, m.title, m.tg_chat_id, m.tg_message_id, m.tg_user_id, m.tg_author, m.notified_outcome,
                     s.signal_category, s.reject_reason, s.verdict_reason, s.otbor_disputed, s.otbor_failure_reason,
                     {_OUTCOME_SQL} AS outcome
              FROM colleague_materials m
              JOIN signals s ON s.id = m.signal_id
              LEFT JOIN counted t ON t.id = s.id
            )
            SELECT * FROM outcomes
            WHERE outcome IS NOT NULL AND outcome IS DISTINCT FROM notified_outcome
              AND notified_outcome IS DISTINCT FROM %(silent)s
            ORDER BY id
            LIMIT %(limit)s
            """,
            {"attempts": int(config.OTBOR_ATTEMPTS), "days": int(config.OTBOR_NEW_DAYS), "limit": int(limit),
             "silent": SILENT},
        )
        rows = cur.fetchall()
    reasons = {value["code"]: value["label"] for value in load_dictionaries()["reject_reason"]["values"]}
    return [{"material_id": int(row["id"]), "outcome": row["outcome"], "chat_id": int(row["tg_chat_id"]),
             "reply_to": int(row["tg_message_id"]), "user_id": int(row["tg_user_id"]), "author": row["tg_author"],
             "text": notice_text(row, reasons)} for row in rows]


def notice_text(row: dict[str, Any], reasons: dict[str, str]) -> str:
    """Уведомление автору: итог «Отбора» по материалу коротко, с причиной у отказа. С маленькой буквы: бот ставит
    перед ним упоминание автора (в личке: делает букву заглавной)."""
    number, title = row["id"], row["title"]
    kind = str(row["outcome"]).split(":", 1)[0]
    if kind == "accept":
        text = (f"материал №{number} «{title}» взят на платформу: "
                f"{SHOWCASES.get(row['signal_category'] or '', 'витрина не определена')}.")
        if row.get("otbor_disputed"):
            text += " Решение спорное, его проверят."
        return text
    if kind == "reject":
        reason = reasons.get(row.get("reject_reason") or "", "причина не указана")
        detail = f" {row['verdict_reason']}" if row.get("verdict_reason") else ""
        return f"материал №{number} «{title}» не взят: {reason}.{detail}"
    return (f"по материалу №{number} «{title}» решения нет: {row.get('otbor_failure_reason') or 'сбой агента'}."
            " Материал сохранён на платформе.")


def mark_notified(material_id: int, outcome: str) -> bool:
    """Бот доставил итог автору: тот же итог второй раз не уходит. False: материала нет."""
    with connection.get_connection() as conn:
        cur = conn.execute(
            "UPDATE colleague_materials SET notified_outcome = %s, notified_at = now() WHERE id = %s",
            (str(outcome)[:100], int(material_id)),
        )
        conn.commit()
    return cur.rowcount > 0
