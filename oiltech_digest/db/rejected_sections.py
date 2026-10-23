"""Разделы отклонённого (#130, спека #128): «кто убрал → причина» — на месте «Отсеянных и скрытых», только админу.

У каждой публикации раздела короткая причина. «Отбор» — по разделу на каждую причину отказа из справочника;
сбой — «„Отбор“ не ответил» (технический, шаг повторит) и «Ответ не по правилам» (без повтора, решение владельца
23.10); поиск — «Ссылка не подтверждена»; «Другое» — принятое «Отбором», которое видит только админ (витрины его
не показывают); «До „Отбора“» — скрытое прежними правилами радара: решённое до «Отбора» живёт по ним. Дубли в
разделы не идут — их ссылки в главной карточке; выбранное «в дайджест» видно в витрине и в разделы тоже не идёт.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg.rows import dict_row

from oiltech_digest.db import connection
from oiltech_digest.db.otbor_store import FAILURE_INVALID, FAILURE_TECHNICAL
from oiltech_digest.db.repository import (_BUSINESS_VISIBLE_SQL, _RADAR_HIDDEN_SQL, _RADAR_VISIBLE_SQL,
                                          _radar_hidden_reason_sql, signal_digest_sql)
from oiltech_digest.otbor_pack_format import load_dictionaries

# Причина отсева поиском «режима ChatGPT», которую пишет signal_research.verify_research_evidence: ссылка не
# открылась и её нет среди источников поиска (сторож — tests/test_otbor_feed.py).
UNCONFIRMED_LINK = "страница не открылась и адрес не из найденного"
# IS NOT DISTINCT FROM: у карточки без этапа отсева условие — FALSE, а не NULL (оно идёт и под NOT).
_UNCONFIRMED_SQL = (f"(s.filter_stage IS NOT DISTINCT FROM 'search'"
                    f" AND s.filter_reason IS NOT DISTINCT FROM '{UNCONFIRMED_LINK}')")
# Не видна ни в одной витрине и не дубль: только такую раздел и держит.
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


def sections() -> list[Section]:
    """Все разделы по порядку экрана: «Отбор» по причинам справочника, сбой, поиск, «Другое», «До „Отбора“»."""
    rejected = f"{_NOT_SHOWN_SQL} AND s.otbor_decision = 'reject'"
    undecided = f"{_NOT_SHOWN_SQL} AND s.otbor_decision IS NULL"
    reasons = load_dictionaries()["reject_reason"]["values"]
    return [
        *[Section(f"otbor:{value['code']}", "«Отбор»", value["label"],
                  f"{rejected} AND s.reject_reason = '{value['code']}'", "s.verdict_reason") for value in reasons],
        # Отказ без причины бывает, только если шаги ответа не сошлись и после повтора («спорное»).
        Section("otbor_no_reason", "«Отбор»", "Без причины (спорное)", f"{rejected} AND s.reject_reason IS NULL",
                "COALESCE(s.otbor_disputed, s.verdict_reason)"),
        Section("otbor_failed", "Сбой", "«Отбор» не ответил",
                f"{undecided} AND s.otbor_failure = '{FAILURE_TECHNICAL}'", "s.otbor_failure_reason"),
        Section("otbor_invalid", "Сбой", "Ответ не по правилам",
                f"{undecided} AND s.otbor_failure = '{FAILURE_INVALID}'", "s.otbor_failure_reason"),
        Section("search_unconfirmed", "Поиск", "Ссылка не подтверждена",
                f"{_RADAR_HIDDEN_SQL} AND {_UNCONFIRMED_SQL}", "s.filter_reason"),
        Section("other", "«Отбор»", "Другое: видно только админу",
                f"{_NOT_SHOWN_SQL} AND s.otbor_decision = 'accept' AND s.signal_category = 'other'"
                f" AND (s.archived_at IS NULL OR {signal_digest_sql('s')})", "s.verdict_reason"),
        Section("before_otbor", "До «Отбора»", "Скрыто прежними правилами радара",
                f"{_RADAR_HIDDEN_SQL} AND NOT {_UNCONFIRMED_SQL}", _radar_hidden_reason_sql()),
    ]


def find(key: str) -> Section | None:
    return next((section for section in sections() if section.key == key), None)


def counts() -> list[dict]:
    """Разделы с числом публикаций в каждом — для экрана админа, все, и пустые тоже."""
    found = sections()
    select = ", ".join(f"COUNT(*) FILTER (WHERE {section.condition}) AS c{number}"
                       for number, section in enumerate(found))
    with connection.get_connection() as conn:
        conn.execute("SET LOCAL jit = off")
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(f"SELECT {select} FROM signals s")
        row = cur.fetchone()
    return [{"key": section.key, "who": section.who, "reason": section.reason, "count": int(row[f"c{number}"])}
            for number, section in enumerate(found)]
