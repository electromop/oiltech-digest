"""Пакет эксперту для агента «Отбор» (#129, спека #128): разметка вслепую, справочники и линейки на утверждение.

Состав — решения владельца 21-22.10 (спека #128, «Набор и экзамен»), около 150-160 строк:
- прежняя разметка карточки (#94): связь с нефтесервисом у этих строк уже размечена — колонка заполнена
  ответом самого эксперта; входы — из прежнего манифеста (там поправки с первоисточника), а не из базы;
- случайные статьи ленты за 30 дней до --until: 50 отклонённых гейтом и 30 принятых; окно — в манифесте;
- 15-20 находок из «Отсеянных и скрытых» радара: брак судьи и ревью пачки, смешанные ссылки, старые события
  (по очереди из каждого вида); находки других этапов отсева (поиск, сбой судьи) в пакет не идут;
- неотсеянные находки радара с несколькими ссылками — для причины «разные события в одной находке»;
- накопленные материалы коллег, ещё не ставшие сигналом.

Вслепую: в таблице то, что получит агент, — заголовок, текст публикации, ссылка, издатель, даты публикации и
поступления, у находки радара — список её ссылок. Решений гейта, судьи и поиска, сути, темы, оценки автора
материала в ней нет; группа строки не видна ни колонкой, ни порядком. Правила текста и издателя — как у пакета
карточки (signal_card_pack): у находки «режима ChatGPT» дату публикации дала модель поиска, а имя издателя
записала модель, поэтому в таблице сайт из ссылки и пустая дата публикации (возраст события — от даты поступления).

Формат таблицы и манифеста, справочники и линейки — otbor_pack_format; запросы — db.otbor_pack_store. Таблица и
манифест пишутся вне репозитория — он публичный. Проверка размеченной таблицы — otbor_markup.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from oiltech_digest import colleague_materials, feed_window, otbor_pack_workbook
from oiltech_digest.db import otbor_pack_store, repository
from oiltech_digest.otbor_pack_format import (GIVEN_COLUMNS, MANIFEST_KIND, MANIFEST_NAME, MARKUP_SHEET, PROFILE_RU,
                                              RELEVANCE_COLUMN, WORKBOOK_NAME, PackError, answer_columns, fingerprint,
                                              lists, load_dictionaries, load_rubrics, open_book, score_columns,
                                              sheet_rows, text)
from oiltech_digest.scoring_profiles import SCORING_PROFILES
from oiltech_digest.signal_card_pack import (REPO_ROOT, TEXT_LIMIT, _CONTROL_RE, _file_mark, _markable,
                                             _publisher_label, _rank, publication_day, publication_text)

GROUPS = {
    "card_markup": "прежняя разметка карточки",
    "feed_rejected": "отклонённые гейтом статьи ленты",
    "feed_accepted": "принятые гейтом статьи ленты",
    "radar_filtered": "отсеянные и скрытые находки радара",
    "radar_links": "находки с несколькими ссылками",
    "materials": "материалы коллег",
}
QUOTAS = {"feed_rejected": 50, "feed_accepted": 30, "radar_filtered": 18, "radar_links": 10}
# Меньше этого экзамену группы не хватит: статей — сколько решил владелец, «Отсеянных» — 15-20 (спека), находки со
# ссылками нужны хотя бы какие-то.
MINIMUMS = {"feed_rejected": 50, "feed_accepted": 30, "radar_filtered": 15, "radar_links": 1}
FILTERED_KINDS = {"judge": "брак судьи", "review": "брак ревью пачки", "mixed": "смешанные ссылки",
                  "old": "старые события"}
FEED_WINDOW_DAYS = 30
LINKS_LIMIT = 8  # ссылок находки во входе «Отбора» (спека #128)
DEFAULT_SEED = 20261022
_MANIFEST_KEYS = ("id", "group", "kind", "origin", "article_id", "signal_id", "evidence_id", "material_id", "url",
                  "title", "text", "text_sha256", "text_chars", "published", "received", "publisher",
                  "publisher_type", "language", "links", "relevance", "overrides", "override_note", "file_note")


def default_until() -> str:
    """Окно закрыто сегодняшним днём (по Москве): то, что ещё собирается, выборку не сдвигает."""
    return datetime.now(feed_window.MSK).date().isoformat()


def export_pack(out_dir: str | Path | None, *, seed: int | None = None, until: str | None = None,
                apply: bool = False, card_manifest: str | Path | None = None,
                card_markup: str | Path | None = None) -> dict[str, Any]:
    seed = DEFAULT_SEED if seed is None else seed
    until = until or default_until()
    since = (date.fromisoformat(until) - timedelta(days=FEED_WINDOW_DAYS)).isoformat()
    card, card_pack = None, None
    if card_manifest or card_markup:
        card = _card_rows(card_manifest, card_markup)
        card_pack = {**_file_mark("manifest", str(card_manifest)), **_file_mark("markup", str(card_markup))}
    rows = select_rows(seed=seed, since=since, until=until, card=card or [])
    dictionaries, rubrics = load_dictionaries(), load_rubrics()
    blockers = _criteria_problems(rubrics)
    themes = _themes()
    if not themes:
        blockers.append("в базе нет тем (корневых тегов): в колонке «Тема» не из чего выбирать")
    missing = shortfall(rows, card is not None)
    result = {"count": len(rows), "composition": composition(rows), "shortfall": missing, "blockers": blockers,
              "seed": seed, "since": since, "until": until, "applied": False}
    if not apply:
        return result
    if missing or blockers:
        raise PackError("пакет не записан: " + "; ".join(missing + blockers))
    out = _checked_out_dir(out_dir)
    columns_lists = lists(dictionaries, themes)
    # Строки вперемешку и своим ключом: блоками групп таблица выдавала бы эксперту решение гейта и радара.
    rows = sorted(rows, key=lambda item: _rank("table", seed, item["id"]))
    out.mkdir(parents=True, exist_ok=True)
    otbor_pack_workbook.write(out / WORKBOOK_NAME, rows, dictionaries=dictionaries, rubrics=rubrics, themes=themes,
                              lists=columns_lists)
    manifest = {
        "kind": MANIFEST_KIND, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": seed, "window": {"since": since, "until": until, "days": FEED_WINDOW_DAYS},
        "recipe": {"dictionaries_version": dictionaries["version"], "rubrics_version": rubrics["version"],
                   "text_limit": TEXT_LIMIT, "links_limit": LINKS_LIMIT},
        "card_pack": card_pack, "composition": result["composition"],
        "columns": {"given": GIVEN_COLUMNS, "answers": answer_columns(rubrics)},
        "lists": columns_lists, "score_columns": score_columns(rubrics),
        "items": [{key: item.get(key) for key in _MANIFEST_KEYS} for item in rows],
    }
    (out / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**result, "applied": True, "workbook": str(out / WORKBOOK_NAME), "manifest": str(out / MANIFEST_NAME)}


def select_rows(*, seed: int, since: str, until: str, card: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Строки пакета по группам. Прежние строки карточки в другие группы не идут. Те же зерно и окно — те же строки:
    порядок в группе — по хэшу зерна, группы и номера, новый кандидат не перетасовывает остальных."""
    rows = list(card)
    articles = {item["article_id"] for item in rows if item.get("article_id")}
    signals = {item["signal_id"] for item in rows if item.get("signal_id")}
    feed = [item for item in _feed_items(since, until) if item["article_id"] not in articles and _markable(item)]
    for group in ("feed_rejected", "feed_accepted"):
        pool = [item for item in feed if item["group"] == group]
        rows += sorted(pool, key=lambda item: _rank(seed, group, item["id"]))[:QUOTAS[group]]
    radar = [item for item in _radar_items(until) if item["signal_id"] not in signals and _markable(item)]
    rows += _by_kinds([item for item in radar if item["group"] == "radar_filtered"], QUOTAS["radar_filtered"], seed)
    with_links = [item for item in radar if item["group"] == "radar_links" and item["link_count"] >= 2]
    rows += sorted(with_links, key=lambda item: _rank(seed, "radar_links", item["id"]))[:QUOTAS["radar_links"]]
    rows += [item for item in _material_items(until) if _markable(item)]
    _complete_from_base(rows)
    return rows


