"""Журнал обмена с потребителем выпуска: всё, что видит вкладка «Журнал обмена» и печатает команда exchange-status
(#198, спека #197; готовность: #199; обновление, квитанции и отклики: #200).

Настройки обмена без ключей и адресов (только отпечатки ключей и число адресов) и что мешает собрать пакет, принять
пакет откликов и потребителю забрать пакет; готовность выпуска месяца; пакеты для потребителя, новые сверху, с
квитанциями и тем, что сборка сделала сверх состава; запуски обновления и отправленные сигналы в особых состояниях;
пакеты откликов и отказы точки обмена. Модуль только собирает: пакеты строит exchange_packages, обновление
exchange_updates, отклики принимает exchange_reactions.
"""

from __future__ import annotations

from typing import Any

from oiltech_digest import config, exchange_envelope, exchange_packages, exchange_reactions, exchange_updates
from oiltech_digest.api_access import consumer_networks, exchange_setup_problems
from oiltech_digest.db import connection, exchange_store


def journal(*, limit: int = 200) -> dict[str, Any]:
    """Состояние обмена для вкладки «Журнал обмена» и команды exchange-status."""
    keys = exchange_envelope.describe()
    point_problems = exchange_setup_problems()
    with connection.get_connection() as conn:
        packages = exchange_store.packages(conn, limit=limit)
        refusals = exchange_store.refusals(conn, limit=limit)
    for package in packages:
        package["notes"] = exchange_packages.package_notes(package.pop("details") or {})
    return {
        "readiness": exchange_packages.readiness(),
        "settings": {
            "enabled": bool(config.EXCHANGE_ENABLED),
            "first_month": config.EXCHANGE_FIRST_MONTH or None,
            "signing_keys": keys["signing"],
            "consumer_keys": keys["consumer"],
            "decryption_keys": keys["decryption"],
            "consumer_signing_keys": keys["consumer_signing"],
            "addresses": len(consumer_networks()),
            "token": not any("EXCHANGE_TOKEN_HASH" in problem for problem in point_problems),
            "problems": keys["problems"],
            "reaction_problems": keys["reaction_problems"],
            "point_problems": point_problems,
        },
        "packages": packages,
        "refusals": refusals,
        "reaction_packages": exchange_reactions.journal(limit=limit),
        **exchange_updates.journal(limit=limit),
    }
