"""Тема и балл публикации в выпуске и на экране «Дайджест»: у сигнала ленты, у статьи без него у самой статьи
(решение владельца 08.11: тег и балл статьи сняты вместе со старой цепочкой).

Тот же путь, что в test_digest_user_path.py: обычный пользователь, настоящая база и те же запросы, что шлёт экран
(конструктор выпуска, письмо, DOCX, PDF, /api/articles). Часы окна ленты заморожены на 29.09 12:00 МСК.
"""

from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

from oiltech_digest.db import connection, repository
from oiltech_digest.scoring_profiles import SCORING_PRESETS
from tests.digest_user_path_support import DRILLING, ENERGY, MARKET, SEPT, add_article, breakdown, set_card, utc
from tests.digest_user_path_support import issue  # noqa: F401 (фикстура выпуска)


def _tag(name: str, *, parent: str | None = None) -> int:
    """Тег экрана «Теги»: корневая тематика или дочерний тег под ней (корень заводится, если его нет)."""
    with connection.get_connection() as conn:
        parent_id = None
        if parent is not None:
            row = conn.execute("SELECT id FROM tags WHERE name = %s AND parent_id IS NULL", (parent,)).fetchone()
            parent_id = row[0] if row else conn.execute(
                "INSERT INTO tags (name, enabled) VALUES (%s, TRUE) RETURNING id", (parent,)).fetchone()[0]
        tag_id = conn.execute("INSERT INTO tags (name, parent_id, enabled) VALUES (%s, %s, TRUE) RETURNING id",
                              (name, parent_id)).fetchone()[0]
        conn.commit()
    return int(tag_id)


def _tag_article(article_id: int, tag_id: int) -> None:
    """Тег статьи, как его ставила старая цепочка."""
    with connection.get_connection() as conn:
        conn.execute("INSERT INTO article_tags (article_id, tag_id) VALUES (%s, %s)", (article_id, tag_id))
        conn.commit()


def _otbor_article(issue, slug: str, *, day: int, theme: str, score: float = 0, band: str | None = None,
                   article_score: float | None = None) -> tuple[int, int]:
    """Статья, принятая «Отбором», и её сигнал ленты (ключ feed:<статья>): тема «Карточки», балл и ступень её второго
    вызова. Без article_score своего балла у статьи нет, как после снятия старой цепочки; с ним старая цепочка успела
    поставить статье и свой балл (так на проде до 08.11)."""
    published = utc(2026, 9, day, 12)
    with connection.get_connection() as conn:
        article_id = add_article(conn, issue["source"], slug, published=published, score=article_score)
        conn.execute("UPDATE articles SET selector = 'otbor' WHERE id = %s", (article_id,))
        conn.execute("UPDATE article_cards SET relevant = NULL WHERE article_id = %s", (article_id,))
        signal_id = conn.execute(
            "INSERT INTO signals (signal_key, origin, origin_article_id, signal_category, title, theme, maturity,"
            " otbor_decision, score, score_band, first_seen_at, last_seen_at)"
            " VALUES (%s, 'feed', %s, 'business', %s, %s, 'watch', 'accept', %s, %s, %s, %s) RETURNING id",
            (f"feed:{article_id}", article_id, f"Материал {slug}", theme, score, band, published, published),
        ).fetchone()[0]
        conn.commit()
    return article_id, int(signal_id)


def _issue_items(person, **params) -> list[dict]:
    """Позиции выпуска сентября с фильтрами экрана «Дайджест» (тема, поиск, оценка от и до)."""
    response = person.get("/api/digest-content",
                          params={"month": SEPT, "limit": 500, "min_score": 0, "max_score": 100, **params})
    assert response.status_code == 200, response.text
    return response.json()["news"]


def _digest_screen(person, **params) -> dict[int, dict]:
    """Статьи экрана «Дайджест» (/api/articles), по номеру."""
    rows = person.get("/api/articles", params={"limit": 5000, **params}).json()
    return {row["id"]: row for row in rows}