def composition(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"group": {group: count for group in GROUPS if (count := sum(item["group"] == group for item in rows))},
            "kind": dict(sorted(Counter(item["kind"] for item in rows if item.get("kind")).items()))}


def shortfall(rows: list[dict[str, Any]], card_given: bool) -> list[str]:
    counts = Counter(item["group"] for item in rows)
    missing = [] if card_given else [f"{GROUPS['card_markup']}: нужны --card-manifest и --card-markup"]
    return missing + [f"{GROUPS[group]} {counts[group]} из {minimum}"
                      for group, minimum in MINIMUMS.items() if counts[group] < minimum]


def _checked_out_dir(out_dir: str | Path | None) -> Path:
    if not out_dir:
        raise PackError("нужен --out: папка вне репозитория")
    out = Path(out_dir).expanduser().resolve()
    if out == REPO_ROOT or REPO_ROOT in out.parents:
        raise PackError(f"{out} внутри репозитория, а он публичный: укажите папку вне его")
    existing = [name for name in (WORKBOOK_NAME, MANIFEST_NAME) if (out / name).exists()]
    if existing:
        raise PackError(f"в {out} уже есть {', '.join(existing)}: разметку не затираю, укажите другую папку")
    return out


# --- Группы строк --------------------------------------------------------------------------------


