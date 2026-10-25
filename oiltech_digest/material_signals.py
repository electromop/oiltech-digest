"""Материал коллеги — сигнал третьего происхождения (#131, спека #128): тот же конвейер, что у статей и находок.

- При включённом «Отборе» материал при записи сразу становится сигналом происхождения material с меткой пути
  «Отбора». Поля автора — заголовок, описание (суть), компании (участники) — с отметкой «человек»: агенты их не
  переписывают и заполняют только недостающее. Оценка автора — блок ОС с его именем из Telegram: на решение «Отбора»
  и показ она не влияет (решение владельца 22.10).
- Ссылка материала — его публикация, только если адрес ещё ничей: прикрепления к существующему сигналу нет,
  материал — свой сигнал и виден в витрине и без ссылки.
- Накопленные материалы (присланы до включения, статус «принят») проходят конвейер один раз: convert_accumulated
  (команда otbor-materials, сухой прогон по умолчанию).
- Итог «Отбора» сообщается автору: ядро считает итог по сигналу (pending_notices), бот на NL отвечает в чате
  материала на его карточку и отмечает доставленное (mark_notified). Новый итог (человек вернул отклонённое) — новое
  уведомление.

ВНИМАНИЕ про тесты: соединение — `connection.get_connection()` атрибутом модуля (как в colleague_materials).
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest import colleague_materials, config
from oiltech_digest.db import connection, otbor_store
from oiltech_digest.db.repository import (CARD_SET_BY_HUMAN, SIGNAL_ORIGIN_MATERIAL, SIGNAL_SELECTOR_OTBOR,
                                          SYSTEM_TAG_UNCLASSIFIED)
from oiltech_digest.feed_window import OTBOR_ACCEPT, OTBOR_REJECT
from oiltech_digest.otbor_pack_format import load_dictionaries

KEY_PREFIX = "material:"
# Витрина по категории «Отбора» — как её называет уведомление автору.
SHOWCASES = {"business": "бизнес-лента", "technology": "технологический радар",
             "other": "раздел «Другое», он виден администратору"}
# Оценка цены накопленных до вызова: русский текст — около трёх знаков на токен, ответ «Отбора» — около 300 токенов.
_CHARS_PER_TOKEN, _OUTPUT_TOKENS = 3, 300


def to_signal(material: dict[str, Any]) -> int:
    """Сигнал материала: заголовок, суть — описание, участники — компании автора (всё — «человек»), блок ОС с оценкой
    автора, ссылка — если адрес ничей. Повтор — тот же сигнал (ключ по номеру материала). Номер сигнала."""
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
            if material.get("url"):
                # Адрес, который уже держит другая карточка, к материалу не переходит: он свой сигнал.
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
            cur.execute("UPDATE signals SET evidence_count = (SELECT count(*) FROM signal_evidence WHERE signal_id = %s)"
                        " WHERE id = %s", (signal_id, signal_id))
        cur.execute("UPDATE colleague_materials SET signal_id = %s WHERE id = %s AND signal_id IS NULL",
                    (signal_id, int(material["id"])))
        conn.commit()
    return signal_id


def _author_block(cur, signal_id: int, material: dict[str, Any]) -> None:
    """Оценка автора — блок ОС с его именем из Telegram (учётки платформы у него нет)."""
    cur.execute(
        """
        INSERT INTO signal_feedback_events (signal_id, signal_title, event_type, verdict, comment, author, created_at)
        VALUES (%s, %s, 'comment_added', %s, %s, %s, %s)
        """,
        (signal_id, material["title"], material["verdict"], material.get("verdict_comment"),
         material["tg_author"], material["created_at"]),
    )


def accumulated() -> list[dict[str, Any]]:
    """Материалы со статусом «принят», ещё не ставшие сигналом, — по порядку поступления."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM colleague_materials WHERE signal_id IS NULL ORDER BY id")
        return [dict(row) for row in cur.fetchall()]


def convert_accumulated(*, apply: bool) -> dict[str, Any]:
    """Накопленные материалы — один раз через конвейер (#131). Сухой прогон: сколько их и сколько стоит «Отбор» по
    ставкам ядра, ничего не пишет. apply: сигналы и задачи «Отбора» по полосе пересчётов; повторный запуск находит
    только присланное после."""
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
    if not config.OTBOR_ENABLED:
        raise otbor.OtborSetupError("OTBOR_ENABLED выключен: накопленные материалы идут через «Отбор» после включения")
    otbor.configured_model()
    signal_ids = [to_signal(row) for row in rows]
    jobs = otbor.enqueue([], signal_ids=signal_ids, trigger="materials", bulk=True) if signal_ids else []
    return {**plan, "applied": True, "signal_ids": signal_ids, "jobs": len(jobs)}


# --- Итог «Отбора» автору (бот на NL) ------------------------------------------------------------

# Итог сигнала материала ключом: принят и куда, отклонён и почему, решения нет (ответ не по правилам; технический
# сбой — только когда попытки кончились или срок шага вышел: до того «Отбор» ещё повторит).
_OUTCOME_SQL = f"""
CASE
  WHEN s.otbor_decision = '{OTBOR_ACCEPT}' THEN 'accept:' || COALESCE(s.signal_category, '')
  WHEN s.otbor_decision = '{OTBOR_REJECT}' THEN 'reject:' || COALESCE(s.reject_reason, '')
  WHEN s.otbor_failure = '{otbor_store.FAILURE_INVALID}' THEN 'failed:{otbor_store.FAILURE_INVALID}'
  WHEN s.otbor_failure = '{otbor_store.FAILURE_TECHNICAL}' AND {otbor_store.signal_tries_over_sql('t')}
    THEN 'failed:{otbor_store.FAILURE_TECHNICAL}'
END"""


def pending_notices(*, limit: int = 20) -> list[dict[str, Any]]:
    """Итоги «Отбора», которых автор материала ещё не получил: куда и в ответ на что писать (чат и карточка
    материала), кому (автор) и текст. Текст — простой, разметку и упоминание добавляет бот."""
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
            ORDER BY id
            LIMIT %(limit)s
            """,
            {"attempts": int(config.OTBOR_ATTEMPTS), "days": int(config.OTBOR_NEW_DAYS), "limit": int(limit)},
        )
        rows = cur.fetchall()
    reasons = {value["code"]: value["label"] for value in load_dictionaries()["reject_reason"]["values"]}
    return [{"material_id": int(row["id"]), "outcome": row["outcome"], "chat_id": int(row["tg_chat_id"]),
             "reply_to": int(row["tg_message_id"]), "user_id": int(row["tg_user_id"]), "author": row["tg_author"],
             "text": notice_text(row, reasons)} for row in rows]


def notice_text(row: dict[str, Any], reasons: dict[str, str]) -> str:
    """Уведомление автору: итог «Отбора» по материалу коротко, с причиной у отказа. С маленькой буквы: бот ставит
    перед ним упоминание автора (в личке — делает букву заглавной)."""
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
    """Бот доставил итог автору: тот же итог второй раз не уходит. False — материала нет."""
    with connection.get_connection() as conn:
        cur = conn.execute(
            "UPDATE colleague_materials SET notified_outcome = %s, notified_at = now() WHERE id = %s",
            (str(outcome)[:100], int(material_id)),
        )
        conn.commit()
    return cur.rowcount > 0
