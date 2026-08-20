"""Тесты сверки чисел с текстом якоря — чистые функции, без сети и БД."""

import pytest

from oiltech_digest.documents.verification import normalize_number, verify_value


# --- normalize_number: разделители разрядов -------------------------------

def test_normalize_grouping_separators_are_one_number():
    # обычный пробел, неразрывный, узкий неразрывный, точка, запятая, апостроф
    forms = [
        "1 234 567",
        "1 234 567",
        "1 234 567",
        "1.234.567",
        "1,234,567",
        "1’234’567",
        "1234567",
    ]
    assert {normalize_number(f) for f in forms} == {"1234567"}


def test_normalize_decimal_separators_are_one_number():
    assert normalize_number("1,5") == "1.5"
    assert normalize_number("1.5") == "1.5"
    assert normalize_number("1,50") == "1.5"
    assert normalize_number("1,5") == normalize_number("1.5")


def test_normalize_mixed_grouping_and_decimal():
    # десятичным считается тот разделитель, что правее
    assert normalize_number("1 234 567,89") == "1234567.89"
    assert normalize_number("1.234.567,89") == "1234567.89"
    assert normalize_number("1,234,567.89") == "1234567.89"


def test_normalize_single_separator_with_three_digits_is_grouping():
    # «1,234» — разряды: ровно три цифры после разделителя
    assert normalize_number("1,234") == "1234"
    assert normalize_number("1.234") == "1234"


def test_normalize_strips_leading_zeros_and_trailing_fraction_zeros():
    assert normalize_number("015") == "15"
    assert normalize_number("15,00") == "15"
    assert normalize_number("0,50") == "0.5"


def test_normalize_returns_none_for_non_numbers():
    assert normalize_number("") is None
    assert normalize_number("   ") is None
    assert normalize_number(None) is None
    assert normalize_number("около") is None
    assert normalize_number("1,5 млн") is None  # это не число, а число с хвостом
    assert normalize_number(".") is None
    assert normalize_number("12/34") is None


# --- verify_value: базовая сверка ------------------------------------------

def test_verify_plain_number_found_and_missing():
    assert verify_value("15", None, "Пробурено 15 скважин") is True
    assert verify_value("16", None, "Пробурено 15 скважин") is False


def test_verify_number_written_with_other_grouping_in_text():
    # модель отдала «1234567», в документе — «1 234 567» с неразрывным пробелом
    assert verify_value("1234567", None, "Объём составил 1 234 567 за период") is True
    assert verify_value("1 234 567", None, "Объём составил 1.234.567 за период") is True


def test_verify_decimal_comma_against_dot_in_text():
    assert verify_value("1,5", None, "Рост в 1.5 раза") is True


# --- главный контрпример: масштаб --------------------------------------------

def test_verify_scale_mln_vs_mlrd_is_false():
    anchor = "Добыча за год составила 1,5 млн тонн нефти."
    # то же самое число присутствует как подстрока, но масштаб другой
    assert verify_value("1,5", "млрд тонн", anchor) is False
    assert verify_value("1,5", "млн тонн", anchor) is True


def test_verify_scale_thousand_vs_million_is_false():
    anchor = "Объём переработки — 250 тыс. тонн."
    assert verify_value("250", "млн тонн", anchor) is False
    assert verify_value("250", "тыс. тонн", anchor) is True


def test_verify_scale_folded_into_number_matches():
    # «1 500 000 тонн» в тексте и «1,5» + «млн тонн» от модели — одна величина
    assert verify_value("1,5", "млн тонн", "Отгружено 1 500 000 тонн") is True
    assert verify_value("1500000", "тонн", "Отгружено 1,5 млн тонн") is True


def test_verify_bare_number_in_text_does_not_confirm_scaled_claim():
    assert verify_value("1,5", "млн тонн", "Коэффициент 1,5 по методике") is False


# --- главный контрпример: границы числа -------------------------------------

def test_verify_15_is_not_confirmed_by_2015():
    assert verify_value("15", None, "В 2015 году добыча выросла") is False
    assert verify_value("15", "%", "В 2015 году добыча выросла") is False


def test_verify_number_is_not_confirmed_by_longer_neighbour():
    assert verify_value("15", None, "Рост составил 15,3%") is False
    assert verify_value("234", None, "Объём 1 234 567 тонн") is False
    assert verify_value("1", None, "Объём 1 234 567 тонн") is False


