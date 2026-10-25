"""Разделы отклонённого (#130, спека #128): «кто убрал → причина» — на месте «Отсеянных и скрытых», только админу.

На экране они одним списком «Отклонённые» (ALL, решение владельца 23.10): одна плашка в обеих лентах, причина —
«кто убрал → причина: коротко» в самой карточке. У каждой публикации раздела короткая причина. «Отбор» — по разделу на каждую причину отказа из справочника;
сбой — «„Отбор“ не ответил» (технический, шаг повторит) и «Ответ не по правилам» (без повтора, решение владельца
23.10); поиск — технический отсев (#131: «Ссылка не подтверждена», «Текст страницы не получен», «Страница-список»,
«Нет ссылки или заголовка»); «Другое» — принятое «Отбором», которое видит только админ (витрины его не показывают);
«До „Отбора“» — скрытое прежними правилами радара: решённое до «Отбора» живёт по ним. Дубли в разделы не идут — их
ссылки в главной карточке; выбранное «в дайджест» видно в витрине и в разделы тоже не идёт. Находка и материал, которые
ждут «Отбора» (#131), в разделах только со сбоем.
"""

from __future__ import annotations

from dataclasses import dataclass

from oiltech_digest.db import connection
from oiltech_digest.db.otbor_store import FAILURE_INVALID, FAILURE_TECHNICAL
from oiltech_digest.feed_window import OTBOR_ACCEPT, OTBOR_REJECT
from oiltech_digest.db.repository import (_BUSINESS_VISIBLE_SQL, _RADAR_HIDDEN_SQL, _RADAR_VISIBLE_SQL,
                                          _radar_hidden_reason_sql, signal_digest_sql)
from oiltech_digest.otbor_pack_format import load_dictionaries
from oiltech_digest.signal_research import LIST_PAGE, NO_LINK_OR_TITLE, PAGE_NOT_FETCHED, UNCONFIRMED_LINK

# Технический отсев поиска (#130, #131) — свой раздел у каждой причины: «Поиск → …»; список причин — только здесь.
# Отсев поиска по смыслу (прежняя цепочка, до «Отбора») — в разделе «До „Отбора“».
SEARCH_SECTIONS = (("search_unconfirmed", "Ссылка не подтверждена", UNCONFIRMED_LINK),
                   ("search_no_text", "Текст страницы не получен", PAGE_NOT_FETCHED),
                   ("search_list_page", "Страница-список", LIST_PAGE),
                   ("search_no_link", "Нет ссылки или заголовка", NO_LINK_OR_TITLE))


def _search_drop_sql(*reasons: str) -> str:
    """Отсеяно поиском по одной из причин. IS NOT DISTINCT FROM: у карточки без этапа отсева условие — FALSE, а не
    NULL (оно идёт и под NOT)."""
    listed = ", ".join("'" + reason.replace("'", "''") + "'" for reason in reasons)
    return f"(s.filter_stage IS NOT DISTINCT FROM 'search' AND COALESCE(s.filter_reason IN ({listed}), FALSE))"


_TECHNICAL_SQL = _search_drop_sql(*(reason for _key, _label, reason in SEARCH_SECTIONS))
# Не видна ни в одной витрине и не дубль: только такую раздел и держит (находку радара в бизнес-ленте — тоже нет).
_NOT_SHOWN_SQL = (f"s.merged_into_signal_id IS NULL AND NOT ({_RADAR_VISIBLE_SQL})"
                  f" AND NOT ({_BUSINESS_VISIBLE_SQL})")


@dataclass(frozen=True)
class Section:
    """Раздел: кто убрал, причина, какие сигналы (SQL над s) и короткая причина у каждого (SQL)."""

    key: str
    who: str
    reason: str
    condition: str
    short_reason: str


# Все разделы одним списком (плашка «Отклонённые»).
ALL = "all"


