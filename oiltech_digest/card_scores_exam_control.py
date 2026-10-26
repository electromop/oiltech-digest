"""Выгрузка с ядра для экзамена «Карточки» (#133): всё, что экзамен берёт из базы, — одним JSON рядом с манифестом.

Экзамен идёт там, где доступна модель, а база — на ядре (как otbor-exam-control). card-scores-exam-control выгружает:
критерии «Скоринга» обоих профилей (веса и ключевые слова: по ним код считает итог второго вызова), теги тем (13 тем и
кандидаты по ключам для основного вызова), контроль у строк пакета (тег и балл ленты, тема и балл судьи) и «сильные
сверх пакета» — выбранное «в дайджест» и эталонный ТОП заказчика (карточки, заведённые по его списку, и находки по
ссылкам эталона в памяти радара), кроме строк пакета, «другого» и публикаций без текста издателя. Экзамен проверяет
файл при загрузке набора, до вызовов модели: битый файл не должен сорвать оплаченный прогон.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from oiltech_digest.otbor_pack_format import CATEGORY_PROFILE, load_rubrics
from oiltech_digest.scoring_profiles import SCORING_PROFILES
from oiltech_digest.signal_card_pack import (_CONTROL_RE, _markable, _publisher_label, publication_day,
                                             publication_text)
from oiltech_digest.signal_reference import _url_key

NAME = "card_scores_exam_control.json"
_TAG_KEYS = ("id", "parent_id", "name", "name_en", "description", "keywords_json", "keywords_en_json",
             "negative_keywords_json", "sort_order", "parent_name", "parent_name_en")
_CRITERION_KEYS = ("id", "profile", "name", "description", "weight", "keywords_json", "keywords_en_json", "sort_order")
_STRONG_KEYS = ("id", "origin", "title", "text", "publisher", "publisher_type", "published", "received", "category")
SKIPPED = {"in_pack": "в пакете", "other": "категория «другое»", "no_text": "нет текста издателя",
           "unreadable": "не прочесть (страница блокировки или язык)"}


class ControlError(ValueError):
    pass


def control_values(manifest: dict[str, Any]) -> dict[str, Any]:
    """Выгрузка с ядра (там база) по манифесту пакета «Отбора»: критерии, теги, контроль у строк, «сильные»."""
    from oiltech_digest.db import card_scores_exam_store as store, repository

    items = manifest.get("items") if isinstance(manifest, dict) else None
    if not isinstance(items, list) or not all(isinstance(item, dict) and "id" in item for item in items):
        raise ControlError("не манифест пакета «Отбора»: нужны строки items")
    tags = [{key: _plain(tag.get(key)) for key in _TAG_KEYS} for tag in repository.list_enabled_tags()]
    criteria = {profile: [{key: _plain(one.get(key)) for key in _CRITERION_KEYS}
                          for one in repository.list_enabled_scoring_criteria(profile)] for profile in SCORING_PROFILES}
    articles = {int(item["article_id"]): item["id"] for item in items
                if item.get("origin") == "feed" and item.get("article_id")}
    signals = {int(item["signal_id"]): item["id"] for item in items
               if item.get("origin") == "radar" and item.get("signal_id")}
    roots = _roots(tags)
    feed, radar = store.feed_control(sorted(articles)), store.radar_control(sorted(signals))
    rows = {number: {"origin": "feed", "theme": roots.get(found.get("tag_id")), "score": _plain(found.get("score"))}
            for article, number in articles.items() for found in [feed.get(article) or {}]}
    rows.update({number: {"origin": "radar", "theme": found.get("theme"), "score": _plain(found.get("score"))}
                 for signal, number in signals.items() for found in [radar.get(signal) or {}]})
    pack: dict[str, set[Any]] = {"articles": set(articles), "signals": set(signals),
                                 "urls": {_url_key(item.get("url")) for item in items if item.get("url")}}
    reference = sorted({_url_key(url) for url in store.reference_urls()} - {""})
    strong, skipped = _strong(store.strong_radar(reference), store.strong_feed(), pack)
    return {"source": f"выгрузка с ядра {datetime.now(timezone.utc).isoformat(timespec='seconds')}", "tags": tags,
            "criteria": criteria, "rows": rows, "strong": strong, "skipped": skipped}


def _roots(tags: list[dict[str, Any]]) -> dict[Any, str]:
    """Тег → его корневая тематика (тег ленты бывает подтегом)."""
    by_id = {tag["id"]: tag for tag in tags}
    roots = {}
    for tag in tags:
        root, seen = tag, set()
        while root.get("parent_id") in by_id and root["id"] not in seen:
            seen.add(root["id"])
            root = by_id[root["parent_id"]]
        roots[tag["id"]] = root["name"]
    return roots


def _strong(radar: list[dict[str, Any]], feed: list[dict[str, Any]],
            pack: dict[str, set[Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """«Сильные сверх пакета»: публикация — по правилам пакета «Отбора» (текст издателя, сайт из ссылки у находки
    поиска); одна публикация — одна строка; без радарной категории — технология, как у «Карточки»; статья ленты без
    своего сигнала — бизнес."""
    strong: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    seen: set[str] = set()
    candidates = [("radar", f"signal:{row['signal_id']}", row, row.get("signal_id") in pack["signals"],
                   [name for name in ("digest", "top") if row.get(name)], row.get("signal_category") or "technology",
                   row.get("evidence_id") is not None) for row in radar]
    candidates += [("feed", f"article:{row['article_id']}", row, row.get("article_id") in pack["articles"],
                    ["digest"], row.get("signal_category") or "business", bool(row.get("has_text"))) for row in feed]
    for origin, number, row, in_pack, sources, category, has_text in candidates:
        url = _url_key(row.get("url"))
        if in_pack or (url and url in pack["urls"]) or (row.get("article_id") in pack["articles"]):
            skipped["in_pack"] += 1
            continue
        if category not in CATEGORY_PROFILE:
            skipped["other"] += 1
            continue
        if not has_text:
            skipped["no_text"] += 1
            continue
        item = {"id": number, "origin": origin, "sources": sources, "url": row.get("url") or "",
                "title": _CONTROL_RE.sub(" ", row.get("title") or "").strip(),
                "text": publication_text(row.get("text")),
                "publisher": _publisher_label(row), "publisher_type": row.get("publisher_type") or "",
                "language": row.get("language"), "published": publication_day(row.get("published")),
                "received": publication_day(row.get("received")), "category": category,
                **{key: row.get(key) for key in ("industry", "oilfield_relevance", "oilfield_application")}}
        if not _markable(item):
            skipped["unreadable"] += 1
            continue
        if (url or number) in seen:
            continue
        seen.add(url or number)
        strong.append(item)
    return strong, dict(skipped)


def _plain(value: Any) -> Any:
    """Значение базы — в JSON: числа (NUMERIC) — float, даты — строкой."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


