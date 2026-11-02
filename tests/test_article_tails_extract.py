"""Общее правило извлечения (#140): текст статьи кончается там, где кончается материал издателя.

Страницы синтетические: настоящий HTML сайтов в репо не кладём."""

from oiltech_digest.ingestion import article_fetcher

OWN = (
    "Компания завершила испытания новой установки на месторождении и подтвердила расчётный дебит скважин.",
    "Инженеры сравнили показатели с прежним оборудованием и отметили снижение простоев на четверть за квартал.",
    "Следующий этап работ намечен на весну, когда подрядчик поставит ещё два комплекта для соседнего куста.",
)
NEIGHBOURS = (
    "Соседняя новость: другой завод запустил линию по выпуску присадок для дизельного топлива",
    "Соседняя новость: регулятор продлил лицензию на участок в Восточной Сибири ещё на десять лет",
    "Соседняя новость: сервисная компания купила парк буровых установок у конкурента на Ямале",
)
COMMENT = "Читатель считает, что такие проекты окупаются только при высокой цене нефти и никак иначе."


def _paragraphs(*items: str) -> str:
    return "".join(f"<p>{item}</p>" for item in items)


def _links(*items: str) -> str:
    return "<ul>" + "".join(f'<li><a href="/n/{i}">{item}</a></li>' for i, item in enumerate(items)) + "</ul>"


def _page(block: str, block_class: str = "article-page") -> str:
    return f'<html><body><div class="{block_class}"><h1>Заголовок новости</h1>{block}</div></body></html>'


def _assert_only_own(text: str) -> None:
    for item in OWN:
        assert item in text
    for item in NEIGHBOURS:
        assert item not in text
    assert COMMENT not in text


def test_nested_block_of_neighbours_is_dropped_though_its_paragraphs_have_no_class():
    page = _page(_paragraphs(*OWN) + f'<div class="related-news">{_paragraphs(*NEIGHBOURS)}</div>')

    _assert_only_own(article_fetcher.extract_main_text(page))


def test_comments_marked_by_id_are_dropped_from_a_flat_block():
    # Тело на <br> без абзацев: текст берётся из всего узла, и метка абзаца тут не поможет.
    flat = "<br>".join(OWN * 2)
    page = _page(f'{flat}<div id="comments"><div>{COMMENT}</div></div>')

    _assert_only_own(article_fetcher.extract_main_text(page))


def test_comments_longer_than_the_article_are_dropped():
    # У АНГИ блок последних комментариев 2,7 тыс. знаков при новости в тысячу.
    comments = "".join(f'<div class="comment"><p>{COMMENT} Отзыв {i}.</p></div>' for i in range(12))
    page = _page(_paragraphs(*OWN) + f'<div class="comments">{comments}</div>')

    _assert_only_own(article_fetcher.extract_main_text(page))


def test_mark_on_a_wrapper_of_the_whole_article_does_not_lose_the_text():
    # Обёртка всей статьи с меткой в классе: чистим её изнутри, а не выбрасываем.
    page = _page(
        '<div class="body-sidebar-wrap">' + _paragraphs(*OWN) + f'<div class="sidebar">{_links(*NEIGHBOURS)}</div></div>'
    )

    _assert_only_own(article_fetcher.extract_main_text(page))


def test_layout_modifier_in_a_class_is_not_a_mark():
    page = _page('<div class="with-comments has-related">' + _paragraphs(*OWN) + "</div>"
                 '<p>Подрядчик уточнит сроки после зимнего сезона, когда завершится отсыпка кустовых площадок.</p>')

    text = article_fetcher.extract_main_text(page)

    assert all(item in text for item in OWN)


def test_inline_expert_comment_stays_in_the_text():
    expert = "Эксперт отметил, что установка окупится за три года при нынешних тарифах на электроэнергию."
    page = _page(_paragraphs(OWN[0]) + f'<div class="comment expert-comment"><p>{expert}</p></div>'
                 + _paragraphs(*OWN[1:]))

    assert expert in article_fetcher.extract_main_text(page)


def test_word_inside_a_longer_class_word_is_not_a_mark():
    # Подстрока ads есть в uploads, share в shareholders: блок с текстом остаётся.
    page = _page(_paragraphs(OWN[0]) + f'<div class="uploads-shareholders">{_paragraphs(*OWN[1:])}</div>')

    assert all(item in article_fetcher.extract_main_text(page) for item in OWN)


def test_text_is_cut_at_the_tail_heading_after_the_material():
    for heading in ("Читайте также", "Related articles", "Другие новости:", "More news"):
        page = _page(_paragraphs(*OWN) + f"<h3>{heading}</h3>" + _links(*NEIGHBOURS))

        text = article_fetcher.extract_main_text(page)

        _assert_only_own(text)
        assert heading.rstrip(":") not in text


def test_bare_heading_text_before_a_list_cuts_too():
    # Заголовок просто текстом в общем блоке, а пункты с датой вне ссылки.
    items = "".join(f'<div>25.09.26 <a href="/n">{item}</a></div>' for item in NEIGHBOURS)
    page = _page(_paragraphs(*OWN) + f"<div>Читайте также:<br>{items}</div>")

    _assert_only_own(article_fetcher.extract_main_text(page))


def test_heading_inside_a_link_cuts_too():
    page = _page(_paragraphs(*OWN) + f'<h2><a href="/news">Related news ›</a></h2><div>{_links(*NEIGHBOURS)}</div>')

    _assert_only_own(article_fetcher.extract_main_text(page))


