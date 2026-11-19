"""Команды обмена с потребителем выпуска (#198, спека #197). Подключает их cli.build_parser (add_commands).

exchange-release          шаг планировщика: выпуск закрытого месяца с первого из настройки (за флагом EXCHANGE_ENABLED)
exchange-release --month  выпуск названного закрытого месяца (стартовая загрузка), флаг не нужен
exchange-addition         дозагрузка номеров с месяцем выпуска
exchange-new-key          новая пара ключей платформы: закрытая часть в .env ядра, открытая с отпечатком потребителю
exchange-status           то же состояние, что вкладка «Журнал обмена»
"""

from __future__ import annotations

import argparse
import re
from zoneinfo import ZoneInfo

_MSK = ZoneInfo("Europe/Moscow")


def _moment(value) -> str:
    return value.astimezone(_MSK).strftime("%d.%m.%Y %H:%M") if value is not None else ""


def cmd_exchange_release(args: argparse.Namespace) -> None:
    from oiltech_digest import exchange_packages

    try:
        built = ([exchange_packages.build_release(args.month, by="команда")] if args.month
                 else exchange_packages.release_step())
    except exchange_packages.Refused as exc:
        raise SystemExit(f"exchange-release: {exc}") from exc
    if built is None:
        print("exchange-release: шаг выключен (EXCHANGE_ENABLED)")
        return
    if not built:
        print("exchange-release: собирать нечего")
    for one in built:
        print(f"exchange-release: {one.describe()}")


def cmd_exchange_addition(args: argparse.Namespace) -> None:
    from oiltech_digest import exchange_packages

    numbers = [int(part) for part in re.split(r"[\s,]+", args.ids or "") if part]
    try:
        result = exchange_packages.build_addition(numbers, args.month, by="команда")
    except exchange_packages.Refused as exc:
        raise SystemExit(f"exchange-addition: {exc}") from exc
    for line in result.describe():
        print(f"exchange-addition: {line}")


def cmd_exchange_new_key(args: argparse.Namespace) -> None:
    from oiltech_digest import exchange_envelope

    pair = exchange_envelope.new_key_pair(args.kind)
    print(f"Закрытый ключ: в .env ядра, {pair['setting']} (новый первым, прежний через запятую, пока действует):")
    print(pair["private"])
    print(f"Открытый ключ для потребителя: {pair['public']}")
    print(f"Отпечаток: {pair['fingerprint']}")


def cmd_exchange_status(args: argparse.Namespace) -> None:
    from oiltech_digest import exchange_packages

    state = exchange_packages.journal(limit=args.limit)
    settings = state["settings"]
    print(f"Шаг выпуска: {'включён' if settings['enabled'] else 'выключен'}; первый месяц: "
          f"{settings['first_month'] or 'не задан'}")
    print(f"Ключ подписи платформы: {', '.join(settings['signing_keys']) or 'нет'}; ключ потребителя: "
          f"{', '.join(settings['consumer_keys']) or 'нет'}")
    print(f"Адресов потребителя: {settings['addresses']}; токен: {'задан' if settings['token'] else 'не задан'}")
    for problem in settings["problems"]:
        print(f"Не готово: {problem}")
    if not state["packages"]:
        print("Пакетов нет")
    for package in state["packages"]:
        label = exchange_packages.package_label(package["kind"], package["month"])
        taken = (f"забран {_moment(package['first_taken_at'])}, последний раз {_moment(package['last_taken_at'])}"
                 if package["first_taken_at"] else "не забран")
        print(f"№{package['number']} {label}: карточек {len(package['cards'])}, собран "
              f"{_moment(package['built_at'])} МСК ({package['built_by'] or 'кто, не записано'}), {taken}")
        if package["cards"]:
            print("    " + ", ".join(str(card["signal_id"]) for card in package["cards"]))


def add_commands(sub) -> None:
    p_release = sub.add_parser(
        "exchange-release",
        help="шаг планировщика «выпуск для потребителя»: пакет выпуска закрытого месяца (за флагом EXCHANGE_ENABLED); "
             "с --month: выпуск названного закрытого месяца",
    )
    p_release.add_argument("--month", default="", help="закрытый месяц ГГГГ-ММ (стартовая загрузка), флаг не нужен")
    p_release.set_defaults(func=cmd_exchange_release)

    p_addition = sub.add_parser("exchange-addition", help="дозагрузка сигналов потребителю по номерам")
    p_addition.add_argument("--month", required=True, help="месяц выпуска ГГГГ-ММ, с которым карточки уйдут")
    p_addition.add_argument("--ids", required=True, help="номера сигналов через запятую или пробел")
    p_addition.set_defaults(func=cmd_exchange_addition)

    p_key = sub.add_parser("exchange-new-key", help="новая пара ключей платформы для обмена с потребителем")
    p_key.add_argument("--kind", choices=["signing", "decryption"], required=True,
                       help="signing: подпись пакетов; decryption: расшифровка откликов")
    p_key.set_defaults(func=cmd_exchange_new_key)

    p_status = sub.add_parser("exchange-status", help="состояние обмена: настройки и пакеты, как вкладка «Журнал обмена»")
    p_status.add_argument("--limit", type=int, default=50, help="сколько последних пакетов показать")
    p_status.set_defaults(func=cmd_exchange_status)
