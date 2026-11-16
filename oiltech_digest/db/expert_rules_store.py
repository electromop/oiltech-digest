"""Хранилище правил эксперта (#190, спека #187, ADR 0007): правила и версии наборов агентов.

Правило (expert_rules) не правится: уточнение и сведение заводят новое, а прежние уходят из набора новой версией.
Версия набора (expert_rule_sets) хранит состав номерами правил и прежнюю версию; действующая у агента одна. Согласование
выпускает новую версию поверх действующей (номер: следующий у агента), откат делает действующей прежнюю (её номер
прежний). Нет действующей версии: версия 0, правил нет. Действующих правил у агента не больше LIMIT: согласование
сверх потолка отказывает и ждёт предложения свести правила. Новая версия действует для новых вызовов агента; прошлые
решения не пересчитываются ни при согласовании, ни при откате (решение владельца 12.11).

ВНИМАНИЕ про тесты: соединение: `connection.get_connection()` атрибутом модуля: изоляция тестов держится на подмене
этого атрибута.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg.rows import dict_row

from oiltech_digest import expert_rules
from oiltech_digest.db import connection

_RULE_FIELDS = ("id", "agent", "step", "category", "topic", "text", "replaces", "event_ids", "quote", "action_id",
                "approved_by", "created_at")


class RulesRefused(Exception):
    """Набор правил не изменён: причина словами для журнала и ответа администратору (409)."""


def _lock(conn, agent: str) -> None:
    """Замок набора агента до конца транзакции: два согласования одного агента не выпустят одну версию дважды."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"expert_rules:{agent}",))


def current_set(agent: str, *, conn=None) -> dict[str, Any] | None:
    """Действующая версия набора агента с правилами в порядке состава; None: версия 0, правил нет."""
    if conn is None:
        with connection.get_connection() as own:
            return current_set(agent, conn=own)
    cur = conn.cursor(row_factory=dict_row)
    row = cur.execute("SELECT * FROM expert_rule_sets WHERE agent = %s AND current", (agent,)).fetchone()
    if row is None:
        return None
    rules = rules_by_ids(row["rule_ids"], conn=conn)
    return {**row, "rules": [rules[int(one)] for one in row["rule_ids"] if int(one) in rules]}


def current_sets() -> dict[str, dict[str, Any]]:
    """Действующие наборы всех агентов: агент → набор (у агента без правил строки нет)."""
    with connection.get_connection() as conn:
        rows = conn.cursor(row_factory=dict_row).execute(
            "SELECT * FROM expert_rule_sets WHERE current ORDER BY agent").fetchall()
        rules = rules_by_ids([one for row in rows for one in row["rule_ids"]], conn=conn)
    return {row["agent"]: {**row, "rules": [rules[int(one)] for one in row["rule_ids"] if int(one) in rules]}
            for row in rows}


def rules_by_ids(rule_ids: Sequence[int], *, conn=None) -> dict[int, dict[str, Any]]:
    ids = sorted({int(one) for one in rule_ids})
    if not ids:
        return {}
    if conn is None:
        with connection.get_connection() as own:
            return rules_by_ids(ids, conn=own)
    rows = conn.cursor(row_factory=dict_row).execute(
        f"SELECT {', '.join(_RULE_FIELDS)} FROM expert_rules WHERE id = ANY(%s)", (ids,)).fetchall()
    return {int(row["id"]): row for row in rows}


def payload_block(agent: str) -> dict[str, Any]:
    """Блок задачи агента: действующий набор (версия и правила) и настройка контура «правило эксперта главнее
    основной инструкции»."""
    from oiltech_digest import feedback_settings

    priority = bool(feedback_settings.current()["expert_rule_priority"])
    found = current_set(agent)
    if found is None:
        return expert_rules.block(agent, 0, [], priority=priority)
    return expert_rules.block(agent, int(found["version"]), found["rules"], priority=priority)


def known_version(agent: str, value: Any) -> int | None:
    """Номер версии набора из итога воркера (недоверенный ввод): такая версия у агента есть (0: без правил); иначе
    None."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number == 0:
        return 0
    if number < 0:
        return None
    with connection.get_connection() as conn:
        row = conn.execute("SELECT 1 FROM expert_rule_sets WHERE agent = %s AND version = %s",
                           (agent, number)).fetchone()
    return number if row else None


def history(agent: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Версии набора агента, новые сверху: номер, состав, кто и когда, какое предложение, действует ли."""
    with connection.get_connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(
            "SELECT s.*, p.version AS previous_version FROM expert_rule_sets s"
            " LEFT JOIN expert_rule_sets p ON p.id = s.previous_id"
            " WHERE s.agent = %s ORDER BY s.version DESC LIMIT %s", (agent, int(limit))).fetchall()


def set_of_action(action_id: int, *, conn=None) -> dict[str, Any] | None:
    """Версия набора, которую выпустило согласование предложения."""
    sql = "SELECT * FROM expert_rule_sets WHERE action_id = %s ORDER BY id DESC LIMIT 1"
    if conn is not None:
        return conn.cursor(row_factory=dict_row).execute(sql, (int(action_id),)).fetchone()
    with connection.get_connection() as own:
        return own.cursor(row_factory=dict_row).execute(sql, (int(action_id),)).fetchone()


