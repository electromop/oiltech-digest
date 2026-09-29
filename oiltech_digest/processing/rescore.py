"""Пересчёт балла статей с ИИ (сессия G, ADR 0002): выборка, стоимость, задачи.

Нужен, когда у профиля business сменились тексты критериев или сам набор: подпункты старых
баллов отвечали на другие вопросы, и пересчёт без ИИ (rescore-recompute) их не исправит.
Смена одних весов — дело rescore-recompute, модель для этого не зовётся.

Только окно ленты (feed_window: текущий месяц, до 5-го числа по МСК — ещё и прошлый): архив —
только просмотр. Задачи — process_articles с пометкой only=["scoring"] в полосе пересчётов:
воркер зовёт одну оценку по записанной сути, ядро пишет только балл (stages_to_write).
"""

from __future__ import annotations

from typing import Any

from oiltech_digest import config, feed_window, network_policy, scoring_profiles
# Модулем, а не функцией: тестовая фикстура подменяет connection.get_connection.
from oiltech_digest.db import connection, repository

RESCORE_STAGES = ["scoring"]


def rescore_selection(profile: str, month: str) -> dict[str, Any]:
    """Видимые в ленте оценённые статьи месяца, чей балл посчитан не текущим набором профиля.

    Балл со снимком, совпадающим с текущим набором по id и текстам, пересчёта с ИИ не требует
    (если отличаются только веса — хватит rescore-recompute). Без снимка (до профилей) — неизвестно,
    каким набором посчитан: в выборку."""
    profile = scoring_profiles.check_profile(profile)
    if profile != scoring_profiles.ARTICLE_SCORING_PROFILE:
        raise ValueError(f"статьи ленты оцениваются только профилем {scoring_profiles.ARTICLE_SCORING_PROFILE}; "
                         f"профиль {profile} к оценке не подключён (ADR 0002)")
    period = feed_window.month_key(feed_window.parse_month(month))
    window = feed_window.current()
    if period not in window.open_months:
        raise ValueError(f"месяц {period} вне окна ленты ({', '.join(window.open_months)}): архив — только просмотр")
    current = repository.list_enabled_scoring_criteria(profile)
    snapshot = scoring_profiles.criteria_snapshot(current)
    current_texts = scoring_profiles.snapshot_texts(snapshot)
    current_weights = {entry["id"]: entry["weight"] for entry in snapshot}
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT a.id, s.criteria_snapshot,
                   EXISTS (SELECT 1 FROM user_article_states u
                           WHERE u.article_id = a.id AND u.status = 'digest') AS in_digest
            FROM article_scores s
            JOIN articles a ON a.id = s.article_id
            JOIN sources src ON src.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE COALESCE(s.profile, %s) = %s
              AND {feed_window.visible_sql("a", "c", "src")}
              AND {feed_window.period_month_sql("a")} = %s
            ORDER BY a.id
            """,
            (scoring_profiles.ARTICLE_SCORING_PROFILE, profile, period),
        ).fetchall()
    selection: dict[str, Any] = {
        "profile": profile, "month": period, "scored": len(rows),
        "up_to_date": 0, "weights_only": 0, "changed": 0, "no_snapshot": 0,
        "article_ids": [], "in_digest": 0,
    }
    for article_id, stored, in_digest in rows:
        if stored is None:
            selection["no_snapshot"] += 1
        elif current_texts is not None and scoring_profiles.snapshot_texts(stored) == current_texts:
            selection["up_to_date"] += 1
            weights = {int(entry["id"]): float(entry.get("weight") or 0) for entry in stored}
            selection["weights_only"] += int(weights != current_weights)
            continue
        else:
            selection["changed"] += 1
        selection["article_ids"].append(int(article_id))
        selection["in_digest"] += int(bool(in_digest))
    return selection


def scoring_cost_estimate(count: int, *, days: int = 30) -> dict[str, Any]:
    """Стоимость `count` вызовов оценки: N × (in·p_in + out·p_out) / 10⁶ (ADR 0002).

    in и out — средние токены стадии scoring за `days` дней у модели, что оценивала чаще всех:
    это модель из .env воркера NL, на ядре её иначе не узнать. Рассуждение модели входит в
    output_tokens. Ставки — config.price_for_model. Сверять — со счётом провайдера."""
    with connection.get_connection() as conn:
        row = conn.execute(
            """
            SELECT model, count(*), avg(input_tokens), avg(output_tokens)
            FROM ai_processing_runs
            WHERE stage = 'scoring' AND status = 'ok' AND provider = 'openai' AND model IS NOT NULL
              AND created_at > now() - make_interval(days => %s)
            GROUP BY model
            ORDER BY count(*) DESC, model
            LIMIT 1
            """,
            (days,),
        ).fetchone()
    if row is None:
        return {"count": count, "days": days, "model": None, "runs": 0, "usd_per_call": None, "usd_total": None}
    model, runs, avg_in, avg_out = row
    price_in, price_out = config.price_for_model(model)
    per_call = (float(avg_in) * price_in + float(avg_out) * price_out) / 1_000_000
    return {
        "count": count, "days": days, "model": model, "runs": int(runs),
        "avg_input_tokens": round(float(avg_in)), "avg_output_tokens": round(float(avg_out)),
        "price_in": price_in, "price_out": price_out,
        "usd_per_call": per_call, "usd_total": per_call * count,
    }


def enqueue_rescore(article_ids: list[int], *, batch_size: int = 20) -> list[int]:
    """Задачи внешнего контура: process_articles с явным списком и пометкой only=["scoring"]."""
    decision = network_policy.route_ai_bulk()
    if decision.execution_region != "external":
        # Локальный конвейер пометки only не знает, а у оценённой статьи балл пропускает:
        # задача прошла бы молча впустую.
        raise RuntimeError("пересчёт балла идёт только через внешний контур ИИ")
    batch = max(1, batch_size)
    jobs = []
    for start in range(0, len(article_ids), batch):
        chunk = article_ids[start : start + batch]
        job = repository.create_background_job(
            "process_articles",
            {"article_ids": chunk, "limit": len(chunk), "offline": False, "only": RESCORE_STAGES},
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        jobs.append(int(job["id"]))
    return jobs