def _materials(person) -> dict[int, dict]:
    """Карточки PDF выпуска сентября, по номеру статьи."""
    return {item["article_id"]: item for section in person.materials().values() for item in section
            if item["item_type"] == "article"}


def test_issue_takes_theme_and_score_of_a_publication_from_its_feed_signal(issue):
    """Решение владельца 08.11: тег и балл статьи снимаются вместе со старой цепочкой, тема и балл публикации живут в
    её сигнале ленты. До 08.11 старая цепочка ставила свой тег и балл и статьям «Отбора», и письмо, DOCX, фильтр по
    теме, порог и порядок выпуска шли по ним, а бизнес-лента и PDF по сигналу. Статья без сигнала ленты живёт по своим
    тегу и баллу, как раньше: рубрика «родитель / тег»."""
    analyst, a = issue["analyst"], issue["a"]
    deals = _tag("Сделки", parent=MARKET)
    # «Карточка» дала бурение и 91 («сильный»); старая цепочка той же статье дала «рынок / сделки» и 30.
    otbor, _ = _otbor_article(issue, "otbor", day=14, theme=DRILLING, score=91, band="strong", article_score=30)
    _tag_article(otbor, deals)
    _tag_article(a["mid"], deals)
    for article_id in (a["mid"], a["hi"], otbor):
        analyst.mark(article_id)

    # Без черновика статьи идут по баллу публикации: 91 у сигнала, затем 88 и 64 у статей без сигнала.
    items = _issue_items(analyst)
    assert [(item["article_id"], item["category"], item["score"], item["score_label"]) for item in items] == [
        (otbor, DRILLING, 91.0, "Сильный"),
        (a["hi"], "Без тега", 88.0, "Высокая"),
        (a["mid"], f"{MARKET} / Сделки", 64.0, "Средняя"),
    ]
    # Фильтр по теме, поиск и порог экрана: по теме и баллу публикации, у статьи без сигнала по её тегу (и родителю).
    assert [item["article_id"] for item in _issue_items(analyst, top_tag=DRILLING)] == [otbor]
    assert [item["article_id"] for item in _issue_items(analyst, top_tag=MARKET)] == [a["mid"]]
    assert [item["article_id"] for item in _issue_items(analyst, search="буровое оборудование")] == [otbor]
    assert [item["article_id"] for item in _issue_items(analyst, min_score=90)] == [otbor]
    assert [item["article_id"] for item in _issue_items(analyst, max_score=50)] == []
    # PDF ставит карточки по тому же баллу, что письмо.
    assert {key: item["score"] for key, item in _materials(analyst).items()} == {otbor: 91.0, a["hi"]: 88.0,
                                                                                a["mid"]: 64.0}

    # Письмо и DOCX: рубрика карточки по теме публикации.
    letter = analyst.get("/api/digest-email", params={"month": SEPT, "limit": 500, "min_score": 0}).text
    assert f'<span class="news-card-tag">{DRILLING}</span>' in letter
    assert '<span class="news-card-tag">Без тега</span>' in letter
    with ZipFile(BytesIO(analyst.export("docx"))) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert f"{DRILLING} · World Oil" in document_xml

    # Черновик задаёт порядок, тема и балл те же.
    assert analyst.save(SEPT, [a["mid"], otbor, a["hi"]]).status_code == 200
    assert [(item["article_id"], item["category"], item["score"]) for item in _issue_items(analyst)] == [
        (a["mid"], f"{MARKET} / Сделки", 64.0), (otbor, DRILLING, 91.0), (a["hi"], "Без тега", 88.0),
    ]