# --- единицы -----------------------------------------------------------------

def test_verify_unit_mismatch_is_false():
    anchor = "Добыча составила 1,5 млн тонн."
    assert verify_value("1,5", "млн барр", anchor) is False
    assert verify_value("1,5", "млн м3", anchor) is False


def test_verify_common_units():
    assert verify_value("15", "%", "Рост составил 15% год к году") is True
    assert verify_value("15", "%", "Рост составил 15 процентов") is True
    assert verify_value("120", "тонн", "Отгружено 120 тонн продукции") is True
    assert verify_value("500", "барр", "Дебит 500 баррелей") is True
    assert verify_value("42", "м3", "Закачано 42 м3 раствора") is True
    assert verify_value("42", "м3", "Закачано 42 м³ раствора") is True
    assert verify_value("300", "руб", "Цена 300 рублей за единицу") is True
    assert verify_value("70", "$", "Цена 70 долл. за баррель") is True
    assert verify_value("70", "долл", "Цена 70 $ за баррель") is True
    assert verify_value("120", "км", "Длина 120 километров") is True
    assert verify_value("30", "сут", "Срок 30 суток") is True


def test_verify_unit_case_and_form_are_folded():
    assert verify_value("1,5", "МЛН ТОНН", "Добыча 1,5 млн тонн") is True
    assert verify_value("1,5", "млн Тонны", "Добыча 1,5 МЛН ТОНН") is True


def test_verify_compound_unit_with_slash():
    anchor = "Дебит скважины 1,2 тыс. барр/сут в среднем."
    assert verify_value("1,2", "тыс. барр/сут", anchor) is True
    assert verify_value("1,2", "тыс. барр/год", anchor) is False


def test_verify_currency_written_before_number():
    assert verify_value("1,5", "млрд $", "Инвестиции составили $1,5 млрд") is True
    assert verify_value("1,5", "млрд $", "Инвестиции составили $1,5 млн") is False


def test_verify_unknown_unit_is_compared_literally():
    anchor = "Пробурено 15 скважин за квартал"
    assert verify_value("15", "скважин", anchor) is True
    assert verify_value("15", "тонн", anchor) is False


def test_verify_unit_none_ignores_scale_and_unit():
    # модель не заявила единицу — спрашиваем только про цифры
    assert verify_value("1,5", None, "Добыча 1,5 млн тонн") is True
    assert verify_value("1,5", "", "Добыча 1,5 млн тонн") is True


def test_verify_value_may_carry_scale_itself():
    # value="1,5 млн" + unit="тонн" — масштаб не должен посчитаться дважды
    assert verify_value("1,5 млн", "тонн", "Добыча 1,5 млн тонн") is True
    assert verify_value("1,5 млн", "тонн", "Добыча 1,5 млрд тонн") is False
    assert verify_value("1,5 млн", "млн тонн", "Добыча 1,5 млн тонн") is True


def test_verify_picks_matching_occurrence_among_several():
    anchor = "В 2023 году — 1,5 млн барр, в 2024 году — 1,5 млн тонн."
    assert verify_value("1,5", "млн тонн", anchor) is True
    assert verify_value("1,5", "млн барр", anchor) is True
    assert verify_value("1,5", "млн м3", anchor) is False


# --- границы: пустой вход не бросает исключений -----------------------------

@pytest.mark.parametrize(
    "value, unit, anchor",
    [
        ("", "млн тонн", "Добыча 1,5 млн тонн"),
        ("   ", "млн тонн", "Добыча 1,5 млн тонн"),
        (None, "млн тонн", "Добыча 1,5 млн тонн"),
        ("1,5", "млн тонн", ""),
        ("1,5", "млн тонн", "   "),
        ("1,5", "млн тонн", None),
        ("н/д", "млн тонн", "Добыча 1,5 млн тонн"),
        ("", None, ""),
    ],
)
def test_verify_empty_or_unparsable_input_returns_false(value, unit, anchor):
    assert verify_value(value, unit, anchor) is False


def test_verify_anchor_without_numbers_returns_false():
    assert verify_value("15", None, "Числа в этом абзаце отсутствуют") is False