def apply_rule(rule: dict[str, Any], *, replaces: Sequence[int], action_id: int | None, event_ids: Sequence[int],
               quote: str | None, author: str | None, user_id: int | None, conn) -> dict[str, Any]:
    """Согласованное правило: новая версия набора агента поверх действующей, внутри транзакции вызывающего (её коммитит
    он). replaces: правила, которые новое уточняет или сводит; их в действующем наборе может уже не быть (откат, другое
    согласование): тогда отказ. Сверх LIMIT действующих правил: отказ. Номер нового правила и новой версии."""
    agent = rule["agent"]
    _lock(conn, agent)
    current = current_set(agent, conn=conn)
    rule_ids = [int(one) for one in (current or {}).get("rule_ids") or []]
    gone = [int(one) for one in replaces if int(one) not in rule_ids]
    if gone:
        raise RulesRefused("правило №" + ", №".join(map(str, gone)) + " уже не действует: набор сменился после "
                           "предложения, нужно новое предложение")
    kept = [one for one in rule_ids if one not in {int(value) for value in replaces}]
    if len(kept) + 1 > expert_rules.LIMIT:
        raise RulesRefused(f"у агента {expert_rules.agent_label(agent)} будет {len(kept) + 1} действующих правил, "
                           f"а можно не больше {expert_rules.LIMIT}: сначала сведите похожие правила в одно")
    rule_id = int(conn.execute(
        "INSERT INTO expert_rules (agent, step, category, topic, text, replaces, event_ids, quote, action_id,"
        " approved_by, user_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (agent, rule["step"], rule.get("category"), rule.get("topic"), rule["text"],
         sorted({int(one) for one in replaces}), sorted({int(one) for one in event_ids}), quote, action_id, author,
         user_id)).fetchone()[0])
    number = int(conn.execute("SELECT COALESCE(max(version), 0) + 1 FROM expert_rule_sets WHERE agent = %s",
                              (agent,)).fetchone()[0])
    if current is not None:
        conn.execute("UPDATE expert_rule_sets SET current = FALSE WHERE id = %s", (current["id"],))
    set_id = int(conn.execute(
        "INSERT INTO expert_rule_sets (agent, version, rule_ids, previous_id, current, action_id, author, user_id)"
        " VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s) RETURNING id",
        (agent, number, [*kept, rule_id], (current or {}).get("id"), action_id, author, user_id)).fetchone()[0])
    return {"rule_id": rule_id, "set_id": set_id, "version": number,
            "previous_version": int(current["version"]) if current else 0}


def rollback(set_id: int, *, conn) -> dict[str, Any]:
    """Откат: действующей снова становится версия, которая действовала до этой (у первой: версия 0, правил нет).
    Откатить можно только действующую версию. Внутри транзакции вызывающего."""
    row = conn.cursor(row_factory=dict_row).execute("SELECT * FROM expert_rule_sets WHERE id = %s",
                                                    (int(set_id),)).fetchone()
    if row is None:
        raise RulesRefused(f"версии набора №{int(set_id)} нет")
    _lock(conn, row["agent"])
    row = conn.cursor(row_factory=dict_row).execute("SELECT * FROM expert_rule_sets WHERE id = %s FOR UPDATE",
                                                    (int(set_id),)).fetchone()
    if not row["current"]:
        current = current_set(row["agent"], conn=conn)
        raise RulesRefused(f"откатить можно только действующую версию набора: сейчас действует версия "
                           f"{int(current['version']) if current else 0}, а не {int(row['version'])}")
    conn.execute("UPDATE expert_rule_sets SET current = FALSE WHERE id = %s", (row["id"],))
    previous = None
    if row["previous_id"] is not None:
        previous = conn.cursor(row_factory=dict_row).execute(
            "UPDATE expert_rule_sets SET current = TRUE WHERE id = %s RETURNING *", (row["previous_id"],)).fetchone()
    return {"agent": row["agent"], "from_version": int(row["version"]),
            "to_version": int(previous["version"]) if previous else 0}


# --- Вход разбора ОС о правилах ------------------------------------------------------------------


def window_marks(since, until, *, limit: int) -> list[dict[str, Any]]:
    """Отметки окна похожих отметок по всем карточкам (оценка, обоснование, комментарий, правки), с ролью автора, но
    без имени, и полями карточки, о которой они: новые сверху."""
    from oiltech_digest.db.feedback_store import JOURNAL_MARK_SQL

    with connection.get_connection() as conn:
        conn.execute("SET LOCAL jit = off")
        return conn.cursor(row_factory=dict_row).execute(
            f"""
            SELECT sfe.id, sfe.signal_id, sfe.created_at, sfe.verdict, sfe.reason, sfe.comment, sfe.corrected_title,
                   sfe.corrected_thesis, sfe.correction_undone_at, u.role, COALESCE(s.title_ru, s.title) AS card_title,
                   s.signal_category, s.theme, s.score_band, s.otbor_decision, s.technology_maturity
            FROM signal_feedback_events sfe
            LEFT JOIN users u ON u.id = sfe.user_id
            JOIN signals s ON s.id = sfe.signal_id
            WHERE sfe.created_at > %s AND sfe.created_at <= %s AND {JOURNAL_MARK_SQL}
            ORDER BY sfe.created_at DESC, sfe.id DESC
            LIMIT %s
            """, (since, until, int(limit))).fetchall()


def taken_rule_texts(proposal_kind: str) -> list[tuple[str, str]]:
    """(агент, текст) правил, которые уже действовали или уже предложены (ждут решения, убраны, применены): их не
    предлагают снова."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            "SELECT details->'rule'->>'agent', details->'rule'->>'text' FROM feedback_actions"
            " WHERE kind = %s AND status IN ('pending', 'dismissed', 'applied')"
            " UNION SELECT agent, text FROM expert_rules", (proposal_kind,)).fetchall()
    return [(str(agent or ""), str(text)) for agent, text in rows if text]
