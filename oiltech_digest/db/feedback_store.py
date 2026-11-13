"""Хранилище журнала ОС (#189, спека #187): действия ОС, недельные срезы, пары «одно ли событие» от людей и выборки
отметок для журнала и разбора ОС.

Отметки остаются в журнале отметок (signal_feedback_events); действия по ним: feedback_actions (вид, статус,
основание: номера отметок, цитата, агенты, что сделано или предложено, автор, время; parent_id: путь действия).
Отметка в журнале: запись блока ОС (оценка, причина, комментарий, неприменённая правка или «Дубль») или применённая
правка заголовка и сути (у неё нет оценки, но это явное действие администратора).

ВНИМАНИЕ про тесты: соединение: `connection.get_connection()` атрибутом модуля: изоляция тестов держится на подмене
этого атрибута.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest.db import connection
from oiltech_digest.db.repository import _FEEDBACK_IS_BLOCK_SQL

# Виды действий ОС.
IMMEDIATE, PROPOSAL, DECISION, APPLIED, UNDO, SAMPLE_TASK = (
    "immediate", "signal_proposal", "decision", "applied", "undo", "sample_task")
# Строки журнала верхнего уровня: отметки (из журнала отметок), предложения, задачи выборки и явные действия
# администратора без отметки («Вернуть» в «Отклонённых»); «сразу» по отметке идёт в её строке.
TOP_KINDS = (PROPOSAL, SAMPLE_TASK, IMMEDIATE)
# Статусы.
DONE, PENDING, APPROVED, MODIFIED, DISMISSED, APPLIED_STATUS, UNDONE, STALE = (
    "done", "pending", "approved", "modified", "dismissed", "applied", "undone", "stale")
STATUSES = (DONE, PENDING, APPROVED, MODIFIED, DISMISSED, APPLIED_STATUS, UNDONE, STALE)
# Агенты, к которым относится отметка или действие (фильтр журнала).
AGENTS = ("otbor", "summary", "card", "scores", "dedup")
# Оценки шкалы, которые говорят о балле карточки: их расхождение с баллом платформы идёт в срез (решение 6 гриля).
RATING_VERDICTS = ("strong_signal", "approved", "watch_later", "background_material", "reject", "overrated",
                   "needs_better_source", "bad_translation", "too_generic")

_HAS_CORRECTION_SQL = ("(btrim(COALESCE(sfe.corrected_title, '')) <> ''"
                       " OR btrim(COALESCE(sfe.corrected_thesis, '')) <> '')")
# Отметка в журнале ОС: запись блока ОС или применённая правка заголовка и сути.
JOURNAL_MARK_SQL = (f"({_FEEDBACK_IS_BLOCK_SQL} OR (sfe.event_type = 'comment_added' AND sfe.applied"
                    f" AND {_HAS_CORRECTION_SQL}))")
# Агенты отметки: по оценке и правкам.
MARK_AGENTS_SQL = (
    "array_remove(ARRAY["
    "CASE WHEN sfe.verdict IN ('wrong_block', 'wrong_domain') THEN 'otbor' END, "
    "CASE WHEN sfe.verdict = 'merge_duplicate' THEN 'dedup' END, "
    f"CASE WHEN sfe.verdict IN ({', '.join(repr(one) for one in RATING_VERDICTS)}) THEN 'scores' END, "
    f"CASE WHEN {_HAS_CORRECTION_SQL} THEN 'summary' END]::text[], NULL)"
)
_AUTHOR_SQL = "COALESCE(u.email, sfe.author)"
_MARK_FIELDS = ("sfe.id, sfe.signal_id, sfe.created_at, sfe.verdict, sfe.reason, sfe.comment, sfe.corrected_title,"
                " sfe.corrected_thesis, sfe.duplicate_of_signal_id, sfe.applied, sfe.correction_undone_at, u.role")


# --- Действия ОС ---------------------------------------------------------------------------------


def create_action(kind: str, status: str, *, signal_id: int | None = None, parent_id: int | None = None,
                  slice_id: int | None = None, event_ids: Sequence[int] = (), quote: str | None = None,
                  agents: Sequence[str] = (), details: dict[str, Any] | None = None, author: str | None = None,
                  user_id: int | None = None, conn=None) -> int:
    """Записать действие ОС; номер. conn: внутри чужой транзакции (её коммитит тот, кто её открыл)."""
    sql = ("INSERT INTO feedback_actions (kind, status, signal_id, parent_id, slice_id, event_ids, quote, agents,"
           " details, author, user_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id")
    params = (kind, status, signal_id, parent_id, slice_id, sorted({int(one) for one in event_ids}), quote,
              sorted(set(agents)), Json(details or {}), author, user_id)
    if conn is not None:
        return int(conn.execute(sql, params).fetchone()[0])
    with connection.get_connection() as own:
        action_id = int(own.execute(sql, params).fetchone()[0])
        own.commit()
    return action_id


def get_action(action_id: int, *, for_update: bool = False, conn=None) -> dict[str, Any] | None:
    sql = "SELECT * FROM feedback_actions WHERE id = %s" + (" FOR UPDATE" if for_update else "")
    if conn is not None:
        return conn.cursor(row_factory=dict_row).execute(sql, (int(action_id),)).fetchone()
    with connection.get_connection() as own:
        return own.cursor(row_factory=dict_row).execute(sql, (int(action_id),)).fetchone()


def set_status(action_id: int, status: str, *, details: dict[str, Any] | None = None, conn) -> None:
    """Сменить статус действия; details дописываются поверх прежних."""
    conn.execute("UPDATE feedback_actions SET status = %s, details = details || %s::jsonb, updated_at = now()"
                 " WHERE id = %s",
                 (status, Json(details or {}), int(action_id)))


def children(parent_ids: Sequence[int]) -> dict[int, list[dict[str, Any]]]:
    """Шаги пути действий: решения, применения и отмены по родителю, по порядку."""
    ids = sorted({int(one) for one in parent_ids})
    found: dict[int, list[dict[str, Any]]] = {one: [] for one in ids}
    if not ids:
        return found
    with connection.get_connection() as conn:
        rows = conn.cursor(row_factory=dict_row).execute(
            "SELECT * FROM feedback_actions WHERE parent_id = ANY(%s) ORDER BY created_at, id", (ids,)).fetchall()
    for row in rows:
        found[int(row["parent_id"])].append(row)
    return found


def proposals_for_events(event_ids: Sequence[int]) -> dict[int, list[dict[str, Any]]]:
    """Предложения, основанные на отметках: номер отметки → предложения (номер и статус)."""
    ids = sorted({int(one) for one in event_ids})
    found: dict[int, list[dict[str, Any]]] = {}
    if not ids:
        return found
    with connection.get_connection() as conn:
        rows = conn.cursor(row_factory=dict_row).execute(
            f"SELECT id, status, event_ids, created_at FROM feedback_actions WHERE kind = '{PROPOSAL}'"
            " AND event_ids && %s::bigint[] ORDER BY id", (ids,)).fetchall()
    for row in rows:
        for event_id in row["event_ids"]:
            if int(event_id) in ids:
                found.setdefault(int(event_id), []).append({"id": int(row["id"]), "status": row["status"]})
    return found


def pending_proposal(signal_id: int, *, conn) -> dict[str, Any] | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT * FROM feedback_actions WHERE signal_id = %s AND kind = '{PROPOSAL}' AND status = '{PENDING}'"
        " ORDER BY id DESC LIMIT 1 FOR UPDATE", (int(signal_id),)).fetchone()


def latest_with_change(signal_id: int, field: str) -> dict[str, Any] | None:
    """Последнее неотменённое явное действие или применение улучшения с изменением поля (например, «Убрано
    людьми»)."""
    with connection.get_connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(
            f"SELECT * FROM feedback_actions WHERE signal_id = %s AND ((kind = '{IMMEDIATE}' AND status = '{DONE}')"
            f" OR (kind = '{APPLIED}' AND status = '{APPLIED_STATUS}')) AND details->'changes' @> %s::jsonb"
            " ORDER BY id DESC LIMIT 1", (int(signal_id), Json([{"field": field}]))).fetchone()


def expire_pending(*, kinds: Sequence[str], older_than_days: int) -> int:
    """Предложения и задачи без решения дольше срока: «устарело», остаются в журнале."""
    with connection.get_connection() as conn:
        count = conn.execute(
            f"UPDATE feedback_actions SET status = '{STALE}', updated_at = now(),"
            " details = details || jsonb_build_object('stale_reason', %s::text)"
            f" WHERE kind = ANY(%s) AND status = '{PENDING}' AND created_at < now() - make_interval(days => %s)",
            (f"без решения дольше {int(older_than_days)} дней", list(kinds), int(older_than_days))).rowcount
        conn.commit()
    return int(count or 0)


def task_in_month(kind: str, month: str) -> bool:
    """Задача этого вида за месяц (ГГГГ-ММ по Москве) уже есть."""
    with connection.get_connection() as conn:
        return conn.execute("SELECT 1 FROM feedback_actions WHERE kind = %s AND details->>'month' = %s LIMIT 1",
                            (kind, month)).fetchone() is not None


# --- Недельные срезы -----------------------------------------------------------------------------


def create_slice(*, trigger: str, status: str, window_from: datetime, window_to: datetime,
                 week_start=None, signals: int = 0, note: str | None = None, author: str | None = None) -> int | None:
    """Завести срез; None: срез по расписанию на эту неделю уже есть (не дважды за неделю)."""
    with connection.get_connection() as conn:
        row = conn.execute(
            "INSERT INTO feedback_slices (trigger, status, window_from, window_to, week_start, signals, note, author,"
            " finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s = 'running' THEN NULL ELSE now() END)"
            " ON CONFLICT (week_start) WHERE trigger = 'schedule' DO NOTHING RETURNING id",
            (trigger, status, window_from, window_to, week_start, int(signals), note, author, status)).fetchone()
        conn.commit()
    return int(row[0]) if row else None


def attach_job(slice_id: int, job_id: int) -> None:
    with connection.get_connection() as conn:
        conn.execute("UPDATE feedback_slices SET job_id = %s WHERE id = %s", (int(job_id), int(slice_id)))
        conn.commit()


def finish_slice(slice_id: int, *, status: str, note: str | None = None) -> None:
    with connection.get_connection() as conn:
        conn.execute("UPDATE feedback_slices SET status = %s, note = COALESCE(%s, note), finished_at = now()"
                     " WHERE id = %s", (status, note, int(slice_id)))
        conn.commit()


def add_proposals(slice_id: int, count: int, *, conn) -> None:
    conn.execute("UPDATE feedback_slices SET proposals = proposals + %s WHERE id = %s", (int(count), int(slice_id)))


# Срез «идёт», а его задача упала, пропала из очереди (ON DELETE SET NULL) или так и не поставлена (процесс
# остановился между заведением среза и постановкой задачи): срез не выполнен, его окно возьмёт следующий.
_SLICE_FAILED_SQL = ("(f.status = 'running' AND (j.status = 'failed' OR (f.job_id IS NULL"
                     " AND f.created_at < now() - interval '10 minutes')))")
_SLICE_NOTE_SQL = (f"CASE WHEN {_SLICE_FAILED_SQL} THEN COALESCE(j.error_message, 'задача разбора ОС не поставлена или"
                   " пропала из очереди') ELSE f.note END")


def slices(*, limit: int = 10) -> list[dict[str, Any]]:
    """Последние срезы; у среза, чья задача не выполнена, статус «не выполнен» (только чтение)."""
    with connection.get_connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(
            f"""
            SELECT f.id, f.trigger, CASE WHEN {_SLICE_FAILED_SQL} THEN 'failed' ELSE f.status END AS status,
                   f.window_from, f.window_to, f.week_start, f.job_id, f.signals, f.proposals,
                   {_SLICE_NOTE_SQL} AS note, f.author, f.created_at, f.finished_at
            FROM feedback_slices f LEFT JOIN background_jobs j ON j.id = f.job_id
            WHERE f.trigger <> 'seed'
            ORDER BY f.id DESC
            LIMIT %s
            """, (int(limit),)).fetchall()


def settle_failed_slices() -> None:
    """Записать «не выполнен» срезам, чья задача не выполнена: иначе «идёт» держал бы следующие срезы."""
    with connection.get_connection() as conn:
        # Целевая строка f и её копия s с задачей j: в условии и причине поля f (той же строки).
        conn.execute(
            f"UPDATE feedback_slices f SET status = 'failed', finished_at = now(), note = {_SLICE_NOTE_SQL}"
            f" FROM feedback_slices s LEFT JOIN background_jobs j ON j.id = s.job_id"
            f" WHERE s.id = f.id AND {_SLICE_FAILED_SQL}")
        conn.commit()


def db_now() -> datetime:
    """Часы базы: по ним отметки получают время записи, по ним же считается окно среза (часы приложения могут
    расходиться с ними на доли секунды, и свежая отметка выпала бы из окна)."""
    with connection.get_connection() as conn:
        return conn.execute("SELECT now()").fetchone()[0]


def last_done_window_end() -> datetime | None:
    """Конец окна последнего выполненного среза (или начальной строки seed): начало окна следующего."""
    with connection.get_connection() as conn:
        row = conn.execute("SELECT max(window_to) FROM feedback_slices WHERE status = 'done'").fetchone()
    return row[0] if row else None


def running_slice() -> dict[str, Any] | None:
    settle_failed_slices()
    with connection.get_connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(
            "SELECT * FROM feedback_slices WHERE status = 'running' ORDER BY id DESC LIMIT 1").fetchone()


def scheduled_this_week(week_start) -> bool:
    with connection.get_connection() as conn:
        return conn.execute("SELECT 1 FROM feedback_slices WHERE trigger = 'schedule' AND week_start = %s",
                            (week_start,)).fetchone() is not None


def month_spend(stages: Sequence[str], since: datetime) -> float:
    """Расход контура ОС в учёте расхода ИИ с начала месяца, $."""
    with connection.get_connection() as conn:
        row = conn.execute("SELECT COALESCE(sum(cost_usd), 0) FROM ai_processing_runs WHERE stage = ANY(%s)"
                           " AND created_at >= %s", (list(stages), since)).fetchone()
    return float(row[0] or 0)


# --- Отметки для разбора ОС ----------------------------------------------------------------------


def signals_with_new_marks(window_from: datetime, window_to: datetime) -> list[int]:
    """Сигналы с новыми записями блока ОС в окне среза (оценка, причина, комментарий, неприменённая правка или
    «Дубль»): склеенные дубли и отметки без карточки мимо."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT sfe.signal_id FROM signal_feedback_events sfe JOIN signals s ON s.id = sfe.signal_id"
            f" WHERE sfe.created_at > %s AND sfe.created_at <= %s AND {_FEEDBACK_IS_BLOCK_SQL}"
            " AND s.merged_into_signal_id IS NULL ORDER BY 1", (window_from, window_to)).fetchall()
    return [int(row[0]) for row in rows]


