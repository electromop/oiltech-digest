"""Отрицательные отклики потребителя выпуска во входе разбора ОС (#200, спека #197).

Отклики «Не про нефтесервис», «Ошибка в карточке», «Не новое», «Мало фактов», принятые точкой обмена с прошлого среза,
входят в пакет недельного среза как отметки без автора с ролью «сотрудник потребителя», рядом с отметками людей
платформы; положительные («Сохранить», «В работу», «В выгрузку», «Полезный сигнал») в срез не идут. Отклик относится к
корню склейки своего сигнала. Снятый отклик гасит поставленный раньше того же вида на той же карточке (автора у отклика
нет, пару находит вид): в разбор идут только те, что стоят.

Предложение ссылается на отклики отдельным списком рядом с номерами отметок; цитатой служит подпись кнопки; порог
улучшения считает отклики как отметки участников; уборку обосновывает и «Не про нефтесервис». Карточку отклики меняют
только через согласованное предложение. В эталон ОС они не входят: он строится из журнала отметок ОС. Задаче разбора с
откликами нужна сборка воркера контракта 14 (contract.required_contract).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from oiltech_digest.db import connection, exchange_reactions_store
from oiltech_digest.exchange_reactions import LABELS, NEGATIVE

ROLE = "сотрудник потребителя"
# Отклик, который обосновывает уборку карточки, как отметка «Не релевантно».
REMOVAL = "not_oilfield"


def _standing(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Отклики, которые стоят: снятый гасит поставленный раньше того же вида у той же карточки."""
    stacks: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (int(row["root"]), str(row["reaction"]))
        if row["placed"]:
            stacks.setdefault(key, []).append(row)
        elif stacks.get(key):
            stacks[key].pop()
    return sorted((row for stack in stacks.values() for row in stack), key=lambda row: int(row["id"]))


def _window(since: datetime, until: datetime) -> list[dict[str, Any]]:
    with connection.get_connection() as conn:
        return _standing(exchange_reactions_store.accepted_between(conn, NEGATIVE, since, until))


def new_signals(window_from: datetime, window_to: datetime) -> list[int]:
    """Корни склейки сигналов, у которых в окне среза есть отрицательные отклики."""
    return sorted({int(row["root"]) for row in _window(window_from, window_to)})


def for_signals(signal_ids: Sequence[int], since: datetime, until: datetime) -> dict[int, list[dict[str, Any]]]:
    """Отрицательные отклики окна по карточкам (корням склейки): номер, вид, день."""
    found: dict[int, list[dict[str, Any]]] = {int(one): [] for one in signal_ids}
    for row in _window(since, until):
        if int(row["root"]) in found:
            found[int(row["root"])].append(row)
    return found


def payload(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Отклики в пакете задачи для воркера: без автора и метки источника, только роль, день и подпись кнопки."""
    return [{"id": int(row["id"]), "role": ROLE, "day": str(row["day"]), "reaction": LABELS[row["reaction"]]}
            for row in rows]


def label(row: dict[str, Any]) -> str:
    """Подпись кнопки отклика: текст, из которого берётся цитата."""
    return LABELS.get(str(row.get("reaction") or ""), "")


def basis(reaction_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Отклики по номерам для журнала ОС: основание предложения отдельным списком рядом с отметками."""
    with connection.get_connection() as conn:
        rows = exchange_reactions_store.by_id(conn, reaction_ids)
    return {one: {"id": one, "role": ROLE, "reaction": LABELS.get(row["reaction"], row["reaction"]),
                  "day": str(row["day"]), "signal_id": int(row["signal_id"])} for one, row in rows.items()}
