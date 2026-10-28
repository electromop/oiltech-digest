"""Починка даты публикации уже собранных статей новым разбором страницы (#139).

У Pipeline & Gas Journal с 30.05.2026 первый `<time>` страницы несёт `datetime="2025-10-01"` на
всех статьях, и сбор брал его: 542 статьи из 560 легли в октябрь 2025 года, мимо лент и выпусков
своих месяцев. Разбор исправлен (дата страницы сверяется с датой в адресе статьи), здесь починка
строк в базе.

Сухой прогон заново скачивает и разбирает страницы и даёт поимённый план «id, было, стало».
Записывается ровно этот план, без новой перекачки: владелец согласует список, и между согласованием
и записью он не меняется. Правило узкое, как у починки тел: в план идут только статьи, у которых
сохранённая дата противоречит дате в адресе; «стало» — дата нового разбора той же страницы,
сошедшаяся с адресом, или пусто, если не сошлась ни одна (лента возьмёт дату поступления). Пишется
только дата публикации и её копия у публикации сигнала ленты: перерасчёт ИИ по исправленным —
отдельное решение владельца, он платный.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Any, Callable

from oiltech_digest import feed_window
from oiltech_digest.db import feed_signals, repository
from oiltech_digest.ingestion import dates, normalize
from oiltech_digest.ingestion.article_page import parse_article_page

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DateDecision:
    article_id: int
    action: str  # change | skip
    defect: bool  # сохранённая дата противоречит адресу статьи
    reason: str
    url: str
    old: datetime | None
    new: datetime | None  # после записи плана: у замены — дата нового разбора, у пропуска — прежняя
    page: datetime | None  # что даёт новый разбор страницы
    page_status: str  # read | not downloaded | not the article


def _feed_month(published: datetime | None, collected: datetime) -> str:
    """Месяц ленты и выпуска — как feed_window.period_month_sql (часовой пояс базы, на проде UTC)."""
    return feed_window.month_key((published or collected).astimezone(timezone.utc))


def _skip_reason(article: dict[str, Any]) -> str | None:
    """Почему статья не идёт в план при любой странице; None — сохранённая дата противоречит адресу."""
    in_url = dates.url_date(article.get("url"))
    if in_url is None:
        return "url has no date"
    if article.get("published_at") is None:
        return "no stored date"
    # В базе — метка времени: полночь UTC там бывает и настоящим временем (выпуск в 17:00 по
    # Калифорнии), поэтому дефект — только то, что не объясняется сдвигом пояса.
    if in_url.agrees(article["published_at"], timestamp=True):
        return "stored date agrees with url"
    return None


def plan_date(article: dict[str, Any], content: bytes | str | None) -> DateDecision:
    """Решение по одной статье: что даёт страница `content` и идёт ли статья в план."""
    old = article.get("published_at")
    title, url = str(article.get("title") or ""), str(article["url"])
    page, status = None, "not downloaded"
    if content:
        _, parsed, text = parse_article_page(content, title, url=url)
        # Сайт отдал не статью (снята, увёл на главную): «сегодняшняя» дата чужой страницы того же
        # месяца сверку с адресом прошла бы.
        status = "read" if normalize.title_matches_body(title, text) else "not the article"
        page = parsed if status == "read" else None

    def decision(action: str, reason: str, *, defect: bool = True) -> DateDecision:
        return DateDecision(int(article["id"]), action, defect, reason, url, old,
                            page if action == "change" else old, page, status)

    skip = _skip_reason(article)
    if skip is not None:
        return decision("skip", skip, defect=False)
    if status == "not downloaded":
        return decision("skip", "page not downloaded")
    if status == "not the article":
        return decision("skip", "page is not the article")
    if page is None:
        return decision("change", "no page date agrees with url")
    return decision("change", "page date agrees with url")


def repair_dates(
    articles: list[dict[str, Any]],
    *,
    fetch_fn: Callable[[str], bytes | str | None] | None = None,
    pause_seconds: float = 0.5,
) -> dict[str, Any]:
    """Сухой прогон: скачать и заново разобрать страницы статей; план замен и отчёт по каждой статье.

    В базу не пишет. `changes` — план для apply_plan, `skipped` — остальные статьи с причиной и тем,
    что даёт их страница (так смотрят статьи, под которые правил нет), `months` — переезды по месяцам
    ленты после записи плана."""
    if fetch_fn is None:
        from oiltech_digest.ingestion.http_client import fetch as fetch_fn  # noqa: PLW0127
    decisions: list[DateDecision] = []
    months: dict[tuple[str, str], int] = {}
    for article in articles:
        try:
            content = fetch_fn(str(article["url"]))
        except Exception as exc:  # noqa: BLE001 - одна статья не валит починку
            logger.warning("date_repair fetch failed article=%s: %s", article.get("id"), exc)
            content = None
        item = plan_date(article, content)
        decisions.append(item)
        if item.action == "change":
            move = (_feed_month(item.old, article["collected_at"]), _feed_month(item.new, article["collected_at"]))
            months[move] = months.get(move, 0) + 1
        if pause_seconds:
            time.sleep(pause_seconds)
    stats: dict[str, int] = {}
    for item in decisions:
        key = f"{item.action}:{item.reason}"
        stats[key] = stats.get(key, 0) + 1
    return {
        "checked": len(decisions),
        "changed": sum(1 for item in decisions if item.action == "change"),
        "stats": dict(sorted(stats.items())),
        "months": [{"old_month": old, "new_month": new, "count": count}
                   for (old, new), count in sorted(months.items())],
        "changes": [asdict(item) for item in decisions if item.action == "change"],
        "skipped": [asdict(item) for item in decisions if item.action == "skip"],
    }


def load_plan(path: str | Path) -> list[tuple[int, datetime | None, datetime | None]]:
    """Замены из файла сухого прогона (`--save-plan`): (id, было, стало). Не план — ValueError."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return [(int(item["article_id"]), _moment(item["old"]), _moment(item["new"])) for item in data["changes"]]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{path}: не план repair-article-dates ({type(exc).__name__}: {exc})") from exc