def _card_rows(manifest_path: str | Path | None, markup_path: str | Path | None) -> list[dict[str, Any]]:
    """Прежняя разметка карточки (#94): строки и входы — из её манифеста, связь с нефтесервисом — ответ эксперта
    из заполненной таблицы. Таблица и манифест должны быть об одних и тех же текстах."""
    from oiltech_digest.signal_card import _plain

    if not (manifest_path and markup_path):
        raise PackError(f"{GROUPS['card_markup']}: нужны оба файла, --card-manifest и --card-markup")
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    items = manifest.get("items") if isinstance(manifest, dict) else None
    if not isinstance(items, list) or not all(
            isinstance(item, dict) and {"id", "text", "text_sha256"} <= item.keys() for item in items):
        raise PackError(f"{Path(manifest_path).name}: не манифест пакета карточки, нужен список items с id, text "
                        "и text_sha256")
    markup = Path(markup_path)
    answers = {row["ID"]: row for row in sheet_rows(open_book(markup), MARKUP_SHEET,
                                                    ["ID", "Текст публикации", RELEVANCE_COLUMN], markup.name)}
    allowed = {_plain(value["label"]): value["label"] for value in load_dictionaries()["oilfield_relevance"]["values"]}
    rows, problems = [], []
    for item in items:
        answer = answers.get(item["id"])
        relevance = text(answer.get(RELEVANCE_COLUMN)) if answer else ""
        if answer is None:
            problems.append(f"{item['id']}: нет строки в таблице")
        elif fingerprint(item["text"]) != item["text_sha256"] or fingerprint(
                answer.get("Текст публикации")) != item["text_sha256"]:
            problems.append(f"{item['id']}: текст не тот, что в манифесте")
        elif _plain(relevance) not in allowed:
            problems.append(f"{item['id']}: связь «{relevance}» не из справочника")
        else:
            # Издатель — как его видел эксперт: источник ленты или корпуса, у находки поиска — сайт из ссылки.
            publisher = item.get("publisher") if item.get("publisher_type") else _publisher_label(item)
            rows.append({**{key: item.get(key) for key in ("origin", "article_id", "signal_id", "evidence_id", "url",
                                                            "published", "publisher_type", "override_note")},
                         "id": item["id"], "group": "card_markup", "title": item.get("title") or "",
                         "text": item["text"], "text_sha256": item["text_sha256"], "text_chars": len(item["text"]),
                         "publisher": publisher or "", "relevance": allowed[_plain(relevance)],
                         "overrides": item.get("overrides") or [], "links": []})
    extra = sorted(set(answers) - {item["id"] for item in items})
    if extra:
        problems.append("в таблице лишние строки " + ", ".join(extra))
    if problems:
        raise PackError(f"{GROUPS['card_markup']} не сходится с манифестом: " + "; ".join(problems))
    return rows


def _feed_items(since: str, until: str) -> list[dict[str, Any]]:
    return [_item("feed_accepted" if row.pop("relevant") else "feed_rejected", "feed", f"article:{row['article_id']}",
                  row) for row in otbor_pack_store.feed_candidates(since, until)]


def _radar_items(until: str) -> list[dict[str, Any]]:
    """Находки радара: виды отсева спеки — в «Отсеянные», неотсеянные — кандидаты в находки со ссылками; находки
    других этапов отсева (поиск, сбой судьи) не идут никуда."""
    items = []
    for row in otbor_pack_store.radar_candidates(until):
        group = ("radar_filtered" if row["kind"] in FILTERED_KINDS
                 else "radar_links" if row["kind"] is None else None)
        if group:
            items.append(_item(group, "radar", f"signal:{row['signal_id']}", {**row, "publisher": _publisher_label(row)}))
    return items


def _material_items(until: str) -> list[dict[str, Any]]:
    items = []
    for row in otbor_pack_store.material_candidates(until):
        material_text, note = colleague_materials.publication_text(row)
        items.append(_item("materials", "material", f"material:{row['id']}", {
            "material_id": row["id"], "title": row["title"], "text": material_text, "url": row["url"],
            "published": row["created_at"], "received": row["created_at"],
            "publisher": _publisher_label(row) if row["url"] else "", "publisher_type": "", "language": None,
            "file_note": note}))
    return items