def sections() -> list[Section]:
    """Все разделы по порядку экрана, группами «кто убрал»: «Отбор» (причины справочника, без причины, «Другое»),
    сбой, поиск, «До „Отбора“»."""
    rejected = f"{_NOT_SHOWN_SQL} AND s.otbor_decision = '{OTBOR_REJECT}'"
    undecided = f"{_NOT_SHOWN_SQL} AND s.otbor_decision IS NULL"
    reasons = load_dictionaries()["reject_reason"]["values"]
    return [
        *[Section(f"otbor:{value['code']}", "«Отбор»", value["label"],
                  f"{rejected} AND s.reject_reason = '{value['code']}'", "s.verdict_reason") for value in reasons],
        # Отказ без причины бывает, только если шаги ответа не сошлись и после повтора («спорное»).
        Section("otbor_no_reason", "«Отбор»", "Без причины (спорное)", f"{rejected} AND s.reject_reason IS NULL",
                "COALESCE(s.otbor_disputed, s.verdict_reason)"),
        Section("other", "«Отбор»", "Другое: видно только админу",
                f"{_NOT_SHOWN_SQL} AND s.otbor_decision = '{OTBOR_ACCEPT}' AND s.signal_category = 'other'"
                f" AND (s.archived_at IS NULL OR {signal_digest_sql('s')})", "s.verdict_reason"),
        Section("otbor_failed", "Сбой", "«Отбор» не ответил",
                f"{undecided} AND s.otbor_failure = '{FAILURE_TECHNICAL}'", "s.otbor_failure_reason"),
        Section("otbor_invalid", "Сбой", "Ответ не по правилам",
                f"{undecided} AND s.otbor_failure = '{FAILURE_INVALID}'", "s.otbor_failure_reason"),
        *[Section(key, "Поиск", label, f"{_NOT_SHOWN_SQL} AND {_RADAR_HIDDEN_SQL} AND {_search_drop_sql(reason)}",
                  "s.filter_reason") for key, label, reason in SEARCH_SECTIONS],
        Section("before_otbor", "До «Отбора»", "Скрыто прежними правилами радара",
                f"{_NOT_SHOWN_SQL} AND {_RADAR_HIDDEN_SQL} AND NOT {_TECHNICAL_SQL}", _radar_hidden_reason_sql()),
    ]


def everything() -> Section:
    """Все разделы одним списком: причина у публикации — «кто убрал → причина: коротко» первого её раздела."""
    found = sections()

    def literal(text: str) -> str:
        return "'" + text.replace("'", "''") + "'"

    def short(section: Section) -> str:
        # Причина раздела в короткой причине не повторяется: «ответ не по правилам: нет строки…» → «нет строки…»,
        # а короткая причина, равная причине раздела («нет ссылки или заголовка»), не пишется вовсе.
        value, prefix = f"NULLIF({section.short_reason}, '')", f"{section.reason.lower()}: "
        return (f"CASE WHEN lower({value}) = {literal(section.reason.lower())} THEN NULL"
                f" WHEN starts_with(lower({value}), {literal(prefix)})"
                f" THEN substr({value}, {len(prefix) + 1}) ELSE {value} END")

    reason = " ".join(
        f"WHEN {section.condition} THEN {literal(f'{section.who} → {section.reason}')}"
        f" || COALESCE(': ' || {short(section)}, '')" for section in found)
    return Section(ALL, "Все", "Отклонённые", " OR ".join(f"({section.condition})" for section in found),
                   f"CASE {reason} END")


def find(key: str) -> Section | None:
    if key == ALL:
        return everything()
    return next((section for section in sections() if section.key == key), None)


def total() -> int:
    """Сколько публикаций во всех разделах — число плашки «Отклонённые»."""
    with connection.get_connection() as conn:
        conn.execute("SET LOCAL jit = off")
        row = conn.execute(f"SELECT COUNT(*) FROM signals s WHERE {everything().condition}").fetchone()
    return int(row[0])
