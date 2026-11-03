"""Хранилище проверки «Отбора» на реальных данных (#134, спека #128): решения рядом, в своей таблице
(otbor_shadow_decisions), а не в сигнале публикации. Витрины, лента статей и шаги дальше «Отбора» их не видят;
переключение (otbor_shadow.switch) переносит решение в сигнал статьи тем же путём, что пишет сам «Отбор».

Проверка берёт статьи ленты, которые решил гейт до «Отбора», и только те, что «Отбор» вправе пересмотреть (спека:
не старше окна, не в закрытом месяце, без решения человека). Решение человека у статьи: отметка (кроме «новой» без
комментария), отзыв, блок ОС по статье или её сигналу ленты, старая личная отметка, черновик выпуска (правило
владельца 02.11 «решения людей беречь всегда»). Перенос перепроверяет это у каждой статьи в своей транзакции: за время
с проверки и за время самого переноса человек мог её отметить, а месяц закрыться.

ВНИМАНИЕ про тесты: соединение — `connection.get_connection()` атрибутом модуля (как в otbor_store): изоляция тестов
держится на подмене этого атрибута.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest import feed_window
from oiltech_digest.db import connection, feed_signals, marks
from oiltech_digest.db.otbor_store import DECIDED, FAILURE_TECHNICAL, KIND, _signal_for_update, _write_decision
from oiltech_digest.db.repository import _RADAR_VISIBLE_SQL
from oiltech_digest.db.signal_card_store import KIND as CARD_KIND

# Пометка запуска проверки #134.
TRIGGER = "shadow"
# Стадия расхода проверки в ai_processing_runs: тот же агент, но оплата проверки видна отдельно.
STAGE = "otbor_shadow"
# Шаг «Хода решения» у карточки, которую перенос делает заново.
CARD_AGAIN = {"stage": "«Карточка»", "text": "Карточка будет сделана заново: сделана до решения «Отбора»"}


def human_decided_sql(article: str = "a") -> str:
    """У статьи есть решение человека: отметка, кроме «новой» без комментария; отзыв; блок ОС по статье или её
    сигналу ленты; старая личная отметка; черновик выпуска. «В дайджест» — отметка, поэтому тоже здесь."""
    return (
        f"(EXISTS (SELECT 1 FROM article_marks hm WHERE hm.article_id = {article}.id"
        "   AND (hm.status <> 'new' OR COALESCE(btrim(hm.analyst_comment), '') <> ''))"
        f" OR EXISTS (SELECT 1 FROM feedback_entries hf WHERE hf.article_id = {article}.id)"
        f" OR EXISTS (SELECT 1 FROM signal_feedback_events he WHERE he.article_id = {article}.id"
        f"   OR he.signal_id IN (SELECT hs.id FROM signals hs WHERE hs.signal_key = 'feed:' || {article}.id))"
        f" OR EXISTS (SELECT 1 FROM user_article_states hu WHERE hu.article_id = {article}.id"
        "   AND (hu.status <> 'new' OR COALESCE(btrim(hu.analyst_comment), '') <> ''))"
        f" OR EXISTS (SELECT 1 FROM monthly_digest_items hd WHERE hd.article_id = {article}.id))"
    )


def eligible_article_sql(window: feed_window.FeedWindow) -> str:
    """Статья, которую проверка берёт, а переключение переносит (над a, s — источник, c — карточка статьи): решена
    гейтом и на его пути (новое при флаге решает сам «Отбор»), лента её показала бы, если бы гейт её принял (не
    перепечатка, не на удаление, источник не в архиве), месяц открыт, «Отбор» её не решал и решения человека нет."""
    return (
        f"a.selector IS NULL AND c.relevant IS NOT NULL AND {feed_window.ungated_visible_sql('a', 's')}"
        f" AND NOT {window.closed_sql(feed_window.period_month_sql('a'))}"
        f" AND {feed_window.otbor_decision_sql('a')} IS NULL AND NOT {human_decided_sql('a')}"
    )


_ARTICLE_FROM = ("FROM articles a JOIN sources s ON s.id = a.source_id"
                 " JOIN article_cards c ON c.article_id = a.id")
# Задачи проверки, которые ещё идут: строка на номер статьи из списка задачи.
_PENDING_SQL = (
    "SELECT (jsonb_array_elements_text(j.payload_json->'article_ids'))::bigint AS id FROM background_jobs j"
    f" WHERE j.kind = '{KIND}' AND j.payload_json->>'shadow' = 'true'"
    " AND j.status IN ('queued', 'running', 'finalizing') AND jsonb_typeof(j.payload_json->'article_ids') = 'array'"
)


def candidate_article_ids(*, since: date, window: feed_window.FeedWindow) -> list[int]:
    """Статьи для проверки: поступили с `since`, подходят (eligible_article_sql), строки проверки у них нет или в ней
    технический сбой (модель не ответила: повторить), и они не в задаче проверки, которая ещё идёт. Решённая
    проверкой статья второй раз не оплачивается; ответ не по правилам не повторяется (решение владельца 23.10)."""
    with connection.get_connection() as conn:
        conn.execute("SET LOCAL jit = off")
        rows = conn.execute(
            f"""
            WITH pending AS ({_PENDING_SQL})
            SELECT a.id
            {_ARTICLE_FROM}
            LEFT JOIN otbor_shadow_decisions sh ON sh.article_id = a.id
            WHERE a.collected_at >= %(since)s AND {eligible_article_sql(window)}
              AND (sh.id IS NULL OR (sh.switched_at IS NULL AND sh.decision IS NULL
                                     AND sh.failure = '{FAILURE_TECHNICAL}'))
              AND NOT EXISTS (SELECT 1 FROM pending p WHERE p.id = a.id)
            ORDER BY a.id
            """,
            {"since": since},
        ).fetchall()
    return [int(row[0]) for row in rows]


def pending_articles() -> int:
    """Сколько статей стоит в задачах проверки, которые ещё идут."""
    with connection.get_connection() as conn:
        row = conn.execute(f"SELECT count(DISTINCT id) FROM ({_PENDING_SQL}) pending").fetchone()
    return int(row[0])


# Повтор итога у строки: решение прошлой попытки сбой не снимает, шаги ложатся в ход решения (общее у статьи и находки).
_UPSERT_SET = """
              trigger = EXCLUDED.trigger,
              decision = COALESCE(EXCLUDED.decision, otbor_shadow_decisions.decision),
              values_json = CASE WHEN EXCLUDED.decision IS NULL THEN otbor_shadow_decisions.values_json
                                 ELSE EXCLUDED.values_json END,
              disputed = CASE WHEN EXCLUDED.decision IS NULL THEN otbor_shadow_decisions.disputed
                              ELSE EXCLUDED.disputed END,
              failure = CASE WHEN COALESCE(EXCLUDED.decision, otbor_shadow_decisions.decision) IS NULL
                             THEN EXCLUDED.failure END,
              failure_reason = CASE WHEN COALESCE(EXCLUDED.decision, otbor_shadow_decisions.decision) IS NULL
                                    THEN EXCLUDED.failure_reason END,
              decision_log = otbor_shadow_decisions.decision_log || EXCLUDED.decision_log,
              recipe = CASE WHEN EXCLUDED.decision IS NULL AND otbor_shadow_decisions.decision IS NOT NULL
                            THEN otbor_shadow_decisions.recipe ELSE EXCLUDED.recipe END,
              gate_relevant = EXCLUDED.gate_relevant,
              updated_at = now()
