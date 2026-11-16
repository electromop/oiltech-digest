"""Эталон ОС для автопроверки правил эксперта (#190, спека #187): строки агента и шага.

Эталон ОС (решение 10 гриля): оценки и правки администратора и согласованные им улучшения; отметки остальных участников
попадают в него только через согласованное улучшение. ТОП заказчика в эталон ОС не входит (решение владельца 13.11).
Соответствие оценки ожидаемому ответу: как в экзаменах (#133, утверждено 27.10):
- «Отбор»: «Не релевантно» → «не брать»; «Низкая ценность / шум» → «не брать», только если обоснование называет причину
  из справочника «Отбора»; остальные оценки годного сигнала → «брать»; «Не тот блок» → категория, которую поставил
  администратор (её же ставит согласованное «Не тот блок»), оценки без него → категория, в которой карточка показана;
  согласованная уборка → «не брать»;
- баллы: «Сильный сигнал» → «сильный», «Полезный сигнал» → «полезный», «Наблюдать» и «Фоновый материал» → «наблюдать»,
  «Низкая ценность / шум» и «Не релевантно» → «слабый», «Оценка завышена» → ступенью ниже посчитанной;
- «Карточка»: значения полей согласованных улучшений;
- «Дедуп»: «Дубль» с номером и согласованная склейка → «одно событие», отмена склейки → «не одно»;
- «Суть и перевод»: правки заголовка и сути администратора и согласованные (последние N, «было / стало»);
- поиск: ТОП-список темы за N дней: оценки администратора «Сильный» и «Полезный» и выбранные «в дайджест» (решение
  владельца 13.11).

Строка: ключ, сигнал, ожидаемый ответ, принятый ответ агента с версией набора правил, по которой он принят (только если
версия инструкции и справочников та же, что сейчас, и значение поставил агент, а не человек), и подпись для журнала.
По строке на сигнал (у «Дедупа» на пару, у «Карточки» на сигнал и поле): последняя отметка или улучшение.

ВНИМАНИЕ про тесты: соединение: `connection.get_connection()` атрибутом модуля.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row

from oiltech_digest import feedback_changes
from oiltech_digest.card_scores_exam_score import EXPERT_BANDS as VERDICT_BAND
from oiltech_digest.db import connection
from oiltech_digest.db.feedback_store import APPLIED, APPLIED_STATUS, PROPOSAL
from oiltech_digest.db.repository import CARD_SET_BY_AGENT, CARD_SET_BY_HUMAN, signal_digest_sql
from oiltech_digest.feed_window import OTBOR_ACCEPT as ACCEPT, OTBOR_REJECT as REJECT
from oiltech_digest.signal_scoring import BAND_ORDER as BANDS

# Оценка администратора → ступень баллов: таблица экзамена «Карточки» (#133, VERDICT_BAND); «Оценка завышена»:
# ступенью ниже посчитанной.
_GOOD = ("strong_signal", "approved", "watch_later", "background_material", "overrated", "wrong_block")
# Поля «Карточки» по шагу правила: «компании и страны»: участники и страны события.
CARD_STEP_FIELDS = {"theme": ("theme",), "event_date": ("event_date",),
                    "participants": ("participants", "event_countries"), "event_type": ("event_type",),
                    "region": ("region",), "technology_class": ("technology_class",),
                    "maturity": ("technology_maturity",)}
# Шаги «Сути и перевода» → правки: заголовок, суть или обе (термины).
SUMMARY_STEP_FIELDS = {"title": ("title",), "summary": ("summary",), "terms": ("title", "summary")}
_ADMIN_MARK_SQL = ("JOIN users u ON u.id = sfe.user_id AND u.role = 'admin'")


def _rows(sql: str, params: Any = None) -> list[dict[str, Any]]:
    with connection.get_connection() as conn:
        conn.execute("SET LOCAL jit = off")
        return conn.cursor(row_factory=dict_row).execute(sql, params).fetchall()


def _accepted_version(recipe: Any, *, prompt_version: str, dictionaries_key: str | None,
                      dictionaries_version: Any, same: dict[str, Any] | None = None) -> int | None:
    """Версия набора правил, по которой принят ответ агента, если его инструкция и справочники те же, что сейчас
    (рецепт до #190 без версии набора: принят без правил, версия 0); иначе None: принятый ответ не годится. same:
    поля рецепта, которые тоже должны совпасть, если они в рецепте есть (отпечаток критериев баллов)."""
    recipe = recipe if isinstance(recipe, dict) else {}
    if recipe.get("prompt_version") != prompt_version:
        return None
    if dictionaries_key and recipe.get(dictionaries_key) != dictionaries_version:
        return None
    if any(key in recipe and recipe[key] != value for key, value in (same or {}).items()):
        return None
    try:
        return max(0, int(recipe.get("expert_rules_version") or 0))
    except (TypeError, ValueError):
        return None


# --- «Отбор» -------------------------------------------------------------------------------------


def otbor_rows(steps: list[str], category: str | None, limit: int, *, reasons: dict[str, str]) -> list[dict[str, Any]]:
    """Решение и категория «Отбора» против оценок администратора и согласованных улучшений (категория, уборка).
    reasons: коды причин отказа справочника «Отбора» → подписи (их ищет обоснование «Шума»)."""
    from oiltech_digest import otbor
    from oiltech_digest.otbor_pack_format import load_dictionaries

    dictionaries = load_dictionaries()
    found = _rows(
        f"""
        SELECT DISTINCT ON (sfe.signal_id) sfe.id AS event_id, sfe.signal_id, sfe.verdict, sfe.reason, sfe.comment,
               sfe.created_at AS at, s.origin, s.origin_article_id, s.otbor_decision, s.signal_category, s.card_set_by,
               s.otbor_recipe, s.reject_reason, COALESCE(s.title_ru, s.title) AS label
        FROM signal_feedback_events sfe {_ADMIN_MARK_SQL}
        JOIN signals s ON s.id = sfe.signal_id
        WHERE sfe.verdict = ANY(%s) AND s.otbor_decision IS NOT NULL AND s.merged_into_signal_id IS NULL
        ORDER BY sfe.signal_id, sfe.created_at DESC, sfe.id DESC
        """, ([*_GOOD, "reject", "wrong_domain"],))
    found += _rows(
        f"""
        SELECT DISTINCT ON (fa.signal_id) NULL::bigint AS event_id, fa.signal_id, CASE
                 WHEN fa.details->'changes' @> '[{{"field": "removed"}}]' THEN 'removed' ELSE 'category' END AS verdict,
               NULL AS reason, NULL AS comment, fa.created_at AS at, s.origin, s.origin_article_id, s.otbor_decision,
               s.signal_category, s.card_set_by, s.otbor_recipe, s.reject_reason, COALESCE(s.title_ru, s.title) AS label
        FROM feedback_actions fa JOIN feedback_actions p ON p.id = fa.parent_id AND p.kind = '{PROPOSAL}'
        JOIN signals s ON s.id = fa.signal_id
        WHERE fa.kind = '{APPLIED}' AND fa.status = '{APPLIED_STATUS}' AND s.merged_into_signal_id IS NULL
          AND (fa.details->'changes' @> '[{{"field": "removed"}}]'
               OR fa.details->'changes' @> '[{{"field": "signal_category"}}]')
        ORDER BY fa.signal_id, fa.created_at DESC
        """)
    latest: dict[int, dict[str, Any]] = {}
    for row in sorted(found, key=lambda one: one["at"]):
        latest[int(row["signal_id"])] = row
    rows = []
    for row in sorted(latest.values(), key=lambda one: one["at"], reverse=True):
        set_by = row["card_set_by"] or {}
        human_category = set_by.get("signal_category") == CARD_SET_BY_HUMAN
        if category and row["signal_category"] != category:
            continue
        kind, expected, reason = _otbor_expected(row, steps, reasons, human_category)
        if expected is None:
            continue
        version = _accepted_version(row["otbor_recipe"], prompt_version=otbor.PROMPT_VERSION,
                                    dictionaries_key="dictionaries_version",
                                    dictionaries_version=dictionaries.get("version"))
        machine = (set_by.get("otbor_decision") != CARD_SET_BY_HUMAN and not human_category
                   and row["verdict"] not in ("removed", "category"))
        accepted = ({"decision": row["otbor_decision"], "category": row["signal_category"],
                     "reason": row["reject_reason"]} if machine and version is not None else None)
        ref = (f"article:{int(row['origin_article_id'])}" if row["origin"] == "feed" and row["origin_article_id"]
               else f"signal:{int(row['signal_id'])}")
        rows.append({"key": f"{kind}:{int(row['signal_id'])}", "signal_id": int(row["signal_id"]), "ref": ref,
                     "kind": kind, "expected": expected, "reason": reason, "accepted": accepted,
                     "accepted_version": version if accepted else None, "label": row["label"] or "",
                     "event_id": row["event_id"]})
        if len(rows) >= limit:
            break
    return rows


def _otbor_expected(row: dict[str, Any], steps: list[str], reasons: dict[str, str],
                    human_category: bool) -> tuple[str, Any, str | None]:
    """(вид строки, ожидаемый ответ, ожидаемая причина отказа). «Шум» даёт строку, только если обоснование называет
    причину справочника, и эта причина тоже ожидается («решение и причина», как в экзамене)."""
    verdict = row["verdict"]
    if "category" in steps and (verdict == "category" or (verdict == "wrong_block" and human_category)):
        return "category", row["signal_category"], None
    if "category" in steps and verdict in _GOOD and verdict != "wrong_block" and not human_category:
        category = row["signal_category"] if row["signal_category"] in ("business", "technology") else None
        return "category", category, None
    if not set(steps) - {"category"}:
        return "decision", None, None
    if verdict in ("wrong_domain", "removed"):
        return "decision", REJECT, None
    if verdict == "reject":
        text = f"{row['reason'] or ''} {row['comment'] or ''}".casefold()
        named = next((code for code, label in reasons.items() if len(label) >= 4 and label.casefold() in text), None)
        return "decision", REJECT if named else None, named
    if verdict in _GOOD:
        return "decision", ACCEPT, None
    return "decision", None, None


# --- Баллы ---------------------------------------------------------------------------------------


def scores_rows(category: str | None, limit: int) -> list[dict[str, Any]]:
    """Ступень баллов против оценки администратора; сигнал, который этап сейчас оценивает (принят «Отбором»)."""
    from oiltech_digest import signal_scores
    from oiltech_digest.db.signal_scores_store import _SCORABLE_SQL

    rubrics = signal_scores.load_rubrics()
    fingerprints = signal_scores.criteria_fingerprints(rubrics)
    found = _rows(
        f"""
        SELECT * FROM (
          SELECT DISTINCT ON (sfe.signal_id) sfe.id AS event_id, sfe.signal_id, sfe.verdict, sfe.created_at AS at,
                 s.score_band, s.scores_recipe, s.signal_category, s.score_profile,
                 COALESCE(s.title_ru, s.title) AS label
          FROM signal_feedback_events sfe {_ADMIN_MARK_SQL}
          JOIN signals s ON s.id = sfe.signal_id
          WHERE sfe.verdict = ANY(%s) AND s.score_band IS NOT NULL AND {_SCORABLE_SQL}
          ORDER BY sfe.signal_id, sfe.created_at DESC, sfe.id DESC
        ) last ORDER BY at DESC
        """, ([*VERDICT_BAND, "overrated"],))
    rows = []
    for row in found:
        if category and row["signal_category"] != category:
            continue
        band = row["score_band"]
        if row["verdict"] == "overrated":
            expected = BANDS[min(BANDS.index(band) + 1, len(BANDS) - 1)] if band in BANDS else None
        else:
            expected = VERDICT_BAND[row["verdict"]]
        if expected is None:
            continue
        version = _accepted_version(row["scores_recipe"], prompt_version=signal_scores.PROMPT_VERSION,
                                    dictionaries_key="rubrics_version", dictionaries_version=rubrics["version"],
                                    same={"criteria_fingerprint": fingerprints.get(row["score_profile"])})
        rows.append({"key": f"scores:{int(row['signal_id'])}", "signal_id": int(row["signal_id"]), "kind": "band",
                     "expected": expected, "accepted": band if version is not None else None,
                     "accepted_version": version, "label": row["label"] or "", "event_id": int(row["event_id"])})
        if len(rows) >= limit:
            break
    return rows


# --- «Карточка» ----------------------------------------------------------------------------------


def card_rows(steps: list[str], category: str | None, limit: int) -> list[dict[str, Any]]:
    """Поля «Карточки» против согласованных улучшений карточки (неотменённых)."""
    from oiltech_digest import signal_card
    from oiltech_digest.signal_card_pack import load_dictionaries

    fields = [field for step in steps for field in CARD_STEP_FIELDS.get(step, ())]
    if not fields:
        return []
    version_now = load_dictionaries().get("version")
    found = _rows(
        f"""
        SELECT fa.id AS action_id, fa.signal_id, fa.created_at AS at, change, s.card_recipe, s.signal_category,
               COALESCE(s.title_ru, s.title) AS label
        FROM feedback_actions fa JOIN feedback_actions p ON p.id = fa.parent_id AND p.kind = '{PROPOSAL}'
        JOIN signals s ON s.id = fa.signal_id
        CROSS JOIN LATERAL jsonb_array_elements(fa.details->'changes') change
        WHERE fa.kind = '{APPLIED}' AND fa.status = '{APPLIED_STATUS}' AND change->>'field' = ANY(%s)
          AND s.merged_into_signal_id IS NULL
        ORDER BY fa.created_at DESC, fa.id DESC
        """, (fields,))
    rows, seen = [], set()
    for row in found:
        change = row["change"] or {}
        key = f"card:{int(row['signal_id'])}:{change.get('field')}"
        if key in seen or (category and row["signal_category"] != category):
            continue
        seen.add(key)
        before = change.get("before") or {}
        version = _accepted_version(row["card_recipe"], prompt_version=signal_card.PROMPT_VERSION,
                                    dictionaries_key="dictionaries_version", dictionaries_version=version_now)
        machine = before.get("set_by") == CARD_SET_BY_AGENT and version is not None
        rows.append({"key": key, "signal_id": int(row["signal_id"]), "kind": "field", "field": change.get("field"),
                     "expected": (change.get("after") or {}).get("columns") or {},
                     "accepted": before.get("columns") if machine else None,
                     "accepted_version": version if machine else None, "label": row["label"] or ""})
        if len(rows) >= limit:
            break
    return rows


def feed_articles(signal_ids: list[int]) -> dict[int, int]:
    """Сигналы ленты → их статьи: вход «Сути и перевода» у сигнала ленты: его статья."""
    found = _rows("SELECT id, origin_article_id FROM signals WHERE id = ANY(%s) AND origin = 'feed'"
                  " AND origin_article_id IS NOT NULL", ([int(one) for one in signal_ids],))
    return {int(row["id"]): int(row["origin_article_id"]) for row in found}


# --- «Дедуп» -------------------------------------------------------------------------------------


def dedup_rows(limit: int) -> list[dict[str, Any]]:
    """Пары «одно ли событие»: «Дубль» администратора с номером и согласованная склейка (одно), отмена склейки (не
    одно). По паре: последнее слово."""
    found = _rows(
        f"""
        SELECT sfe.signal_id AS a, sfe.duplicate_of_signal_id AS b, TRUE AS same, sfe.created_at AS at
        FROM signal_feedback_events sfe {_ADMIN_MARK_SQL}
        WHERE sfe.verdict = 'merge_duplicate' AND sfe.duplicate_of_signal_id IS NOT NULL
        UNION ALL
        SELECT fa.signal_id, (change->>'root')::bigint, TRUE, fa.created_at
        FROM feedback_actions fa JOIN feedback_actions p ON p.id = fa.parent_id AND p.kind = '{PROPOSAL}'
        CROSS JOIN LATERAL jsonb_array_elements(fa.details->'changes') change
        WHERE fa.kind = '{APPLIED}' AND fa.status = '{APPLIED_STATUS}' AND change->>'field' = 'duplicate_of'
        UNION ALL
        SELECT signal_id, other_signal_id, same_event, created_at FROM signal_event_pairs
        """)
    latest: dict[tuple[int, int], dict[str, Any]] = {}
    for row in sorted(found, key=lambda one: one["at"]):
        if row["a"] is None or row["b"] is None or int(row["a"]) == int(row["b"]):
            continue
        pair = tuple(sorted((int(row["a"]), int(row["b"]))))
        latest[pair] = row
    rows = [{"key": f"pair:{a}:{b}", "signal_id": a, "pair": [a, b], "kind": "pair", "expected": bool(row["same"]),
             "accepted": None, "accepted_version": None, "label": ""}
            for (a, b), row in sorted(latest.items(), key=lambda item: item[1]["at"], reverse=True)]
    return rows[:limit]


# --- «Суть и перевод» ----------------------------------------------------------------------------


def summary_rows(steps: list[str], category: str | None, limit: int) -> list[dict[str, Any]]:
    """Последние правки заголовка и сути администратора (форма ОС, «Дайджест», «Материалы») и согласованные улучшения
    текста, неотменённые: текст эксперта рядом с «было / стало»."""
    fields = {field for step in steps for field in SUMMARY_STEP_FIELDS.get(step, ())}
    found = _rows(
        f"""
        SELECT sfe.id AS event_id, sfe.signal_id, sfe.created_at AS at, sfe.corrected_title, sfe.corrected_thesis,
               s.signal_category, COALESCE(s.title_ru, s.title) AS label
        FROM signal_feedback_events sfe {_ADMIN_MARK_SQL}
        JOIN signals s ON s.id = sfe.signal_id
        WHERE sfe.applied AND sfe.correction_undone_at IS NULL AND s.merged_into_signal_id IS NULL
          AND (btrim(COALESCE(sfe.corrected_title, '')) <> '' OR btrim(COALESCE(sfe.corrected_thesis, '')) <> '')
        ORDER BY sfe.created_at DESC, sfe.id DESC
        LIMIT %s
        """, (int(limit) * 4,))
    rows, seen = [], set()
    for row in found:
        if category and row["signal_category"] != category:
            continue
        for field, text in (("title", row["corrected_title"]), ("summary", row["corrected_thesis"])):
            key = f"text:{int(row['signal_id'])}:{field}"
            if field not in fields or not (text or "").strip() or key in seen:
                continue
            seen.add(key)
            rows.append({"key": key, "signal_id": int(row["signal_id"]), "kind": "text", "field": field,
                         "expected": " ".join(text.split()), "accepted": None, "accepted_version": None,
                         "label": row["label"] or ""})
    return rows[:limit]


# --- Поиск радара --------------------------------------------------------------------------------


def search_rows(topic: str, days: int) -> list[dict[str, Any]]:
    """ТОП-список темы за days дней (решение владельца 13.11): карточки темы, поступившие за окно, с оценкой
    администратора «Сильный сигнал» или «Полезный сигнал» или выбранные «в дайджест»; ссылки и заголовок каждой."""
    since = datetime.now(timezone.utc) - timedelta(days=int(days))
    found = _rows(
        f"""
        SELECT s.id AS signal_id, COALESCE(s.title_ru, s.title) AS label, s.title,
               ARRAY(SELECT e.source_url FROM signal_evidence e WHERE e.signal_id = s.id AND e.source_url IS NOT NULL
                     UNION SELECT a.url FROM articles a WHERE a.id = s.origin_article_id) AS urls
        FROM signals s
        WHERE s.theme = %s AND s.first_seen_at >= %s AND s.merged_into_signal_id IS NULL
          AND ({signal_digest_sql('s')} OR EXISTS (
                SELECT 1 FROM signal_feedback_events sfe {_ADMIN_MARK_SQL}
                WHERE sfe.signal_id = s.id AND sfe.verdict IN ('strong_signal', 'approved')))
        ORDER BY s.first_seen_at DESC, s.id DESC
        """, (topic, since))
    return [{"key": f"top:{int(row['signal_id'])}", "signal_id": int(row["signal_id"]), "kind": "top",
             "expected": True, "accepted": None, "accepted_version": None, "label": row["label"] or "",
             "title": row["title"] or "", "urls": [url for url in row["urls"] or [] if url]} for row in found]


def card_field_value(field: str, values: dict[str, Any]) -> Any:
    """Значение поля «Карточки» для сравнения с эталоном: как его видят люди (подписи), а не как хранится."""
    spec = feedback_changes.FIELDS[field]
    return feedback_changes.display_value(field, {column: values.get(column) for column in spec.columns}
                                          | {"event_date_precision": values.get("event_date_precision")})