def test_digest_screen_shows_the_same_score_of_a_publication_as_the_business_feed(issue):
    """Экран «Дайджест» берёт статьи из /api/articles: тема, балл, подпись и разбивка по критериям там те же, что у
    сигнала ленты на бизнес-ленте. Подпись по правилу витрин: ступень второго вызова «Карточки», у прежнего балла
    пороги ленты 80/65/40."""
    analyst, a = issue["analyst"], issue["a"]
    otbor, signal_id = _otbor_article(issue, "otbor", day=14, theme=DRILLING, score=91, band="strong",
                                      article_score=30)
    # «Свежесть» без даты события в тексте в итог не входит: в разбивке её нет, «0» солгал бы.
    set_card(signal_id, **breakdown("business", 95, None, 80))
    for article_id in (otbor, a["mid"]):
        analyst.mark(article_id)

    rows = _digest_screen(analyst, status="digest")
    [card] = [card for card in analyst.get("/api/signals", params={"showcase": "business"}).json()
              if card["id"] == signal_id]
    assert (rows[otbor]["tag"], rows[otbor]["score"], rows[otbor]["rating"]) == (DRILLING, 91.0, "Сильный")
    assert (card["theme"], card["score"], card["score_label"]) == (DRILLING, 91.0, "Сильный")
    preset = SCORING_PRESETS["viktor"]["business"]
    assert [(item["name"], item["weight"], item["final_score"]) for item in rows[otbor]["score_items"]] == [
        (preset[0]["name"], float(preset[0]["weight"]), 95.0), (preset[2]["name"], float(preset[2]["weight"]), 80.0),
    ]
    # Статья без сигнала ленты: прежние тег и балл статьи, подпись по порогам ленты.
    assert (rows[a["mid"]]["tag"], rows[a["mid"]]["score"], rows[a["mid"]]["rating"]) == ("Без тега", 64.0,
                                                                                          "Средняя")


def test_otbor_article_without_its_own_tag_and_score_goes_by_its_feed_signal(issue):
    """Так будет после снятия стадий тега и балла статьи: у статьи «Отбора» нет ни тега, ни балла статьи, только тема
    «Карточки» и балл её второго вызова в сигнале ленты. Сигнал, которого второй вызов ещё не оценил, идёт «без
    баллов», как на бизнес-ленте: в выпуске последним, на экране «Дайджест» с нулём и «Без оценки»."""
    analyst, a = issue["analyst"], issue["a"]
    scored, _ = _otbor_article(issue, "energy", day=15, theme=ENERGY, score=66, band="useful")
    waiting, _ = _otbor_article(issue, "waiting", day=16, theme=repository.SYSTEM_TAG_UNCLASSIFIED)
    for article_id in (waiting, scored, a["hi"]):
        analyst.mark(article_id)

    assert [(item["article_id"], item["category"], item["score"], item["score_label"])
            for item in _issue_items(analyst)] == [
        (a["hi"], "Без тега", 88.0, "Высокая"),
        (scored, ENERGY, 66.0, "Полезный"),
        (waiting, repository.SYSTEM_TAG_UNCLASSIFIED, None, None),
    ]
    # Порог «Оценка от» выпуска балла, которого нет, не пропускает, как у статьи без балла до 08.11.
    assert [item["article_id"] for item in _issue_items(analyst, min_score=50)] == [a["hi"], scored]
    materials = _materials(analyst)
    assert (materials[scored]["score"], materials[scored]["rubric"]) == (66.0, "ЭНЕРГЕТИКА")
    assert materials[waiting]["score"] is None

    rows = _digest_screen(analyst, status="digest")
    assert (rows[scored]["tag"], rows[scored]["score"], rows[scored]["rating"]) == (ENERGY, 66.0, "Полезный")
    assert (rows[waiting]["tag"], rows[waiting]["score"], rows[waiting]["rating"]) == (
        repository.SYSTEM_TAG_UNCLASSIFIED, 0, "Без оценки")
    assert rows[waiting]["score_items"] == []
    # Лента с «Оценкой от» неоценённое не прячет: балла у него ещё нет (правило ленты для необработанного).
    feed = _digest_screen(analyst, min_score=50)
    assert {scored, waiting, a["unscored"]} <= set(feed) and a["low"] not in feed