def signal_marks(signal_ids: Sequence[int], since: datetime, until: datetime) -> dict[int, list[dict[str, Any]]]:
    """Отметки сигналов за окно похожих отметок, без имён авторов: роль (администратор или участник), оценка, причина,
    комментарий, правки, «Дубль», применена ли отметка и не отменена ли её правка."""
    ids = sorted({int(one) for one in signal_ids})
    found: dict[int, list[dict[str, Any]]] = {one: [] for one in ids}
    if not ids:
        return found
    with connection.get_connection() as conn:
        rows = conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_MARK_FIELDS} FROM signal_feedback_events sfe LEFT JOIN users u ON u.id = sfe.user_id"
            f" WHERE sfe.signal_id = ANY(%s) AND sfe.created_at > %s AND sfe.created_at <= %s AND {JOURNAL_MARK_SQL}"
            " ORDER BY sfe.signal_id, sfe.created_at, sfe.id", (ids, since, until)).fetchall()
    for row in rows:
        found[int(row["signal_id"])].append(row)
    return found


def marks_by_id(event_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Отметки по номерам, с автором: основание предложений в журнале."""
    ids = sorted({int(one) for one in event_ids})
    if not ids:
        return {}
    with connection.get_connection() as conn:
        rows = conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_MARK_FIELDS}, {_AUTHOR_SQL} AS author FROM signal_feedback_events sfe"
            " LEFT JOIN users u ON u.id = sfe.user_id WHERE sfe.id = ANY(%s)", (ids,)).fetchall()
    return {int(row["id"]): row for row in rows}


# --- Журнал --------------------------------------------------------------------------------------


# Витрина карточки так, как её видят экраны: находка поиска без категории живёт на радаре (_radar_quality_sql).
SHOWCASE_SQL = "COALESCE(s.signal_category, CASE WHEN s.origin = 'radar' THEN 'technology' END)"
# Статус отметки в журнале: у отметки с явным действием администратора: его статус; у отметки участника после выката
# одной формы ОС (history_end): статус предложения, в которое она вошла, а до среза (slice_end) «ждёт решения»; у
# остальных: «сделано».
_MARK_STATUS_SQL = (
    "CASE WHEN im.id IS NOT NULL THEN im.status"
    " WHEN NOT sfe.applied AND sfe.created_at > COALESCE(%(history_end)s::timestamptz, '-infinity')"
    f" THEN COALESCE(pr.status, CASE WHEN sfe.created_at > COALESCE(%(slice_end)s::timestamptz, '-infinity')"
    f" THEN '{PENDING}' ELSE '{DONE}' END)"
    f" ELSE '{DONE}' END"
)


def slice_bounds() -> tuple[datetime | None, datetime | None]:
    """(конец истории: окно seed, выкат одной формы ОС; конец окна последнего выполненного среза)."""
    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT max(window_to) FILTER (WHERE trigger = 'seed'), max(window_to) FILTER (WHERE status = 'done')"
            " FROM feedback_slices").fetchone()
    return (row[0], row[1]) if row else (None, None)


def journal_marks(*, status: str | None, showcase: str | None, agent: str | None, author: str | None,
                  signal_id: int | None, limit: int, history_end: datetime | None,
                  slice_end: datetime | None) -> list[dict[str, Any]]:
    """Отметки журнала ОС, новые сверху: с явным действием администратора по отметке (im_*), если оно было, и
    статусом последнего предложения, в которое отметка вошла (proposal_status)."""
    params: dict[str, Any] = {"history_end": history_end, "slice_end": slice_end, "limit": int(limit)}
    clauses = [JOURNAL_MARK_SQL]
    if status:
        clauses.append(f"({_MARK_STATUS_SQL}) = %(status)s")
        params["status"] = status
    if showcase:
        clauses.append(f"{SHOWCASE_SQL} = %(showcase)s")
        params["showcase"] = showcase
    if agent:
        clauses.append(f"%(agent)s = ANY({MARK_AGENTS_SQL})")
        params["agent"] = agent
    if author:
        clauses.append(f"{_AUTHOR_SQL} = %(author)s")
        params["author"] = author
    if signal_id is not None:
        clauses.append("sfe.signal_id = %(signal_id)s")
        params["signal_id"] = int(signal_id)
    with connection.get_connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(
            f"""
            SELECT {_MARK_FIELDS}, {_AUTHOR_SQL} AS author, {MARK_AGENTS_SQL} AS agents,
                   COALESCE(s.title_ru, s.title) AS signal_title, {SHOWCASE_SQL} AS showcase,
                   {_FEEDBACK_IS_BLOCK_SQL} AS is_block, {_MARK_STATUS_SQL} AS status, pr.status AS proposal_status,
                   im.id AS im_id, im.status AS im_status, im.details AS im_details
            FROM signal_feedback_events sfe
            LEFT JOIN users u ON u.id = sfe.user_id
            LEFT JOIN signals s ON s.id = sfe.signal_id
            LEFT JOIN LATERAL (
              SELECT fa.id, fa.status, fa.details FROM feedback_actions fa
              WHERE fa.kind = '{IMMEDIATE}' AND fa.event_ids @> ARRAY[sfe.id]
              ORDER BY fa.id DESC LIMIT 1
            ) im ON TRUE
            LEFT JOIN LATERAL (
              SELECT fa.status FROM feedback_actions fa
              WHERE fa.kind = '{PROPOSAL}' AND fa.event_ids @> ARRAY[sfe.id]
              ORDER BY fa.id DESC LIMIT 1
            ) pr ON TRUE
            WHERE {' AND '.join(clauses)}
            ORDER BY sfe.created_at DESC, sfe.id DESC
            LIMIT %(limit)s
            """, params).fetchall()


def journal_actions(*, status: str | None, showcase: str | None, agent: str | None, author: str | None,
                    signal_id: int | None, kinds: Sequence[str], limit: int) -> list[dict[str, Any]]:
    """Предложения, задачи выборки и явные действия без отметки журнала ОС, новые сверху. Автор у предложения: автор
    любой отметки основания, у явного действия: администратор."""
    clauses, params = [f"fa.kind = ANY(%s) AND (fa.kind <> '{IMMEDIATE}' OR cardinality(fa.event_ids) = 0)"], [list(kinds)]
    if status:
        clauses.append("fa.status = %s")
        params.append(status)
    if showcase:
        clauses.append(f"{SHOWCASE_SQL} = %s")
        params.append(showcase)
    if agent:
        clauses.append("%s = ANY(fa.agents)")
        params.append(agent)
    if author:
        clauses.append("(fa.author = %s OR EXISTS (SELECT 1 FROM signal_feedback_events sfe LEFT JOIN users u"
                       f" ON u.id = sfe.user_id WHERE sfe.id = ANY(fa.event_ids) AND {_AUTHOR_SQL} = %s))")
        params += [author, author]
    if signal_id is not None:
        clauses.append("fa.signal_id = %s")
        params.append(int(signal_id))
    with connection.get_connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(
            f"""
            SELECT fa.*, COALESCE(s.title_ru, s.title) AS signal_title, {SHOWCASE_SQL} AS showcase
            FROM feedback_actions fa
            LEFT JOIN signals s ON s.id = fa.signal_id
            WHERE {' AND '.join(clauses)}
            ORDER BY fa.created_at DESC, fa.id DESC
            LIMIT %s
            """, (*params, int(limit))).fetchall()


def mark_authors() -> list[str]:
    """Авторы отметок журнала: варианты фильтра «по автору»."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {_AUTHOR_SQL} FROM signal_feedback_events sfe LEFT JOIN users u ON u.id = sfe.user_id"
            f" WHERE {JOURNAL_MARK_SQL} AND {_AUTHOR_SQL} IS NOT NULL ORDER BY 1").fetchall()
    return [str(row[0]) for row in rows]


