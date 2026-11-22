"""Команды обмена с потребителем выпуска (#198, спека #197) и готовности выпуска (#199). Подключает их
cli.build_parser (add_commands).

exchange-release          шаг планировщика: выпуск закрытого месяца с первого из настройки (за флагом EXCHANGE_ENABLED),
                          когда готовы все его материалы; неготовый ждёт
exchange-release --month  выпуск названного закрытого месяца (стартовая загрузка), флаг не нужен; --as-is: «Отправить
                          как есть», сразу, с записью в журнал обмена, что было не готово
exchange-addition         дозагрузка номеров с месяцем выпуска
exchange-update           шаг планировщика «обновление для потребителя»: раз в сутки по Москве (за флагом
                          EXCHANGE_ENABLED) изменённые карточки, склейки, снятия и возвраты; --now: сейчас, флаг не нужен
exchange-new-key          новая пара ключей платформы: закрытая часть в .env ядра, открытая с отпечатком потребителю
exchange-reactions        выгрузка откликов потребителя за месяц в xlsx: счёт по корню склейки на конец месяца
exchange-status           то же состояние, что вкладка «Журнал обмена»
digest-readiness          шаг планировщика «перепроверка готовности»: в последний день перед закрытием месяца с часа
                          READINESS_RECHECK_HOUR и после закрытия до сборки выпуска недостающие шаги конвейера заново;
                          --month: готовность выпуска названного месяца, только чтение
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

    if args.as_is and not args.month:
        raise SystemExit("exchange-release: --as-is отправляет выпуск названного месяца: нужен --month ГГГГ-ММ")
    try:
        if args.month:
            built = exchange_packages.build_release(args.month, by="команда", as_is=args.as_is)
            print(f"exchange-release: {built.report()}")
            return
        step = exchange_packages.release_step()
    except exchange_packages.Refused as exc:
        raise SystemExit(f"exchange-release: {exc}") from exc
    if step is None:
        print("exchange-release: шаг выключен (EXCHANGE_ENABLED)")
        return
    if not step.built and not step.waiting:
        print("exchange-release: собирать нечего")
    for one in step.built:
        print(f"exchange-release: {one.describe()}")
    for waiting in step.waiting:
        print(f"exchange-release: {waiting.summary}")


def _print_readiness(state: dict) -> None:
    """Готовность выпуска месяца, как блок вкладки «Журнал обмена»."""
    from oiltech_digest import feed_window, material_readiness

    label = feed_window.month_label(feed_window.parse_month(state["month"]))
    release = f"; пакет выпуска №{state['release']}" if state["release"] else ""
    print(f"Выпуск за {label}: готово {state['ready']} из {state['total']}{release}")
    for one in state["not_ready"]:
        title = f" {one['title']}" if one["title"] else ""
        print(f"    {material_readiness.number_label(one['signal_id'], one['id'])}{title}: "
              f"{material_readiness.labels(one['missing'])}")
    if state["as_is"]["reason"] and state["not_ready"]:
        print(f"    Отправить как есть нельзя. {state['as_is']['reason']}")


def cmd_digest_readiness(args: argparse.Namespace) -> None:
    from oiltech_digest import exchange_packages, feed_window, material_readiness

    if args.month:
        try:
            _print_readiness(exchange_packages.readiness(args.month))
        except exchange_packages.Refused as exc:
            raise SystemExit(f"digest-readiness: {exc}") from exc
        return
    moment = feed_window._now()
    try:
        waiting = exchange_packages.pending_release_months(now=moment)
    except exchange_packages.Refused:
        waiting = []  # настройку шага выпуска называет его шаг, перепроверка идёт дальше
    checked = material_readiness.recheck_step(moment, waiting=waiting)
    if not checked:
        print("digest-readiness: перепроверять нечего (не день перед закрытием месяца, выпуск не ждёт)")
    for one in checked:
        label = feed_window.month_label(feed_window.parse_month(one.month))
        lacking = sum(1 for material in one.materials if material.missing)
        print(f"digest-readiness: выпуск за {label}: не готовы {lacking} из {len(one.materials)}, "
              f"поставлено задач {len(one.enqueued.jobs)}")
        for step, ids in one.enqueued.steps.items():
            print(f"    {step}: {', '.join(map(str, ids))}")
        for reason in one.enqueued.skipped:
            print(f"    не поставлено: {reason}")


def cmd_exchange_update(args: argparse.Namespace) -> None:
    from oiltech_digest import exchange_packages, exchange_updates

    try:
        result = (exchange_updates.build_update(by="команда", trigger=exchange_updates.MANUAL) if args.now
                  else exchange_updates.update_step())
    except exchange_packages.Refused as exc:
        raise SystemExit(f"exchange-update: {exc}") from exc
    if result is None:
        print("exchange-update: шаг выключен (EXCHANGE_ENABLED)")
        return
    print(f"exchange-update: {result.built.describe() if result.built else result.message}")
    for note in result.notes:
        print(f"    {note}")


def cmd_exchange_addition(args: argparse.Namespace) -> None:
    from oiltech_digest import exchange_packages

    numbers = [int(part) for part in re.split(r"[\s,]+", args.ids or "") if part]
    try:
        result = exchange_packages.build_addition(numbers, args.month, by="команда")
    except exchange_packages.Refused as exc:
        raise SystemExit(f"exchange-addition: {exc}") from exc
    for line in result.describe():
        print(f"exchange-addition: {line}")


def cmd_exchange_reactions(args: argparse.Namespace) -> None:
    from pathlib import Path

    from oiltech_digest import config, exchange_packages, exchange_reactions, feed_window

    try:
        month = exchange_packages._checked_month(args.month)
    except exchange_packages.Refused as exc:
        raise SystemExit(f"exchange-reactions: {exc}") from exc
    target = Path(args.out) if args.out else config.EXPORTS_DIR / f"exchange-reactions-{month}.xlsx"
    rows = exchange_reactions.export_month(month, target)
    label = feed_window.month_label(feed_window.parse_month(month))
    print(f"exchange-reactions: отклики за {label}: сигналов {rows}, файл {target}")


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
        print(f"Пакеты не соберутся: {problem}")
    for problem in settings["point_problems"]:
        print(f"Потребитель не заберёт пакеты: {problem}")
    _print_readiness(state["readiness"])
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
        for note in package["notes"]:
            print(f"    {note}")


def add_commands(sub) -> None:
    p_release = sub.add_parser(
        "exchange-release",
        help="шаг планировщика «выпуск для потребителя»: пакет выпуска закрытого месяца (за флагом EXCHANGE_ENABLED); "
             "с --month: выпуск названного закрытого месяца",
    )
    p_release.add_argument("--month", default="", help="закрытый месяц ГГГГ-ММ (стартовая загрузка), флаг не нужен")
    p_release.add_argument("--as-is", action="store_true",
                           help="«Отправить как есть»: выпуск месяца сразу, с записью, какие материалы были не готовы")
    p_release.set_defaults(func=cmd_exchange_release)

    p_readiness = sub.add_parser(
        "digest-readiness",
        help="шаг планировщика «перепроверка готовности выпуска»: недостающие шаги конвейера неготовым материалам; "
             "с --month: готовность выпуска месяца, только чтение",
    )
    p_readiness.add_argument("--month", default="", help="месяц выпуска ГГГГ-ММ: показать готовность, ничего не ставить")
    p_readiness.set_defaults(func=cmd_digest_readiness)

    p_update = sub.add_parser(
        "exchange-update",
        help="шаг планировщика «обновление для потребителя»: раз в сутки (за флагом EXCHANGE_ENABLED); с --now: сейчас",
    )
    p_update.add_argument("--now", action="store_true", help="собрать обновление сейчас, флаг и сутки не нужны")
    p_update.set_defaults(func=cmd_exchange_update)

    p_addition = sub.add_parser("exchange-addition", help="дозагрузка сигналов потребителю по номерам")
    p_addition.add_argument("--month", required=True, help="месяц выпуска ГГГГ-ММ, с которым карточки уйдут")
    p_addition.add_argument("--ids", required=True, help="номера сигналов через запятую или пробел")
    p_addition.set_defaults(func=cmd_exchange_addition)

    p_reactions = sub.add_parser("exchange-reactions", help="выгрузка откликов потребителя за месяц (xlsx)")
    p_reactions.add_argument("--month", required=True, help="месяц ГГГГ-ММ: отклики, нажатые в этом месяце")
    p_reactions.add_argument("--out", default="", help="файл xlsx (по умолчанию exports/exchange-reactions-ГГГГ-ММ.xlsx)")
    p_reactions.set_defaults(func=cmd_exchange_reactions)

    p_key = sub.add_parser("exchange-new-key", help="новая пара ключей платформы для обмена с потребителем")
    p_key.add_argument("--kind", choices=["signing", "decryption"], required=True,
                       help="signing: подпись пакетов; decryption: расшифровка откликов")
    p_key.set_defaults(func=cmd_exchange_new_key)

    p_status = sub.add_parser("exchange-status", help="состояние обмена: настройки и пакеты, как вкладка «Журнал обмена»")
    p_status.add_argument("--limit", type=int, default=50, help="сколько последних пакетов показать")
    p_status.set_defaults(func=cmd_exchange_status)