# --- Чтение файла (экзамен) ----------------------------------------------------------------------


def read(folder: Path, ids: set[str]) -> dict[str, Any]:
    """Выгрузка с ядра рядом с манифестом: без неё экзамен не идёт — веса, ключевые слова и теги есть только в базе."""
    path = folder / NAME
    if not path.exists():
        raise ControlError(f"нет {NAME} рядом с манифестом: критерии «Скоринга», теги тем, контроль и «сильные сверх "
                           "пакета» выгружает с ядра card-scores-exam-control")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ControlError(f"{NAME}: не JSON ({exc})") from exc
    problems = _problems(data, ids)
    if problems:
        raise ControlError(f"{NAME}: " + "; ".join(problems))
    return data


def _problems(data: Any, ids: set[str]) -> list[str]:
    if not isinstance(data, dict):
        return ["нужен объект выгрузки card-scores-exam-control"]
    problems = []
    criteria, tags, rows, strong = (data.get(key) for key in ("criteria", "tags", "rows", "strong"))
    rubrics = load_rubrics()["profiles"]
    for profile in SCORING_PROFILES:
        found = (criteria or {}).get(profile) if isinstance(criteria, dict) else None
        if not (isinstance(found, list) and found and all(
                isinstance(one, dict) and isinstance(one.get("name"), str)
                and isinstance(one.get("weight"), (int, float)) for one in found)):
            problems.append(f"критерии профиля {profile} с именем и весом")
            continue
        if round(sum(float(one["weight"]) for one in found), 6) != 100:
            problems.append(f"сумма весов профиля {profile} не 100: итог исказился бы")
        if sorted(one["name"] for one in found) != sorted(one["name"] for one in rubrics[profile]):
            problems.append(f"критерии профиля {profile} не те, что в линейках: баллы эксперта не с чем сравнить")
    if not (isinstance(tags, list) and any(isinstance(tag, dict) and tag.get("name") and tag.get("parent_id") is None
                                           for tag in tags)):
        problems.append("теги тем (корневые тематики)")
    if not (isinstance(rows, dict) and all(isinstance(one, dict) for one in rows.values())):
        problems.append("контроль rows {строка: {theme, score}}")
    elif set(rows) - ids:
        problems.append(f"строк {', '.join(sorted(set(rows) - ids)[:5])} нет в манифесте: выгрузка другого пакета")
    if not (isinstance(strong, list) and all(isinstance(one, dict) and all(key in one for key in _STRONG_KEYS)
                                             and one["category"] in CATEGORY_PROFILE for one in strong)):
        problems.append("«сильные сверх пакета» strong со входами публикации и категорией бизнес или технология")
    elif {one["id"] for one in strong} & ids:
        problems.append("«сильные сверх пакета» среди строк пакета")
    return problems