def test_short_article_with_a_longer_list_after_the_heading_is_cut():
    # Новость короче списка соседних: положение заголовка в тексте тут ничего не скажет.
    items = NEIGHBOURS * 4
    page = _page(_paragraphs(OWN[0], OWN[1]) + "<h2>Читайте также</h2>" + _links(*items))

    text = article_fetcher.extract_main_text(page)

    assert OWN[0] in text and OWN[1] in text
    assert not any(item in text for item in NEIGHBOURS)


def test_same_words_in_the_middle_of_the_material_do_not_cut():
    sentence = "Читайте также материал о прошлом этапе испытаний на этом месторождении, там есть все цифры."
    inline = '<p>Читайте также: <a href="/n">Другая новость о прошлом этапе испытаний</a></p>'
    page = _page(
        _paragraphs(OWN[0], sentence) + inline + "<h3>Комментарии</h3>" + _paragraphs(OWN[1], OWN[2], OWN[1] + " Итог.")
    )

    text = article_fetcher.extract_main_text(page)

    assert sentence in text
    assert all(item in text for item in OWN)


def test_heading_at_the_start_of_the_block_does_not_cut():
    page = _page('<div>Related articles</div>' + _paragraphs(*OWN))

    assert all(item in article_fetcher.extract_main_text(page) for item in OWN)


def test_flat_text_is_cut_at_the_heading_and_only_at_the_one_after_the_material():
    # Весь текст одним узлом, разметки у заголовка нет.
    body = " ".join(OWN * 2)
    tail = f"Читайте также: 25.09.26 {NEIGHBOURS[0]} 17.09.26 {NEIGHBOURS[1]}"
    page = f'<html><body><div class="article">{body} {tail}</div></body></html>'

    text = article_fetcher.extract_main_text(page)

    assert text.endswith(OWN[2])

    middle = f"{OWN[0]} Читайте также: прошлый этап испытаний прошёл без замечаний. {body}"
    page = f'<html><body><div class="article">{middle}</div></body></html>'

    assert article_fetcher.extract_main_text(page).startswith(middle[:200])
    assert article_fetcher.extract_main_text(page).endswith(OWN[2])


def test_json_ld_body_is_cut_at_the_heading():
    body = " ".join(OWN * 2)
    tail = f"Related articles {NEIGHBOURS[0]} {NEIGHBOURS[1]}"
    page = ('<html><head><script type="application/ld+json">{"headline": "Новость", "articleBody": "'
            + f"{body} {tail}" + '"}</script></head><body><div>Подвал</div></body></html>')

    assert article_fetcher.extract_main_text(page).endswith(OWN[2])


def test_list_with_a_heading_before_a_short_article_does_not_swallow_it():
    # Колонка соседних новостей раньше статьи в разметке, перед ней только крошки и заголовок.
    page = ('<html><body><main><div><a href="/">Главная</a> <a href="/news">Новости</a></div>'
            "<h1>Заголовок новости про испытания установки на месторождении в этом году</h1>"
            "<h3>Related articles</h3>" + _links(*NEIGHBOURS * 2) +
            '<div class="article-body">' + _paragraphs(OWN[0], OWN[1]) + "</div></main></body></html>")

    text = article_fetcher.extract_main_text(page)

    assert OWN[0] in text and OWN[1] in text


def test_inline_link_in_the_last_third_of_the_material_does_not_cut():
    # Вставка посреди статьи: за ней продолжается связный текст, и его меньше, чем до неё.
    before = _paragraphs(*OWN, OWN[0] + " Первый итог подвели в понедельник.")
    after = _paragraphs(OWN[1] + " Второй итог подведут весной.")
    for inline in ('<p>Читайте также: <a href="/n">Другая новость</a></p>',
                   '<p>RELATED: <a href="/n">Другая новость</a></p>',
                   '<p>See also <a href="/n">Другая новость</a></p>'):
        text = article_fetcher.extract_main_text(_page(before + inline + after))

        assert "Второй итог подведут весной." in text


def test_comments_under_a_heading_are_cut_though_they_read_as_text():
    # Блок без метки в классе: у комментариев связный текст, поэтому смотрим на его долю.
    page = _page(_paragraphs(*OWN, OWN[0] + " Итог.", OWN[1] + " Вывод.") + "<h3>Комментарии</h3>"
                 + f"<div><p>{COMMENT} И ещё отзыв.</p></div>")

    text = article_fetcher.extract_main_text(page)

    assert COMMENT not in text and OWN[2] in text


def test_block_of_neighbours_longer_than_the_article_is_dropped():
    # Подводки соседних новостей вне ссылок: текста в блоке больше, чем в самой статье.
    items = "".join(f'<div><a href="/n">Новость {i}</a><p>{NEIGHBOURS[i % 3]}. Подробности у редакции.</p></div>'
                    for i in range(9))
    page = _page(_paragraphs(*OWN) + f'<div class="related-news">{items}</div>')

    _assert_only_own(article_fetcher.extract_main_text(page))


def test_single_reader_comment_block_is_dropped():
    page = _page(_paragraphs(*OWN) + f'<div class="comment"><p>{COMMENT}</p></div>')

    _assert_only_own(article_fetcher.extract_main_text(page))


def test_flat_text_is_cut_at_a_short_heading_with_a_colon():
    body = " ".join(OWN * 2)
    page = f'<html><body><div class="article">{body} Related: {NEIGHBOURS[0]} {NEIGHBOURS[1]}</div></body></html>'

    assert article_fetcher.extract_main_text(page).endswith(OWN[2])
