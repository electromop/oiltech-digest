"""Починка уже сохранённых тел статей новым извлечением.

Дефект 18.09 (скриншот заказчика 16.09): у Neftegaz.ru страница новости отдаёт ленту
из нескольких статей, и тело бралось из самого длинного блока. За 60 дней так
испорчено около двух третей статей источника; у 472 видимых суть и баллы посчитаны
по чужому тексту. Извлечение исправлено в efbaefe — здесь починка строк в базе.

Правило замены намеренно узкое: заменяем, только если у сохранённого тела есть
доказуемый дефект (чужое, кракозябры, «простыня») И новое тело — своё. Ровно то же
тело (с точностью до счётчиков и шапки) не трогаем: перерасчёт ИИ стоит денег.

Дефект «хвост» (#140) чинится иначе: текст не заменяется, от него отрезается конец, и отрезанное
ложится рядом (`articles.text_tail`). Хвост ищется по тем же двум правилам, что и при сборе: заголовок
хвоста и повтор у других статей источника видны по самому тексту, страница нужна, только если по
тексту резать нельзя. У АНГИ так почти все статьи: комментарии длиннее новости, и срез больше половины.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import partial
import logging
import re
import time
from typing import Any, Callable

from oiltech_digest import config
from oiltech_digest.db import article_tail_store, repository
from oiltech_digest.ingestion import article_tails, normalize
from oiltech_digest.ingestion.article_fetcher import extract_main_text

logger = logging.getLogger(__name__)

# Доля слов заголовка в начале тела. Старое извлечение брало блок статьи ВМЕСТЕ с
# шапкой, поэтому у своего тела заголовок стоит в первых сотнях знаков. Выборка 18.09:
# 10 случайных тел ниже порога — 10 чужих.
FOREIGN_SHARE = 0.5
HEAD_CHARS = 800
OVERSIZED_CHARS = 50_000
# Доля слов заголовка во ВСЁМ новом теле — второй страж замены: общий страж
# принадлежности (20%) пропускает «росси»+«энерг» у чужой новости. Замер 18.09 на
# 71 замене Neftegaz: у новых тел 0,5–1,0 (60 из 71 — 0,9+), у старых чужих — 0–0,25.
OWN_SHARE = 0.4
_MOJIBAKE_RE = re.compile(
    "(?:[ÐÑÃ][" + re.escape("".join(chr(c) for c in range(0x80, 0xC0))) + "]|â€)"
)
_MOJIBAKE_MIN_HITS = 5


@dataclass(frozen=True)
class RepairDecision:
    article_id: int
    action: str  # replace | skip
    defect: str | None
    reason: str
    old_len: int
    new_len: int
    old_head: str
    new_head: str


def title_share_in_head(title: str, text: str) -> float:
    title_words = set(normalize.significant_words(title))
    if not title_words:
        return 1.0
    head_words = set(normalize.significant_words((text or "")[:HEAD_CHARS]))
    return len(title_words & head_words) / len(title_words)


def title_share_in_text(title: str, text: str) -> float:
    title_words = set(normalize.significant_words(title))
    if not title_words:
        return 1.0
    return len(title_words & set(normalize.significant_words(text or ""))) / len(title_words)


def is_mojibake(text: str) -> bool:
    return len(_MOJIBAKE_RE.findall(text or "")) >= _MOJIBAKE_MIN_HITS


def stored_body_defect(title: str, text: str, repeated: article_tails.RepeatedTail | None = None) -> str | None:
    """Что не так с сохранённым телом: mojibake | oversized | truncated | foreign | tail | None.

    truncated — обрывок короче MIN_FULL_TEXT_CHARS. У Neftegaz.ru их 246 за 60 дней:
    дозагрузка вытаскивала чужой блок, страж его отбивал, и оставалась строка лида
    из RSS (51–120 знаков) — ИИ судил новость по одной фразе.

    tail: в конце чужой блок, заголовок хвоста в самом тексте или `repeated`, повтор у других
    статей источника (его находит stored_repeated_tail, это запрос в базу)."""
    if is_mojibake(text):
        return "mojibake"
    if len(text or "") > OVERSIZED_CHARS:
        return "oversized"
    if len(text or "") < config.MIN_FULL_TEXT_CHARS:
        return "truncated"
    if title_share_in_head(title, text) < FOREIGN_SHARE:
        return "foreign"
    return "tail" if article_tails.has_tail(text, repeated) else None


def _article_key(text: str) -> str:
    return re.sub(r"[\W\d_]+", "", (text or "").lower())


def same_article(old: str, new: str) -> bool:
    """Одна и та же статья с точностью до счётчиков просмотров, дат и шапки."""
    old_key, new_key = _article_key(old), _article_key(new)
    if not old_key or not new_key:
        return False
    return old_key[:200] in new_key or new_key[:200] in old_key


def _decide(article: dict[str, Any], action: str, defect: str | None, reason: str,
            new: str = "") -> tuple[RepairDecision, str]:
    old = str(article.get("raw_text") or "")
    item = RepairDecision(int(article["id"]), action, defect, reason, len(old), len(new),
                          old[:160].replace("\n", " "), new[:160].replace("\n", " "))
    return item, (new if action == "replace" else "")


def plan_repair(article: dict[str, Any], content: bytes | str | None,
                repeated: article_tails.RepeatedTail | None = None) -> tuple[RepairDecision, str]:
    """Решение по одной статье и новое тело (пустое, если не заменяем)."""
    title = str(article.get("title") or "")
    old = str(article.get("raw_text") or "")
    decision = partial(_decide, article)

    defect = stored_body_defect(title, old, repeated)
    if defect is None:
        return decision("skip", None, "stored body looks own")
    if defect == "tail":
        return plan_tail(article, content, repeated)
    if is_mojibake(title):
        # Испорчен сам заголовок: якорь по нему не найдётся, а правка заголовка меняет
        # content_hash (уникальность ленты) — это отдельная работа, здесь только отчёт.
        return decision("skip", defect, "title is mojibake")
    if not content:
        return decision("skip", defect, "page not downloaded")
    new = extract_main_text(content, title=title)
    # «Не длиннее старого» — только для обрывка: своё тело бывает короче чужого
    # (ОДК 3,6 тыс. знаков против «Гидры» 4,2 тыс.), а простыня длиннее по определению.
    if len(new) < config.MIN_FULL_TEXT_CHARS or (defect == "truncated" and len(new) <= len(old)):
        return decision("skip", defect, "new text too short", new)
    if len(new) > OVERSIZED_CHARS:
        return decision("skip", defect, "new text oversized", new)
    if not normalize.title_matches_body(title, new) or title_share_in_text(title, new) < OWN_SHARE:
        return decision("skip", defect, "new text does not match title", new)
    # Только для «чужого»: у «простыни» своя статья лежит ВНУТРИ старого тела, и эта
    # проверка ошибочно сочла бы их одним и тем же.
    if defect == "foreign" and same_article(old, new):
        return decision("skip", defect, "same article as stored", new)
    return decision("replace", defect, "own body extracted", new)


def plan_tail(article: dict[str, Any], content: bytes | str | None,
              repeated: article_tails.RepeatedTail | None) -> tuple[RepairDecision, str]:
    """Решение по хвосту и то, что останется от текста (остаток всегда начало прежнего текста).

    Со страницы берём только границу: где кончается её текст в сохранённом. Предохранитель правила повторов
    здесь не действует, порог тот же, что у сбора (MIN_ARTICLE_TEXT_CHARS): свежий сбор этой страницы дал бы
    столько же текста. Так чистятся и статьи, где комментарии длиннее новости (АНГИ, Белоруснефть)."""
    title = str(article.get("title") or "")
    old = str(article.get("raw_text") or "")
    offset = article_tails.stored_tail_offset(old, repeated)
    if offset is not None:
        return _decide(article, "replace", "tail", "tail cut", old[:offset])
    if not content:
        return _decide(article, "skip", "tail", "page not downloaded")
    new = extract_main_text(content, title=title)
    offset = article_tails.end_of_text_in(old, new)
    if offset is None or not normalize.title_matches_body(title, new):
        return _decide(article, "skip", "tail", "page text differs from stored", new)
    if offset < config.MIN_ARTICLE_TEXT_CHARS:
        return _decide(article, "skip", "tail", "new text too short", new)
    if offset >= len(old.rstrip()):
        return _decide(article, "skip", "tail", "page has no tail to cut", new)
    if not article_tails.page_end_is_plausible(old, offset, repeated):
        return _decide(article, "skip", "tail", "page ends before the tail", new)
    return _decide(article, "replace", "tail", "tail cut by page", old[:offset])


def stored_repeated_tail(article: dict[str, Any]) -> article_tails.RepeatedTail | None:
    """Хвост сохранённого текста, повторяющийся у других статей источника вокруг этой статьи."""
    with repository.get_connection() as conn:
        return article_tail_store.repeated_tail(
            conn, article.get("source_id"), str(article.get("title") or ""), str(article.get("raw_text") or ""),
            article_id=int(article["id"]), around=article.get("collected_at"),
            already_cut=len(article.get("text_tail") or ""),
        )


def _store_tail(article: dict[str, Any], kept: str) -> None:
    old = str(article["raw_text"])
    with repository.get_connection() as conn:
        article_tail_store.store_cut(conn, int(article["id"]), old, kept,
                                     old[len(kept):] + str(article.get("text_tail") or ""))
        conn.commit()


def _download(article: dict[str, Any], fetch_fn: Callable[[str], bytes | str | None]) -> bytes | str | None:
    try:
        return fetch_fn(str(article["url"]))
    except Exception as exc:  # noqa: BLE001 - одна статья не валит починку
        logger.warning("body_repair fetch failed article=%s: %s", article.get("id"), exc)
        return None


def repair_bodies(
    articles: list[dict[str, Any]],
    *,
    apply: bool = False,
    fetch_fn: Callable[[str], bytes | str | None] | None = None,
    pause_seconds: float = 0.5,
    find_tail: Callable[[dict[str, Any]], article_tails.RepeatedTail | None] | None = None,
    only_tails: bool = False,
    all_pages: bool = False,
) -> dict[str, Any]:
    """Перекачать страницы статей с дефектным телом и заменить тело своим.

    `find_tail`: поиск повтора у других статей источника (stored_repeated_tail). `only_tails`: смотреть
    только на хвосты, прочие дефекты не качаются и не меняются. `all_pages`: при этом качать страницу и там,
    где хвост не виден по тексту: общее правило видит и хвост без повтора и заголовка (свои комментарии).

    Возвращает счётчики, id заменённых (для перерасчёта ИИ), выборку решений и сводку хвостов по
    источникам: сколько статей и знаков уйдёт."""
    if fetch_fn is None:
        from oiltech_digest.ingestion.http_client import fetch as fetch_fn  # noqa: PLW0127
    decisions: list[RepairDecision] = []
    replaced: list[int] = []
    tails: dict[Any, dict[str, Any]] = {}
    for article in articles:
        title = str(article.get("title") or "")
        old = str(article.get("raw_text") or "")
        repeated = find_tail(article) if find_tail else None
        if only_tails:
            defect = "tail" if all_pages or article_tails.has_tail(old, repeated) else None
        else:
            defect = stored_body_defect(title, old, repeated)
        if defect is None:
            decisions.append(_decide(article, "skip", None, "stored body looks own")[0])
            continue
        needs_page = defect != "tail" or article_tails.stored_tail_offset(old, repeated) is None
        content = _download(article, fetch_fn) if needs_page else None
        decision, new = (plan_tail if only_tails else plan_repair)(article, content, repeated)
        decisions.append(decision)
        if decision.action == "replace":
            replaced.append(int(article["id"]))
            if defect == "tail":
                row = tails.setdefault(article.get("source_id"), {
                    "source_id": article.get("source_id"), "source": article.get("source_name"),
                    "articles": 0, "chars": 0,
                })
                row["articles"] += 1
                row["chars"] += len(old) - len(new)
                if apply:
                    _store_tail(article, new)
            elif apply:
                repository.update_article_full_text(
                    int(article["id"]), new, normalize.is_truncated(new), "ok", "lxml",
                )
        if needs_page and pause_seconds:
            time.sleep(pause_seconds)
    stats: dict[str, int] = {}
    for item in decisions:
        key = f"{item.action}:{item.defect or 'none'}:{item.reason}"
        stats[key] = stats.get(key, 0) + 1
    return {
        "apply": apply,
        "checked": len(decisions),
        "replaced": len(replaced),
        "replaced_ids": replaced,
        "stats": dict(sorted(stats.items())),
        "tails_by_source": sorted(tails.values(), key=lambda row: -row["chars"]),
        "sample": [asdict(item) for item in decisions if item.action == "replace"][:15]
        + [asdict(item) for item in decisions if item.action == "skip" and item.defect][:15],
    }


_CANDIDATE_COLUMNS = ("a.id, a.source_id, a.title, a.url, a.raw_text, a.text_tail, a.collected_at, "
                      "s.name AS source_name")
_CANDIDATE_FROM = "FROM articles a JOIN sources s ON s.id = a.source_id"


def candidate_articles(
    *,
    source_id: int | None,
    days: int,
    ids: list[int] | None = None,
    statuses: list[str] | None = None,
    all_sources: bool = False,
) -> list[dict[str, Any]]:
    """Статьи для починки: по списку id, по источнику за окно дней, по статусу
    дозагрузки у обрывков всех источников или все статьи окна (`all_sources`).

    По статусу не берём Telegram (у поста нет HTTP-тела — короткий пост это формат, а
    не обрыв) и источники зарубежного контура (с РФ они 403; их тела добирает
    NL-воркер — enqueue-external-refetch)."""
    with repository.get_connection() as conn:
        if ids:
            rows = conn.execute(
                f"SELECT {_CANDIDATE_COLUMNS} {_CANDIDATE_FROM} WHERE a.id = ANY(%s) ORDER BY a.id",
                (ids,),
            ).fetchall()
        elif statuses:
            rows = conn.execute(
                f"""
                SELECT {_CANDIDATE_COLUMNS} {_CANDIDATE_FROM}
                WHERE a.full_text_status = ANY(%s)
                  AND a.collected_at > now() - (%s::text || ' days')::interval
                  AND length(coalesce(a.raw_text, '')) < %s
                  AND s.parse_strategy <> 'telegram'
                  AND coalesce(s.network_region, 'auto') <> 'external'
                  AND (%s::bigint IS NULL OR a.source_id = %s::bigint)
                ORDER BY a.id
                """,
                (statuses, days, config.MIN_FULL_TEXT_CHARS, source_id, source_id),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT {_CANDIDATE_COLUMNS} {_CANDIDATE_FROM}
                WHERE (%s OR a.source_id = %s) AND a.collected_at > now() - (%s::text || ' days')::interval
                  AND a.raw_text IS NOT NULL
                ORDER BY a.id
                """,
                (all_sources and source_id is None, source_id, days),
            ).fetchall()
    return [
        {"id": row[0], "source_id": row[1], "title": row[2], "url": row[3], "raw_text": row[4],
         "text_tail": row[5], "collected_at": row[6], "source_name": row[7]}
        for row in rows
    ]
