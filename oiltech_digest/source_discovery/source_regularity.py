"""Regularity assessment for source candidates."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from dateutil import parser as dateparser


_ARCHIVE_URL_RE = re.compile(r"(^|/)(archive|archives|arhiv|архив|20[0-2]\d)(/|$)", re.I)
_ARCHIVE_TEXT_RE = re.compile(r"\b(архив|archive|archives)\b", re.I)


def assess_source_regularity(
    candidate: dict[str, Any],
    collected: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Detect whether a candidate looks like a regular source, not only parsable."""
    current = _aware_utc(now or datetime.now(timezone.utc))
    articles = _merge_articles(collected, evidence)
    dated = [_parse_datetime(item.get("published_at")) for item in articles]
    dates = sorted((item for item in dated if item is not None), reverse=True)
    latest = dates[0] if dates else None
    oldest = dates[-1] if dates else None
    last_30 = sum(1 for item in dates if 0 <= (current - item).days <= 30)
    last_90 = sum(1 for item in dates if 0 <= (current - item).days <= 90)
    future_dates = sum(1 for item in dates if item > current)
    latest_age_days = (current - latest).days if latest else None
    archive_hint = _has_archive_hint(candidate, articles)

    if not dates:
        label = "unknown"
        regular = False
        confidence = 0.2
        reason = "У найденных материалов нет дат публикации, поэтому регулярный поток пока нельзя подтвердить."
    elif archive_hint and (latest_age_days is None or latest_age_days > 90):
        label = "archive"
        regular = False
        confidence = 0.78
        reason = "Раздел похож на архив: есть архивный признак и нет свежих материалов."
    elif latest_age_days is not None and latest_age_days > 180:
        label = "stale"
        regular = False
        confidence = 0.75
        reason = "Последний материал старше 180 дней, источник выглядит неактивным."
    elif last_30 >= 3 or (last_30 >= 1 and last_90 >= 4):
        label = "regular"
        regular = True
        confidence = 0.82
        reason = "В выборке есть устойчивые свежие публикации за последние 30-90 дней."
    elif last_90 >= 2 or last_30 >= 1:
        label = "active_but_sparse"
        regular = True
        confidence = 0.62
        reason = "Источник обновляется, но поток в выборке пока редкий."
    else:
        label = "stale"
        regular = False
        confidence = 0.68
        reason = "Свежих материалов за последние 90 дней в выборке не найдено."

    risks: list[str] = []
    if len(dates) < max(2, min(5, len(articles))):
        risks.append("Не у всех материалов есть дата публикации.")
    if archive_hint:
        risks.append("В адресе или тексте есть признак архивного раздела.")
    if future_dates:
        risks.append("Есть даты из будущего, возможны анонсы или ошибка парсинга даты.")
    if articles and len(dates) == 0:
        risks.append("Парсер ссылок работает, но дату публикации надо доставать точнее.")

    next_checks: list[str] = []
    if label in {"unknown", "active_but_sparse"}:
        next_checks.append("Увеличить пробную выборку и повторить проверку регулярности.")
    if label in {"unknown", "stale", "archive"}:
        next_checks.append("Проверить, не выбран ли архив, поиск или разовая статья вместо новостного раздела.")
    if len(dates) < len(articles):
        next_checks.append("Уточнить CSS/XPath/metadata-парсинг дат для этого источника.")
    if not next_checks:
        next_checks.append("После одобрения проверить источник еще одним плановым прогоном.")

    return {
        "source": "rules",
        "regularity_label": label,
        "is_regular": regular,
        "sample_size": len(articles),
        "dated_articles": len(dates),
        "articles_last_30_days": last_30,
        "articles_last_90_days": last_90,
        "latest_published_at": latest.isoformat() if latest else None,
        "oldest_published_at": oldest.isoformat() if oldest else None,
        "latest_age_days": latest_age_days,
        "archive_suspected": bool(archive_hint and label in {"archive", "stale", "unknown"}),
        "section_updates": bool(regular),
        "reason": reason,
        "risks": risks[:5],
        "next_checks": next_checks[:5],
        "confidence": confidence,
    }


def regularity_comment(regularity: dict[str, Any] | None) -> str:
    if not regularity:
        return ""
    label = str(regularity.get("regularity_label") or "unknown")
    labels = {
        "regular": "регулярный поток",
        "active_but_sparse": "редкий, но живой поток",
        "stale": "похоже, не обновляется",
        "archive": "похоже на архив",
        "unknown": "регулярность не подтверждена, дат мало",
    }
    last_30 = int(regularity.get("articles_last_30_days") or 0)
    last_90 = int(regularity.get("articles_last_90_days") or 0)
    dated = int(regularity.get("dated_articles") or 0)
    sample = int(regularity.get("sample_size") or 0)
    latest_age = regularity.get("latest_age_days")
    age = f", последний материал {int(latest_age)} дн. назад" if latest_age is not None else ""
    return (
        f"Регулярность источника: {labels.get(label, label)} "
        f"({last_30} за 30 дней, {last_90} за 90 дней, датировано {dated}/{sample}{age}). "
        f"{str(regularity.get('reason') or '').strip()}"
    ).strip()


def _merge_articles(collected: dict[str, Any] | None, evidence: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in (collected or {}).get("articles") or []:
        key = str(item.get("url") or item.get("id") or len(merged))
        merged[key] = dict(item)
    for item in evidence or []:
        key = str(item.get("url") or item.get("id") or len(merged))
        merged[key] = {**merged.get(key, {}), **dict(item)}
    return list(merged.values())


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware_utc(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return _aware_utc(dateparser.parse(text))
    except (TypeError, ValueError, OverflowError):
        return None


def _aware_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _has_archive_hint(candidate: dict[str, Any], articles: list[dict[str, Any]]) -> bool:
    haystack = " ".join(
        [
            str(candidate.get("url") or ""),
            str(candidate.get("name") or ""),
            *(str(item.get("url") or "") for item in articles[:10]),
            *(str(item.get("title") or "") for item in articles[:10]),
        ]
    )
    return bool(_ARCHIVE_URL_RE.search(haystack) or _ARCHIVE_TEXT_RE.search(haystack))
