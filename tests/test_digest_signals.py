"""Сигнал радара в месячном выпуске — не статья (баг до 28.09).

Номер сигнала уезжал в article_id: сохранение черновика падало на внешнем ключе
monthly_digest_items.article_id → articles или молча цепляло чужую статью с тем же
номером, а после сохранения сигнал из выпуска пропадал.
"""

from oiltech_digest.processing import digest
from oiltech_digest.processing.digest import build_digest_content, save_digest_draft


def _row(row_id, *, item_type="article", title=None):
    return {
        "id": row_id,
        "item_type": item_type,
        "title": title or f"{item_type} {row_id}",
        "source_name": "World Oil" if item_type == "article" else "Технологический радар",
        "url": f"https://example.com/{item_type}/{row_id}",
        "published_at": None,
        "tag_name": "Бурение",
        "parent_tag_name": None,
        "total_score": 80,
        "score_label": "High",
        "summary": f"Суть {row_id}",
        "image_url": "",
    }


def _patch(monkeypatch, *, saved_items=None, candidates=None):
    monkeypatch.setattr(
        digest.repository,
        "get_monthly_digest",
        lambda month, user_id=None: {"id": 1, "month": month, "items": saved_items} if saved_items else None,
    )
    monkeypatch.setattr(
        digest.repository,
        "digest_items_by_article_ids",
        lambda ids, selected_by=None, month=None: [_row(article_id) for article_id in ids],
    )
    monkeypatch.setattr(
        digest.repository,
        "digest_candidates",
        lambda month, limit=20, min_score=60, user_id=None, **kwargs: list(candidates or []),
    )


def test_signal_item_carries_signal_id_not_article_id(monkeypatch):
    _patch(monkeypatch, candidates=[_row(10), _row(10, item_type="signal", title="Сигнал радара")])

    content = build_digest_content("2026-09", user_id=3)

    by_type = {item["item_type"]: item for item in content["news"]}
    assert by_type["article"]["article_id"] == 10 and by_type["article"]["signal_id"] is None
    # Номер 10 у сигнала и у статьи совпадает — связь больше не путается.
    assert by_type["signal"]["article_id"] is None and by_type["signal"]["signal_id"] == 10


def test_saved_draft_keeps_selected_radar_signals(monkeypatch):
    _patch(
        monkeypatch,
        saved_items=[{"article_id": 11}],
        candidates=[_row(11), _row(7, item_type="signal", title="Сигнал радара")],
    )

    content = build_digest_content("2026-09", user_id=3)

    assert [(item["item_type"], item["article_id"], item["signal_id"]) for item in content["news"]] == [
        ("article", 11, None),
        ("signal", None, 7),
    ]


def test_draft_save_stores_articles_only(monkeypatch):
    _patch(monkeypatch, candidates=[_row(10), _row(7, item_type="signal")])
    saved = {}

    def fake_save(**kwargs):
        saved.update(kwargs)
        return {"id": 1, "month": kwargs["month"], "items": kwargs["items"]}

    monkeypatch.setattr(digest.repository, "save_monthly_digest", fake_save)

    save_digest_draft("2026-09", user_id=3)

    assert [item["article_id"] for item in saved["items"]] == [10]


def test_hidden_draft_articles_fall_back_to_current_selection(monkeypatch):
    # Все статьи черновика с тех пор скрыты (перепечатка, удаление) — выпуск как раньше
    # берёт текущую подборку, а не одни сигналы.
    _patch(monkeypatch, saved_items=[{"article_id": 11}], candidates=[_row(12), _row(7, item_type="signal")])
    monkeypatch.setattr(digest.repository, "digest_items_by_article_ids", lambda ids, selected_by=None, month=None: [])

    content = build_digest_content("2026-09", user_id=3)

    assert [(item["item_type"], item["article_id"] or item["signal_id"]) for item in content["news"]] == [
        ("article", 12),
        ("signal", 7),
    ]
