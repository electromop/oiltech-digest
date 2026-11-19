"""Карточка сигнала так, как её показывает витрина (#198, спека #197): одно чтение на экран витрины и пакет для
потребителя выпуска, иначе карточка в пакете и на экране разошлись бы.

Карточка: все поля сигнала, заголовок и суть с применёнными правками людей (corrections_lateral, apply_corrections) и
публикации, свои и склеенных дублей, в порядке экрана (publications). Витрина (repository.list_signals и /api/signals)
читает карточки страницы через read и publications; пакет (exchange_packages) теми же функциями.

Модуль ниже repository: repository импортирует его, а не наоборот.
"""

from __future__ import annotations

from collections.abc import Sequence

from psycopg.rows import dict_row

from oiltech_digest.feed_window import FeedWindow, signal_month_sql

# repository.SIGNAL_ORIGIN_FEED: модуль базы импортирует этот модуль, а не наоборот.
_FEED = "feed"

# Исправления из обратной связи («Рекомендуемый заголовок», «…формулировка сути»):
# последнее непустое по карточке. Накладываются при чтении: текст модели в signals не
# затирается, и повторная находка той же карточки прогоном радара правку не перепишет. Только применённые (#188):
# правка администратора и правка материала выпуска; правка участника без прав администратора лежит в его блоке ОС.
# Правка, отменённая из журнала ОС (#189), не ложится: снова текст модели или прежняя правка ({undone}).
_CORRECTIONS_LATERAL = """
LEFT JOIN LATERAL (
  SELECT
    (SELECT e.corrected_title FROM signal_feedback_events e
     WHERE e.signal_id = {alias}.id AND e.applied AND btrim(COALESCE(e.corrected_title, '')) <> ''{until}{undone}
     ORDER BY e.id DESC LIMIT 1) AS corrected_title,
    (SELECT e.corrected_thesis FROM signal_feedback_events e
     WHERE e.signal_id = {alias}.id AND e.applied AND btrim(COALESCE(e.corrected_thesis, '')) <> ''{until}{undone}
     ORDER BY e.id DESC LIMIT 1) AS corrected_thesis
) corr ON TRUE
"""


def corrections_lateral(alias: str, issue_window: FeedWindow | None = None, *, month_sql: str | None = None) -> str:
    """LEFT JOIN LATERAL corr: последние правки заголовка и сути у карточки `alias`.

    Экран витрины и пакет для потребителя: все правки. Выпуск (`issue_window`: окно ленты на сейчас) у карточки
    закрытого месяца берёт только правки, поданные до закрытия (решение владельца 30.09:
    «архив только на просмотр»: и текст закрытого выпуска). Отзыв после закрытия принимается
    как раньше и виден на витрине: он нужен агенту для обучения, но выпуск не меняет.
    `month_sql`: месяц выпуска материала, если он не месяц поступления карточки (у статьи ленты её месяц).
    Отмена правки из журнала ОС (#189) так же: закрытый выпуск не видит отмену после закрытия."""
    until, undone = "", " AND e.correction_undone_at IS NULL"
    if issue_window is not None:
        month = month_sql or signal_month_sql(alias)
        until = " AND " + issue_window.before_close_sql(month, "e.created_at")
        undone = (" AND (e.correction_undone_at IS NULL OR NOT "
                  + issue_window.before_close_sql(month, "e.correction_undone_at") + ")")
    return _CORRECTIONS_LATERAL.format(alias=alias, until=until, undone=undone)


def apply_corrections(row: dict) -> dict:
    """Заголовок и суть карточки: с правками людей поверх текста модели."""
    title = (row.get("corrected_title") or "").strip()
    thesis = (row.get("corrected_thesis") or "").strip()
    if title:
        row["original_title_ru"] = row.get("title_ru")
        row["title_ru"] = title
    if thesis:
        row["original_summary"] = row.get("summary")
        row["summary"] = thesis
    return row


def read(conn, signal_ids: Sequence[int]) -> dict[int, dict]:
    """Карточки по номерам, как на экране витрины: все поля сигнала, заголовок и суть с применёнными правками (все, в
    том числе поданные после закрытия месяца). Нет сигнала: нет и ключа."""
    ids = sorted({int(signal_id) for signal_id in signal_ids})
    if not ids:
        return {}
    rows = conn.cursor(row_factory=dict_row).execute(
        f"""
        SELECT s.*, corr.corrected_title, corr.corrected_thesis
        FROM signals s
        {corrections_lateral("s")}
        WHERE s.id = ANY(%s)
        """,
        (ids,),
    ).fetchall()
    return {int(row["id"]): apply_corrections(row) for row in rows}


def publications(conn, signal_ids: Sequence[int], *, limit: int | None = None) -> dict[int, list[dict]]:
    """Публикации карточек: свои ссылки и ссылки склеенных дублей, у сигнала ленты и его статья, в порядке экрана
    (сила ссылки, дата публикации, когда найдена). limit: сколько на карточку (экран: evidence_limit), None: все (пакет).

    Участники карточки (она сама и её дубли), отдельным списком и простым JOIN: с «OR
    e.signal_id IN (подзапрос)» в условии JOIN база гоняла подзапрос по каждой паре
    карточка × ссылка (ревью PR #83: 150 карточек: 1 118 → 4 мс; на синтетике 600 карточек /
    1 651 ссылка: 342 → 15 мс, строки и порядок те же)."""
    ids = [int(signal_id) for signal_id in signal_ids]
    if not ids or (limit is not None and limit <= 0):
        return {}
    cut = "WHERE ranked.radar_card_rank <= %s" if limit is not None else ""
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        f"""
        WITH members AS (
          SELECT s.id AS card_id, s.id AS member_id FROM signals s WHERE s.id = ANY(%s)
          UNION ALL
          SELECT m.merged_into_signal_id AS card_id, m.id AS member_id
          FROM signals m WHERE m.merged_into_signal_id = ANY(%s)
        ),
        linked AS (
          SELECT mb.card_id, e.id AS evidence_id
          FROM members mb
          JOIN signal_evidence e ON e.signal_id = mb.member_id
          UNION
          -- Статья сигнала ленты: его публикация, даже когда её адрес держит карточка радара
          -- (адрес уникален, ссылка остаётся у радара, feed_signals): показываем ту же ссылку.
          SELECT s.id, e.id
          FROM signals s
          JOIN articles a ON a.id = s.origin_article_id
          JOIN signal_evidence e ON e.source_url = a.url
          WHERE s.id = ANY(%s) AND s.origin = '{_FEED}'
        )
        SELECT *
        FROM (
          SELECT e.*, l.card_id AS radar_card_id,
                 ROW_NUMBER() OVER (
                   PARTITION BY l.card_id
                   ORDER BY e.strength DESC, e.published_at DESC NULLS LAST, e.created_at DESC, e.id DESC
                 ) AS radar_card_rank
          FROM linked l
          JOIN signal_evidence e ON e.id = l.evidence_id
        ) ranked
        {cut}
        ORDER BY ranked.radar_card_id, ranked.radar_card_rank
        """,
        (ids, ids, ids, *([limit] if limit is not None else [])),
    )
    found: dict[int, list[dict]] = {}
    for row in cur.fetchall():
        card_id = int(row.pop("radar_card_id"))
        row.pop("radar_card_rank")
        found.setdefault(card_id, []).append(row)
    return found