def _item(group: str, origin: str, item_id: str, row: dict[str, Any]) -> dict[str, Any]:
    body = publication_text(row.get("text"))
    return {**row, "group": group, "kind": row.get("kind") if group == "radar_filtered" else None,
            "origin": origin, "id": item_id, "title": _CONTROL_RE.sub(" ", row.get("title") or "").strip(),
            "text": body, "text_sha256": fingerprint(body), "text_chars": len(body),
            "published": publication_day(row.get("published")), "received": publication_day(row.get("received")),
            "links": [], "overrides": []}


def _by_kinds(items: list[dict[str, Any]], count: int, seed: int) -> list[dict[str, Any]]:
    """По одной из каждого вида отсева по кругу — чтобы брак судьи не съел группу; порядок внутри вида — по хэшу."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[item["kind"]].append(item)
    queues = [sorted(group, key=lambda item: _rank(seed, "radar_filtered", item["id"]))
              for _, group in sorted(groups.items())]
    chosen: list[dict[str, Any]] = []
    depth = 0
    while len(chosen) < count and any(depth < len(queue) for queue in queues):
        chosen += [queue[depth] for queue in queues if depth < len(queue)][:count - len(chosen)]
        depth += 1
    return chosen


def _complete_from_base(rows: list[dict[str, Any]]) -> None:
    """Ссылки находки (до LINKS_LIMIT, если их больше одной) — у всех строк радара, в том числе прежних; язык и дата
    поступления — у прежних строк (в прежнем манифесте их нет). У ссылки поиска — сайт из адреса и пустая дата, у
    ссылки «режима ChatGPT» без докачки и заголовок пустой (его написала модель поиска)."""
    card = [item for item in rows if item["group"] == "card_markup"]
    articles, signals = otbor_pack_store.card_row_facts(
        sorted({int(item["article_id"]) for item in card if item.get("article_id")}),
        sorted({int(item["signal_id"]) for item in card if item.get("signal_id")}))
    links = otbor_pack_store.evidence_links(
        sorted({int(item["signal_id"]) for item in rows if item["origin"] == "radar" and item.get("signal_id")}))
    for item in rows:
        found = links.get(int(item["signal_id"])) if item["origin"] == "radar" and item.get("signal_id") else None
        item["links"] = [{"url": link["url"],
                          "title": _CONTROL_RE.sub(" ", link["title"]).strip() if link["title"] else None,
                          "publisher": _publisher_label(link), "published": publication_day(link["published"])}
                         for link in found[:LINKS_LIMIT]] if found and len(found) > 1 else []
        if item["group"] != "card_markup":
            continue
        article = articles.get(int(item["article_id"])) if item.get("article_id") else None
        item["language"] = article["language"] if article else None
        arrived = (signals.get(int(item["signal_id"])) if item["origin"] == "radar" and item.get("signal_id")
                   else article["collected_at"] if article else None)
        item["received"] = publication_day(arrived)


# --- Справочная часть ----------------------------------------------------------------------------


def _criteria_problems(rubrics: dict[str, Any]) -> list[str]:
    """Расхождения наборов экрана «Скоринг» с линейками: баллы эксперта должны быть о тех же критериях, по которым
    платформа посчитает итог."""
    problems = []
    for profile in SCORING_PROFILES:
        screen = [str(one["name"]) for one in repository.list_enabled_scoring_criteria(profile)]
        drafted = [one["name"] for one in rubrics["profiles"][profile]]
        missing = [name for name in drafted if name not in screen]
        extra = [name for name in screen if name not in drafted]
        parts = []
        if missing:
            parts.append("в линейках есть, на экране нет " + ", ".join(f"«{one}»" for one in missing))
        if extra:
            parts.append("на экране есть, в линейках нет " + ", ".join(f"«{one}»" for one in extra))
        if parts:
            problems.append(f"критерии «{PROFILE_RU[profile]}» на экране «Скоринг» не те, что в линейках: "
                            + "; ".join(parts))
    return problems


def _themes() -> list[dict[str, str]]:
    """Темы — корневые тематики заказчика без служебного приёмника, как у радара (topics_from_tags)."""
    from oiltech_digest.signal_discovery import topics_from_tags

    return [{"name": topic["name"], "description": topic["description"]}
            for topic in topics_from_tags(repository.list_enabled_tags())]
