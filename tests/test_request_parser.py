from oiltech_digest.ingestion import request_parser


HOME_HTML = b"""
<html>
  <body>
    <a href="/about">About</a>
    <a href="/news/2026/05/field-automation-rollout">Field automation rollout improves wellsite performance</a>
    <a href="/media/article/drilling-analytics-platform">Drilling analytics platform expands to offshore assets</a>
    <a href="https://external.example.com/story">External story</a>
  </body>
</html>
"""

ARTICLE_HTML = b"""
<html>
  <head>
    <meta property="og:title" content="Field automation rollout improves wellsite performance">
    <meta property="article:published_time" content="2026-05-20T08:30:00Z">
  </head>
  <body>
    <article>
      <p>The company deployed a field automation stack across producing wells and surface facilities.</p>
      <p>The program reduced manual interventions, improved uptime and gave engineers better production control.</p>
      <p>Additional industrial context keeps this article above teaser length and suitable for downstream AI stages.</p>
    </article>
  </body>
</html>
"""


QUERY_HOME_HTML = b"""
<html>
  <body>
    <a href="/news.php?id=12345&utm_source=rss">Field automation rollout improves wellsite performance metrics</a>
    <a href="/?p=678">Drilling analytics platform expands to offshore assets across the region</a>
    <a href="/?utm_source=newsletter">Subscribe to our newsletter for the latest updates</a>
  </body>
</html>
"""


def test_extract_candidate_links_prefers_article_like_paths():
    items = request_parser.extract_candidate_links("https://example.com", HOME_HTML, limit=10)

    assert len(items) == 2
    assert items[0].url.startswith("https://example.com/")
    assert all("about" not in item.url for item in items)


def test_extract_candidate_links_preserves_query_id_and_strips_tracking():
    # Бэклог #3: ID статьи в query больше не теряется (иначе ссылка вела на раздел/сайт).
    items = request_parser.extract_candidate_links("https://example.com", QUERY_HOME_HTML, limit=10)
    urls = [item.url for item in items]

    assert "https://example.com/news.php?id=12345" in urls   # query сохранён, utm отрезан
    assert "https://example.com?p=678" in urls               # query-only статья не схлопнулась в главную
    assert all("newsletter" not in url for url in urls)      # трекинг-ссылка на корень отброшена
    assert "https://example.com" not in urls                 # чистая главная не попадает в кандидаты


def test_clean_query_keeps_meaningful_strips_tracking():
    assert request_parser._clean_query("id=42&utm_source=x&utm_medium=y") == "id=42"
    assert request_parser._clean_query("utm_source=x&fbclid=z") == ""
    assert request_parser._clean_query("") == ""
    assert request_parser._clean_query("p=7&ref=home") == "p=7&ref=home"


def test_parse_article_page_extracts_title_date_and_body():
    title, published_at, raw_text = request_parser.parse_article_page(ARTICLE_HTML)

    assert "Field automation rollout" in title
    assert published_at is not None
    assert "reduced manual interventions" in raw_text


def test_parse_source_uses_listing_page_and_updates_last_seen(monkeypatch):
    fetched_urls = []
    inserted = []
    state = {}

    def fake_fetch(url):
        fetched_urls.append(url)
        if url == "https://example.com/news":
            return HOME_HTML
        if url == "https://example.com/news/2026/05/field-automation-rollout":
            return ARTICLE_HTML
        return None

    def fake_insert(article):
        inserted.append(article)
        return True

    monkeypatch.setattr(request_parser, "fetch", fake_fetch)
    monkeypatch.setattr(request_parser.repository, "insert_article", fake_insert)
    monkeypatch.setattr(request_parser.repository, "article_exists", lambda url: False)
    monkeypatch.setattr(request_parser.repository, "touch_last_parsed", lambda source_id: state.setdefault("touched", source_id))
    monkeypatch.setattr(
        request_parser.repository,
        "update_source_request_state",
        lambda source_id, **kwargs: state.update({"source_id": source_id, **kwargs}),
    )
    monkeypatch.setattr(
        request_parser,
        "extract_candidate_links",
        lambda source, listing_url, content, limit=12: [
            request_parser.CandidateLink(
                url="https://example.com/news/2026/05/field-automation-rollout",
                title="Field automation rollout improves wellsite performance",
                score=8,
                published_at=None,
            )
        ],
    )

    stats = request_parser.parse_source(
        {"id": 7, "url": "https://example.com", "listing_url": "https://example.com/news", "category": "международные"},
        article_limit=5,
    )

    assert stats["added"] == 1
    assert fetched_urls == [
        "https://example.com/news",
        "https://example.com/news/2026/05/field-automation-rollout",
    ]
    assert inserted[0]["url"] == "https://example.com/news/2026/05/field-automation-rollout"
    assert state["source_id"] == 7
    assert state["last_seen_article_url"] == "https://example.com/news/2026/05/field-automation-rollout"


