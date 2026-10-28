"""Одно правило «одна ли это компания» (word_forms.Companies, разбор 20.10) — без базы.

Одна таблица случаев прогоняется через три места: ядро (сколько участников останется в карточке, если модель
назвала обе компании с верными цитатами), фильтр «всё про компанию» (найдёт ли клик по одному имени карточку с
другим) и экзамен (одна ли это компания у счёта). Новая развилка в одном месте роняет таблицу в другом: это сторож
одного правила. Ниже то, что правило меняет в ядре: цитата по любому написанию имени и с другим апострофом, чем в
тексте, имя справочника, издатель; и ключ экзамена: одна компания между прогонами.
"""

from datetime import date

import pytest

from oiltech_digest import signal_card, signal_card_filters, word_forms
from oiltech_digest.signal_card_exam_score import CompanyNames
from oiltech_digest.signal_card_pack import load_dictionaries
from tests.signal_card_support import answer

# Пара имён и ответ правила: одна ли это компания.
CASES = [
    # Форма собственности — не часть имени, и с точками.
    ("Atlas Energy Solutions Inc.", "Atlas Energy Solutions", True),
    ("Equinor ASA", "Equinor", True),
    ("ПАО «Газпром»", "Газпром", True),
    ("ПАО Газпром", "Газпром", True),
    ("ПАО «Татнефть»", "Татнефть", True),
    ("Petrobras S.A.", "Petrobras", True),
    # «Имя (АББР)» и «АББР (Имя)» — два написания: имя и аббревиатура.
    ("Petroleum Development Oman (PDO)", "PDO", True),
    ("Petroleum Development Oman (PDO)", "Petroleum Development Oman", True),
    ("DOE (Department of Energy)", "DOE", True),
    # Притяжательное «’s» в имени аббревиатуре не мешает: апостроф делит слово, буквы NOC идут по первым буквам.
    ("Libya’s National Oil Corporation (NOC)", "NOC", True),
    # Аббревиатуры между собой не сравниваются (решение #121): одна аббревиатура бывает у разных компаний.
    ("Petroleum Development Oman (PDO)", "Pipeline Development Organization (PDO)", False),
    # Русское слово — в любом падеже.
    ("Роснефтью", "Роснефть", True),
    # Вид апострофа — не различие: буква-апостроф (ʻ U+02BB, ʿ U+02BF) делит слово, как ‘ и ’ (замер 21.10).
    ("O‘zbekneftgaz", "Oʻzbekneftgaz", True),
    ("Maʿaden", "Ma’aden", True),
    # Компания — целиком: «Газпром» не «Газпром нефть», дочка не мать.
    ("Газпром", "Газпром нефть", False),
    # Любая другая скобка — пояснение, в имя не входит.
    ("Газпром (ПАО)", "Татнефть (ПАО)", False),
    ("Halliburton (США)", "Chevron (США)", False),
    ("Нефтемаш (ПАО)", "Газпром (ПАО)", False),
    ("Oceaneering (США)", "Halliburton (США)", False),
    ("Газпромнефть-Хантос (Газпром нефть)", "Газпром нефть", False),
    ("Journal of Petroleum Technology (SPE)", "SPE", False),
    ("Copernicus/ACP (публикатор)", "alb.aero (публикатор)", False),
]
PUBLISHED = date(2026, 9, 25)


def test_spellings_of_a_name_are_the_name_and_its_abbreviation_never_an_explanation():
    assert word_forms.spellings(" Petroleum  Development Oman (PDO) ") == ["Petroleum Development Oman", "PDO"]
    assert word_forms.spellings("DOE (Department of Energy)") == ["Department of Energy", "DOE"]
    # Слова имени аббревиатура может пропускать: ONGC — Oil and Natural Gas Corporation, DOE — U.S. Department of
    # Energy.
    assert word_forms.spellings("Oil and Natural Gas Corporation (ONGC)") == ["Oil and Natural Gas Corporation", "ONGC"]
    assert word_forms.spellings("U.S. Department of Energy (DOE)") == ["U.S. Department of Energy", "DOE"]
    for name, outside in (("Газпромнефть-Хантос (Газпром нефть)", "Газпромнефть-Хантос"),
                          ("FleetRabbit (поставщик)", "FleetRabbit"), ("Газпром (ПАО)", "Газпром"),
                          ("Halliburton (США)", "Halliburton"), ("Journal of Petroleum Technology (SPE)",
                                                                  "Journal of Petroleum Technology"),
                          ("РГУ нефти и газа (НИУ) имени И. М. Губкина", "РГУ нефти и газа имени И. М. Губкина")):
        assert word_forms.spellings(name) == [outside], name
    assert word_forms.spellings("«—»") == []


def _kept(text, companies, *, publisher="Пресс-служба", publisher_type="Company"):
    """Участники, которых ядро запишет в карточку по ответу модели."""
    publication = {"text": text, "publisher": publisher, "publisher_type": publisher_type, "published": PUBLISHED}
    values = signal_card.check_answer(answer(companies=companies), publication, load_dictionaries(), [])
    return values["participants_json"]


@pytest.mark.parametrize(("one", "other", "same"), CASES)
def test_core_keeps_one_participant_for_one_company(one, other, same):
    text = f"{one} подписала договор. Позже {other} сообщила об испытании."
    first = {"name": one, "quote": f"{one} подписала договор"}
    second = {"name": other, "quote": f"{other} сообщила об испытании"}
    # Каждая цитата верна и сама по себе: участника отбрасывает повтор, а не цитата.
    assert [len(_kept(text, [first])), len(_kept(text, [second]))] == [1, 1]

    assert len(_kept(text, [first, second])) == (1 if same else 2)