# --- Пары «одно ли событие» ----------------------------------------------------------------------


def write_pair(signal_id: int, other_signal_id: int, *, same_event: bool, source: str, action_id: int | None,
               author: str | None, conn) -> None:
    conn.execute(
        "INSERT INTO signal_event_pairs (signal_id, other_signal_id, same_event, source, action_id, author)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (int(signal_id), int(other_signal_id), bool(same_event), source, action_id, author))


# --- Карточки для разбора ОС ---------------------------------------------------------------------


def review_cards(signal_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Поля карточки так, как их видят люди (с применёнными правками заголовка и сути): вход разбора ОС и «было» в
    предложении."""
    from oiltech_digest.db.repository import _signal_corrections_lateral, apply_signal_corrections

    ids = sorted({int(one) for one in signal_ids})
    if not ids:
        return {}
    with connection.get_connection() as conn:
        rows = conn.cursor(row_factory=dict_row).execute(
            f"""
            SELECT s.id, s.origin, s.title, s.title_ru, s.summary, s.signal_category, s.theme, s.event_type,
                   s.technology_maturity, s.event_date, s.event_date_precision, s.event_countries, s.region,
                   s.technology_class, s.participants_json, s.merged_into_signal_id, s.otbor_decision,
                   corr.corrected_title, corr.corrected_thesis
            FROM signals s
            {_signal_corrections_lateral("s")}
            WHERE s.id = ANY(%s)
            """, (ids,)).fetchall()
    return {int(row["id"]): apply_signal_corrections(row) for row in rows}


def record_usage(job_id: int | None, rows: Sequence[dict[str, Any]], *, stage: str) -> None:
    """Расход разбора ОС в учёте расхода ИИ своей стадией, строка на (задача, сигнал): повторное применение итога той
    же задачи счёт не удваивает."""
    if not rows:
        return
    with connection.get_connection() as conn:
        for row in rows:
            conn.execute(
                "INSERT INTO ai_processing_runs (job_id, signal_id, stage, provider, model, input_tokens, output_tokens,"
                " total_tokens, cost_usd, status) VALUES (%s, %s, %s, 'openai', %s, %s, %s, %s, %s, 'ok')"
                " ON CONFLICT (job_id, signal_id, stage) WHERE signal_id IS NOT NULL DO NOTHING",
                (job_id, row["signal_id"], stage, row.get("model"), row["input_tokens"], row["output_tokens"],
                 row["input_tokens"] + row["output_tokens"], row["cost_usd"]))
        conn.commit()


def rejected_by_otbor(since: datetime, *, seed: str, limit: int = 600) -> list[dict[str, Any]]:
    """Публикации, отклонённые «Отбором» с даты поступления (раздел отклонённого «Отбора», без убранного людьми): пул
    ежемесячной выборки на просмотр эксперту. Случайно по всему месяцу и воспроизводимо: порядок по хэшу seed и
    номера, а не первые по номеру (отклонённых за месяц бывают тысячи)."""
    from oiltech_digest.db.rejected_sections import _NOT_SHOWN_SQL
    from oiltech_digest.feed_window import OTBOR_REJECT

    with connection.get_connection() as conn:
        conn.execute("SET LOCAL jit = off")
        return conn.cursor(row_factory=dict_row).execute(
            f"""
            SELECT s.id, s.origin, COALESCE(s.title_ru, s.title) AS title, s.title AS original_title, s.theme,
                   s.summary, s.reject_reason, s.verdict_reason, s.first_seen_at,
                   (SELECT e.source_url FROM signal_evidence e WHERE e.signal_id = s.id ORDER BY e.id LIMIT 1) AS url
            FROM signals s
            WHERE {_NOT_SHOWN_SQL} AND s.otbor_decision = '{OTBOR_REJECT}' AND s.human_removal IS NULL
              AND s.first_seen_at >= %s
            ORDER BY md5(%s || ':' || s.id::text), s.id
            LIMIT %s
            """, (since, seed, int(limit))).fetchall()
