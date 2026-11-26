"""Команды страниц LinkedIn через сервис Apify. Подключает их cli.build_parser (add_commands).

add-linkedin-source <адрес>   завести страницу компании или профиль эксперта источником; --collect-since ГГГГ-ММ-ДД:
                              дата начала сбора по Москве (по умолчанию две недели до заведения)
linkedin-collect              шаг планировщика: посты страниц, которым пора (за флагом LINKEDIN_APIFY_ENABLED и не чаще
                              раза в LINKEDIN_MIN_INTERVAL_HOURS); --force: без флага и интервала, потолки расхода
                              действуют; --source-id: одна страница; --dry-run: план без сервиса и без записи
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

_MSK = ZoneInfo("Europe/Moscow")


def _msk_day(value: str) -> datetime:
    """Дата ГГГГ-ММ-ДД как начало суток по Москве."""
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"нужна дата ГГГГ-ММ-ДД, а не {value!r}") from None
    return datetime.combine(day, time(), _MSK)


def cmd_add_linkedin_source(args: argparse.Namespace) -> None:
    from oiltech_digest.db import linkedin_store

    try:
        added = linkedin_store.add_page(args.url, name=args.name, priority=args.priority, category=args.category,
                                        collect_since=args.collect_since)
    except ValueError as exc:  # PageExists тоже ValueError
        raise SystemExit(f"add-linkedin-source: {exc}") from None
    since = args.collect_since.strftime("%d.%m.%Y") if args.collect_since else "двух недель до заведения"
    kind = "страница компании" if added["kind"] == "company" else "профиль эксперта"
    print(f"add-linkedin-source: #{added['id']} {added['name']} ({kind}) → {added['url']}; посты с {since}")


def cmd_linkedin_collect(args: argparse.Namespace) -> None:
    from oiltech_digest import config
    from oiltech_digest.ingestion import linkedin_apify

    stats = linkedin_apify.collect(source_id=args.source_id, force=args.force, dry_run=args.dry_run)
    if args.dry_run:
        for row in stats["plan"]:
            print(f"  #{row['source_id']} {row['name']} ({row['kind']}): посты с {row['since']}, "
                  f"потолок запуска ${row['cap_usd']:.2f}")
        print(f"linkedin-collect --dry-run: к сбору {len(stats['plan'])}; за месяц ${stats['month_spend_usd']:.2f} "
              f"из ${config.LINKEDIN_APIFY_MONTH_MAX_USD:.2f}" + (f"; {stats['stopped']}" if stats["stopped"] else ""))
        return
    print(f"linkedin-collect: страниц {stats['pages']}, запусков {stats['runs']}, постов {stats['posts']}, "
          f"добавлено {stats['added']}, известных {stats['known']}, раньше даты начала {stats['old']}, "
          f"отсеял предфильтр {stats['prefilter']}, дублей {stats['duplicates']}, картинок {stats['images']}, "
          f"сбоев {stats['failed']}; расход ${stats['cost_usd']:.4f}, за месяц ${stats['month_spend_usd']:.2f} "
          f"из ${config.LINKEDIN_APIFY_MONTH_MAX_USD:.2f}" + (f"; {stats['stopped']}" if stats["stopped"] else ""))
    # Ни одна страница не собралась, а сбои были: код ошибки, чтобы планировщик написал FAIL шага.
    if stats["failed"] and not stats["pages"]:
        raise SystemExit(1)


def add_commands(sub) -> None:
    p_add = sub.add_parser("add-linkedin-source",
                           help="завести страницу компании или профиль эксперта в LinkedIn источником (сбор через Apify)")
    p_add.add_argument("url", help="адрес страницы: linkedin.com/company/<имя>/ или linkedin.com/in/<имя>/")
    p_add.add_argument("--name", default=None, help="имя источника; по умолчанию «<имя в адресе> (LinkedIn)»")
    p_add.add_argument("--collect-since", type=_msk_day, default=None,
                       help="дата начала сбора ГГГГ-ММ-ДД по Москве; по умолчанию две недели до заведения")
    p_add.add_argument("--priority", type=float, default=1.0)
    p_add.add_argument("--category", default=None)
    p_add.set_defaults(func=cmd_add_linkedin_source)

    p_collect = sub.add_parser("linkedin-collect",
                               help="шаг планировщика: посты страниц LinkedIn через Apify (за флагом LINKEDIN_APIFY_ENABLED)")
    p_collect.add_argument("--source-id", type=int, default=None, help="только эта страница")
    p_collect.add_argument("--force", action="store_true",
                           help="без флага и без интервала между сборами страницы; потолки расхода действуют")
    p_collect.add_argument("--dry-run", action="store_true", help="план: страницы, границы и расход, без сервиса")
    p_collect.set_defaults(func=cmd_linkedin_collect)