def _found(company, participant):
    """Найдёт ли фильтр «всё про компанию» карточку с участником: путь api._company_filter (company_group, затем
    group_names по именам участников в базе) при пустом списке дочек."""
    group = signal_card_filters.company_group(company, [])
    return participant in signal_card_filters.group_names(group, [participant])


@pytest.mark.parametrize(("one", "other", "same"), CASES)
def test_filter_finds_the_card_under_another_name_of_the_company(one, other, same):
    assert (_found(one, other), _found(other, one)) == (same, same)


@pytest.mark.parametrize(("one", "other", "same"), CASES)
def test_exam_counts_one_company_by_the_same_rule(one, other, same):
    names = CompanyNames(load_dictionaries()["companies"])
    assert (names.same(one, other), names.same(other, one)) == (same, same)


def test_exam_key_is_one_for_spellings_of_one_company_between_runs():
    # Ключ считает устойчивость экзамена между прогонами и его константу: «X Inc.» в одном прогоне и «X» в
    # другом дают один ключ; имя справочника или алиасов экзамена даёт ключ своей записи. Аббревиатура в скобках в
    # ключ не входит (как у #121): «Petroleum Development Oman (PDO)» — ключ полного имени, одна «PDO» — свой.
    names = CompanyNames(load_dictionaries()["companies"], [{"name": "Нефтемаш", "aliases": ["Neftemash"]}])

    assert names.key("Atlas Energy Solutions Inc.") == names.key("Atlas Energy Solutions") == (
        "atlas", "energy", "solutions")
    assert names.key("Petroleum Development Oman (PDO)") == names.key("Petroleum Development Oman") == (
        "petroleum", "development", "oman")
    assert names.key("PDO") == ("pdo",)
    assert names.key("Роснефтью") == names.key("НК «Роснефть»") == names.key("Rosneft") == ("роснефть",)
    assert names.key("Neftemash Ltd") == names.key("Нефтемаш (ПАО)") == ("нефтемаш",)
    # Общее пояснение в скобках ключ не сводит: другая компания, другой ключ.
    assert names.key("Oceaneering (США)") != names.key("Halliburton (США)")


@pytest.mark.parametrize(("one", "other", "same"), CASES)
def test_one_exam_key_is_always_one_company(one, other, same):
    # Общий ключ никогда не склеивает то, что правило разводит: иначе устойчивость спрятала бы смену компании.
    names = CompanyNames(load_dictionaries()["companies"])
    assert names.key(one) != names.key(other) or names.same(one, other)


TENDER = ("Petroleum Development Oman (PDO) объявила тендер. PDO signed a contract with FleetRabbit. FleetRabbit "
          "offers fleet software. Equinor испытала подводного робота.")


def test_quote_names_the_company_under_any_of_its_spellings():
    # Цитата называет компанию аббревиатурой или именем без пояснения в скобках; имя участника — как дала модель.
    participants = _kept(TENDER, [
        {"name": "Petroleum Development Oman (PDO)", "quote": "PDO signed a contract"},
        {"name": "FleetRabbit (поставщик)", "quote": "FleetRabbit offers fleet software"},
    ])

    assert participants == [
        {"name": "Petroleum Development Oman (PDO)", "quote": "PDO signed a contract", "verified": True,
         "country": None},
        {"name": "FleetRabbit (поставщик)", "quote": "FleetRabbit offers fleet software", "verified": True,
         "country": None},
    ]


def test_apostrophe_divides_a_word_so_a_possessive_still_names_the_company():
    # Апостроф любого вида делит слово, а не склеивает его: «Shell’s» и «Shellʼs» называют Shell.
    for quote in ("Shell’s unit signed a contract", "Shellʼs unit signed a contract"):
        assert [item["name"] for item in _kept(f"{quote}.", [{"name": "Shell", "quote": quote}])] == ["Shell"], quote


def test_quote_names_the_company_whatever_apostrophe_the_model_typed():
    # Апостроф модель переписывает на свой лад, как кавычки и тире: цитата с ‘ находится в тексте с ʻ.
    text = "Oʻzbekneftgaz signed a contract with Halliburton."
    participants = _kept(text, [{"name": "O‘zbekneftgaz", "quote": "O‘zbekneftgaz signed a contract"}])

    assert [item["name"] for item in participants] == ["O‘zbekneftgaz"]


def test_company_from_the_dictionary_is_written_under_its_dictionary_name():
    participants = _kept(TENDER, [{"name": "Equinor ASA", "quote": "Equinor испытала подводного робота"}])

    assert [item["name"] for item in participants] == ["Equinor"]


def test_media_publisher_is_dropped_under_another_spelling_of_its_name():
    text = "ООО «Нефтегаз» сообщает: Halliburton подписала контракт на бурение."
    companies = [{"name": "ООО «Нефтегаз»", "quote": "ООО «Нефтегаз» сообщает"},
                 {"name": "Halliburton", "quote": "Halliburton подписала контракт"}]

    assert [item["name"] for item in _kept(text, companies, publisher="Нефтегаз", publisher_type="Media")] == [
        "Halliburton"]
    # Компания, которая публикует свою новость, — участник.
    assert [item["name"] for item in _kept(text, companies, publisher="Нефтегаз", publisher_type="Company")] == [
        "ООО «Нефтегаз»", "Halliburton"]
