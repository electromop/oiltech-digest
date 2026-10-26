"""Возврат публикации из «Карточки» в «Отбор» (#132, спека #128): если «Карточка» нашла в тексте дату события старше
OTBOR_OLD_EVENT_DAYS, принятая «Отбором» публикация возвращается ему с этим фактом, один раз; если он снова примет —
пометка «спорное» (otbor_store.RETURN_DISPUTED). Без даты события в тексте возврата нет: дата публикации вместо неё не
подставляется (event_dates).
"""

from __future__ import annotations

from typing import Any

from oiltech_digest import config, contract, event_dates
from oiltech_digest.db import signal_card_store


def old_event(signal_id: int, values: dict[str, Any], publication: dict[str, Any]) -> str | None:
    """Вернуть принятую «Отбором» публикацию в «Отбор», если «Карточка» нашла в тексте старое событие: решение
    снимается, факт ложится во вход «Отбора». Один раз; решение человека и выбор «в дайджест» не трогаются
    (signal_card_store.return_to_otbor). Без «Отбора» (флаг, модель) возврат не делается: решать было бы некому.
    Номер публикации для «Отбора» (contract.article_ref или signal_ref); None — возврата нет."""
    fact = event_dates.old_event_fact(values, publication["published"], publication["received"],
                                      config.OTBOR_OLD_EVENT_DAYS)
    if fact is None or not (config.OTBOR_ENABLED and config.OTBOR_MODEL):
        return None
    step = {"stage": "«Карточка»",
            "text": f"Старое событие: {fact}, старше {config.OTBOR_OLD_EVENT_DAYS} дней: возврат в «Отбор»"}
    return signal_card_store.return_to_otbor(signal_id, fact, log=[step])


def enqueue(refs: list[str]) -> None:
    """Задачи «Отбора» о возвращённых публикациях — сразу, по полосе пересчётов: окно шага планировщика старую статью
    уже не берёт."""
    from oiltech_digest import otbor

    otbor.enqueue([one for ref in refs if (one := contract.ref_article_id(ref)) is not None],
                  signal_ids=[one for ref in refs if (one := contract.ref_signal_id(ref)) is not None],
                  trigger="card_return", bulk=True)