def _field_automation_candidate():
    return request_parser.CandidateLink(
        url="https://example.com/news/2026/05/field-automation-rollout",
        title="Field automation rollout improves wellsite performance",
        score=8,
        published_at=None,
    )


def test_parse_source_skips_known_candidate_via_article_exists(monkeypatch):
    """Дедуп держится на article_exists (articles.url уникален), а не на listing_hash.
    Кандидат, уже лежащий в БД, пропускается без фетча и вставки."""
    touched = {}

    monkeypatch.setattr(request_parser, "fetch", lambda url: HOME_HTML)
    monkeypatch.setattr(
        request_parser,
        "extract_candidate_links",
        lambda source, listing_url, content, limit=12: [_field_automation_candidate()],
    )
    monkeypatch.setattr(request_parser.repository, "touch_last_parsed", lambda source_id: touched.setdefault("id", source_id))
    monkeypatch.setattr(request_parser.repository, "update_source_request_state", lambda source_id, **kwargs: None)
    monkeypatch.setattr(request_parser.repository, "insert_article", lambda article: (_ for _ in ()).throw(AssertionError("insert_article should not be called for a known candidate")))
    # кандидат уже в БД → пропускаем без фетча/вставки
    monkeypatch.setattr(request_parser.repository, "article_exists", lambda url: True)

    stats = request_parser.parse_source({"id": 9, "url": "https://example.com/news"})

    assert stats["added"] == 0
    assert stats["skipped_known"] == 1
    assert touched["id"] == 9


def test_parse_source_not_frozen_when_listing_hash_unchanged(monkeypatch):
    """Регресс на дедуп-заморозку: даже если listing_hash совпадает с прошлым
    прогоном, новые статьи (которых нет в БД) обязаны добавляться. Раньше
    short-circuit по listing_hash прятал их → источник застывал навсегда."""
    candidate = _field_automation_candidate()
    inserted = []

    def fake_fetch(url):
        if url == "https://example.com/news":
            return HOME_HTML
        if url == candidate.url:
            return ARTICLE_HTML
        return None

    monkeypatch.setattr(request_parser, "fetch", fake_fetch)
    monkeypatch.setattr(
        request_parser,
        "extract_candidate_links",
        lambda source, listing_url, content, limit=12: [candidate],
    )
    monkeypatch.setattr(request_parser.repository, "article_exists", lambda url: False)
    monkeypatch.setattr(request_parser.repository, "insert_article", lambda article: inserted.append(article) or True)
    monkeypatch.setattr(request_parser.repository, "touch_last_parsed", lambda source_id: None)
    monkeypatch.setattr(request_parser.repository, "update_source_request_state", lambda source_id, **kwargs: None)

    # last_listing_hash намеренно совпадает с текущим листингом — старый код
    # коротил бы на 0; новый обязан добавить незнакомую статью.
    source = {
        "id": 9,
        "url": "https://example.com",
        "listing_url": "https://example.com/news",
        "category": "международные",
        "last_listing_hash": request_parser._listing_hash([candidate]),
    }

    stats = request_parser.parse_source(source)

    assert stats["added"] == 1
    assert inserted[0]["url"] == candidate.url


# Реальный листинг Сургутнефтегаза: ссылки на статьи идут СО СЛЭШЕМ на конце.
# Так устроены многие корпоративные РФ-сайты (Bitrix): без слэша сервер отдаёт 404.
SLASH_HOME_HTML = """
<html><body>
  <a href="/">На главную</a>
  <a href="/press-center/press_releases/">Все пресс-релизы</a>
  <a href="/press-center/press_releases/preduprezhdenie-o-moshennicheskikh-deystviyakh/">
     Предупреждение о мошеннических действиях в отношении партнёров компании</a>
</body></html>
"""


def test_extract_candidate_links_keeps_trailing_slash():
    """Финальный слэш в адресе статьи сохраняется — иначе сайт отдаёт 404.

    Баг (найден на проде 20.07): путь резался через parts.path.rstrip('/'), и парсер шёл
    за статьёй по адресу БЕЗ слэша. Сургутнефтегаз на такой адрес отвечает 404 —
    листинг читался, кандидаты извлекались, а статей добавлялось 0 (источник выглядел
    «замолчавшим»). Срез слэша нужен был только чтобы отсеять главную, поэтому он
    остаётся в ПРОВЕРКЕ, но не в самом URL.
    """
    items = request_parser.extract_candidate_links("https://www.surgutneftegas.ru", SLASH_HOME_HTML, limit=10)
    urls = [item.url for item in items]

    assert "https://www.surgutneftegas.ru/press-center/press_releases/preduprezhdenie-o-moshennicheskikh-deystviyakh/" in urls, (
        "слэш срезан → сайт вернёт 404 и статья не добавится"
    )
    # Главная по-прежнему не считается статьёй (ради этого и был rstrip).
    assert "https://www.surgutneftegas.ru/" not in urls
    assert "https://www.surgutneftegas.ru" not in urls