def _moment(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"дата без часового пояса: {value!r}")
    return parsed


def apply_plan(changes: list[tuple[int, datetime | None, datetime | None]]) -> dict[str, Any]:
    """Записать план одной транзакцией: дату статьи и её копию у публикаций сигналов ленты.

    Дата пишется, только если у статьи всё ещё «было»: дату, которую поменяли после сухого прогона,
    план не затирает (это уже другой список и новое согласование). Мост «лента → сигнал» держит дату
    статьи в signal_evidence.published_at; обновляется только эта копия (feed_signals)."""
    written: list[int] = []
    with repository.get_connection() as conn:
        for article_id, old, new in changes:
            cur = conn.execute(
                "UPDATE articles SET published_at = %s, updated_at = now()"
                " WHERE id = %s AND published_at IS NOT DISTINCT FROM %s",
                (new, article_id, old),
            )
            if cur.rowcount:
                written.append(article_id)
        copies = feed_signals.refresh_publication_dates(conn, written)
        conn.commit()
    done = set(written)
    return {
        "planned": len(changes),
        "written_ids": written,
        "not_written_ids": [article_id for article_id, _, _ in changes if article_id not in done],
        "publication_copies": copies,
    }


def candidate_articles(*, source_id: int | None, days: int, ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Статьи для проверки: по списку id или все статьи источника, собранные за окно дней."""
    with repository.get_connection() as conn:
        if ids:
            rows = conn.execute(
                "SELECT id, title, url, published_at, collected_at FROM articles WHERE id = ANY(%s) ORDER BY id",
                (ids,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, title, url, published_at, collected_at FROM articles
                WHERE source_id = %s AND collected_at > now() - (%s::text || ' days')::interval
                ORDER BY id
                """,
                (source_id, days),
            ).fetchall()
    return [
        {"id": row[0], "title": row[1], "url": row[2], "published_at": row[3], "collected_at": row[4]}
        for row in rows
    ]