"""


def write(article_id: int, *, values: dict[str, Any] | None, disputed: str | None, failure: str | None,
          failure_reason: str | None, log: list[dict[str, str]], recipe: dict[str, Any]) -> bool:
    """Записать итог проверки по статье: решение (values) или сбой. Сбой решения прошлой попытки не снимает (как у
    сигнала, otbor_store._write_failure): шаги ложатся в ход решения. Перенесённую строку не трогает. False — статьи
    нет или строка уже перенесена."""
    decision = (values or {}).get("otbor_decision")
    with connection.get_connection() as conn:
        if conn.execute("SELECT 1 FROM articles WHERE id = %s", (int(article_id),)).fetchone() is None:
            return False
        gate = conn.execute("SELECT relevant FROM article_cards WHERE article_id = %s", (int(article_id),)).fetchone()
        row = conn.execute(
            f"""
            INSERT INTO otbor_shadow_decisions
              (article_id, trigger, decision, values_json, disputed, failure, failure_reason, decision_log, recipe,
               gate_relevant)
            VALUES (%(article)s, %(trigger)s, %(decision)s, %(values)s, %(disputed)s, %(failure)s, %(failure_reason)s,
                    %(log)s, %(recipe)s, %(gate)s)
            ON CONFLICT (article_id) DO UPDATE SET {_UPSERT_SET}
            WHERE otbor_shadow_decisions.switched_at IS NULL
            RETURNING id
            """,
            {"article": int(article_id), "trigger": str(recipe.get("trigger") or TRIGGER), "decision": decision,
             "values": Json(values) if values is not None else None, "disputed": disputed, "failure": failure,
             "failure_reason": failure_reason, "log": Json(log), "recipe": Json(recipe),
             "gate": gate[0] if gate else None},
        ).fetchone()
        conn.commit()
    return row is not None


# Пометка запуска у строк находок пробного прогона широкого поиска (#138).
SEARCH_TRIAL_TRIGGER = "search_trial"


def write_signal(signal_id: int, *, values: dict[str, Any] | None, disputed: str | None, failure: str | None,
                 failure_reason: str | None, log: list[dict[str, str]], recipe: dict[str, Any]) -> bool:
    """Итог «Отбора» по находке пробного прогона широкого поиска (#138): решение или сбой — в строку находки, по тем же
    правилам повтора, что у статьи. False — находки нет."""
    with connection.get_connection() as conn:
        if conn.execute("SELECT 1 FROM signals WHERE id = %s", (int(signal_id),)).fetchone() is None:
            return False
        row = conn.execute(
            f"""
            INSERT INTO otbor_shadow_decisions
              (signal_id, trigger, decision, values_json, disputed, failure, failure_reason, decision_log, recipe)
            VALUES (%(signal)s, %(trigger)s, %(decision)s, %(values)s, %(disputed)s, %(failure)s, %(failure_reason)s,
                    %(log)s, %(recipe)s)
            ON CONFLICT (signal_id) WHERE signal_id IS NOT NULL DO UPDATE SET {_UPSERT_SET}
            WHERE otbor_shadow_decisions.switched_at IS NULL
            RETURNING id
            """,
            {"signal": int(signal_id), "trigger": SEARCH_TRIAL_TRIGGER,
             "decision": (values or {}).get("otbor_decision"), "values": Json(values) if values is not None else None,
             "disputed": disputed, "failure": failure, "failure_reason": failure_reason, "log": Json(log),
             "recipe": Json({**recipe, "trigger": SEARCH_TRIAL_TRIGGER})},
        ).fetchone()
        conn.commit()
    return row is not None


def signal_rows(signal_ids: list[int]) -> list[dict[str, Any]]:
    """Строки проверки по находкам пробного прогона (#138) — для его отчёта."""
    ids = sorted({int(value) for value in signal_ids})
    if not ids:
        return []
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM otbor_shadow_decisions WHERE signal_id = ANY(%s) ORDER BY signal_id", (ids,))
        return cur.fetchall()


def article_rows(*, since: date) -> list[dict[str, Any]]:
    """Строки проверки по статьям, поступившим с `since`, с заголовком и источником, для отчёта владельцу."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT sh.*, a.title, src.name AS source
            FROM otbor_shadow_decisions sh
            JOIN articles a ON a.id = sh.article_id
            JOIN sources src ON src.id = a.source_id
            WHERE sh.trigger = %(trigger)s AND a.collected_at >= %(since)s
            ORDER BY a.id
            """,
            {"since": since, "trigger": TRIGGER},
        )
        return cur.fetchall()


# С чем сравнить технологии из сбора: радар выключен с 06.10, поэтому за само окно сигналов радара почти нет.
RADAR_BASELINE_WEEKS = 8


def radar_counts(since: date) -> dict[str, int]:
    """Сигналы на радаре сейчас: появившиеся с `since` и за RADAR_BASELINE_WEEKS недель до него (как часто радар
    находил, пока работал)."""
    with connection.get_connection() as conn:
        conn.execute("SET LOCAL jit = off")
        row = conn.execute(
            f"""
            SELECT count(*) FILTER (WHERE s.first_seen_at >= %(since)s),
                   count(*) FILTER (WHERE s.first_seen_at < %(since)s
                                    AND s.first_seen_at >= %(since)s::date - make_interval(weeks => %(weeks)s))
            FROM signals s WHERE {_RADAR_VISIBLE_SQL}
            """,
            {"since": since, "weeks": RADAR_BASELINE_WEEKS},
        ).fetchone()
    return {"window": int(row[0]), "before": int(row[1])}


def switch_rows(*, since: date, window: feed_window.FeedWindow) -> dict[str, Any]:
    """Что переключение перенесёт и что пропустит: неперенесённые строки проверки статей окна (window), из них
    переносимые сейчас (eligible: решение есть, статья по-прежнему подходит) и статьи окна, решённые гейтом, у которых
    строки проверки нет вовсе (missing: с ними переключение отказывает)."""
    params = {"since": since, "trigger": TRIGGER}
    with connection.get_connection() as conn:
        conn.execute("SET LOCAL jit = off")
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT sh.*, COALESCE(btrim(c.summary), '') <> '' AS summarized,
                   (sh.decision IS NOT NULL AND {eligible_article_sql(window)}) AS eligible
            FROM otbor_shadow_decisions sh
            JOIN articles a ON a.id = sh.article_id
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            WHERE sh.switched_at IS NULL AND sh.trigger = %(trigger)s AND a.collected_at >= %(since)s
            ORDER BY sh.article_id
            """,
            params,
        )
        window_rows = cur.fetchall()
        missing = conn.execute(
            f"""
            SELECT a.id {_ARTICLE_FROM}
            WHERE a.collected_at >= %(since)s AND {eligible_article_sql(window)}
              AND NOT EXISTS (SELECT 1 FROM otbor_shadow_decisions sh WHERE sh.article_id = a.id)
            ORDER BY a.id
            """,
            params,
        ).fetchall()
    return {"window": window_rows, "eligible": [row for row in window_rows if row["eligible"]],
            "missing": [int(row[0]) for row in missing]}


def card_signal_ids(article_ids: list[int]) -> list[int]:
    """Сигналы ленты названных статей, у которых уже есть карточка (прогон по истории до «Отбора»)."""
    with connection.get_connection() as conn:
        found = conn.execute(
            "SELECT id FROM signals WHERE signal_key = ANY(%s) AND card_recipe IS NOT NULL ORDER BY id",
            ([f"{feed_signals.FEED_KEY_PREFIX}{int(one)}" for one in article_ids],),
        ).fetchall()
    return [int(row[0]) for row in found]


def transfer(row: dict[str, Any], *, window: feed_window.FeedWindow, log: list[dict[str, str]],
             recipe: dict[str, Any]) -> str | None:
    """Перенести решение проверки в сигнал статьи одной транзакцией: статья под замком и по-прежнему подходит
    (eligible_article_sql: решение человека или закрытый месяц могли появиться и во время переноса), решение пишет путь
    самого «Отбора» (otbor_store._write_decision), карточка, сделанная до решения, у принятой снимается (второй вызов
    не возьмёт её раньше новой: он ждёт карточку), строка проверки отмечена перенесённой. DECIDED; None — статья больше
    не подходит, строку оставляем."""
    article_id = int(row["article_id"])
    values = row["values_json"]
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(f"SELECT a.id {_ARTICLE_FROM} WHERE a.id = %(id)s AND {eligible_article_sql(window)}"
                    " FOR UPDATE OF a", {"id": article_id})
        if cur.fetchone() is None:
            conn.rollback()
            return None
        signal = _signal_for_update(cur, article_id)
        if values.get("otbor_decision") == "accept":
            cur.execute("UPDATE signals SET card_recipe = NULL,"
                        " decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb"
                        " WHERE id = %s AND card_recipe IS NOT NULL", (Json([CARD_AGAIN]), signal["id"]))
        written = _write_decision(cur, {**signal, "chosen_id": article_id}, values, disputed=row["disputed"],
                                  log=log, recipe=recipe, chosen_sql=marks.article_marked_sql("%s", "digest"))
        cur.execute("UPDATE otbor_shadow_decisions SET switched_at = now(), updated_at = now() WHERE id = %s",
                    (int(row["id"]),))
        conn.commit()
    if written == DECIDED:
        # Публикация статьи — к её сигналу, у принятой — и суть, тема, балл, когда они есть (как у write_decision).
        feed_signals.sync_quietly([article_id])
    return written


def cards_to_redo(*, since: date) -> list[int]:
    """Сигналы перенесённых принятых статей окна без карточки, с сутью и не в идущей задаче «Карточки»: им карточку
    ставит переключение (полосой пересчётов). Повторный запуск находит те же, если задачи не дошли. Без сути
    карточку по порядку конвейера поставит шаг дня, когда обработка ленты её даст."""
    with connection.get_connection() as conn:
        found = conn.execute(
            f"""
            WITH pending AS (
              SELECT DISTINCT (jsonb_array_elements_text(j.payload_json->'signal_ids'))::bigint AS id
              FROM background_jobs j
              WHERE j.kind = '{CARD_KIND}' AND j.status IN ('queued', 'running', 'finalizing')
                AND jsonb_typeof(j.payload_json->'signal_ids') = 'array'
            )
            SELECT s.id
            FROM otbor_shadow_decisions sh
            JOIN articles a ON a.id = sh.article_id
            JOIN signals s ON s.signal_key = '{feed_signals.FEED_KEY_PREFIX}' || sh.article_id
            WHERE sh.switched_at IS NOT NULL AND sh.trigger = %(trigger)s AND a.collected_at >= %(since)s
              AND s.otbor_decision = 'accept' AND s.card_recipe IS NULL AND s.merged_into_signal_id IS NULL
              AND COALESCE(btrim(s.summary), '') <> ''
              AND NOT EXISTS (SELECT 1 FROM pending p WHERE p.id = s.id)
            ORDER BY s.id
            """,
            {"since": since, "trigger": TRIGGER},
        ).fetchall()
    return [int(row[0]) for row in found]


def average_tokens(stage: str) -> tuple[float, float] | None:
    """Средние токены входа и ответа одной строки расхода стадии за 30 дней на этой базе (для оценки цены
    переключения по ставкам заданной модели); расхода нет — None."""
    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT avg(input_tokens), avg(output_tokens) FROM ai_processing_runs WHERE stage = %s"
            " AND created_at >= now() - interval '30 days'",
            (stage,),
        ).fetchone()
    return (float(row[0]), float(row[1])) if row and row[0] is not None else None
