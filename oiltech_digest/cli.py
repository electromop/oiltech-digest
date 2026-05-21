"""CLI: init-db / seed-sources / discover-rss / parse / stats.

Запуск: python -m oiltech_digest.cli <command> [options]
"""

from __future__ import annotations

import argparse
import logging


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_init_db(args: argparse.Namespace) -> None:
    from oiltech_digest.db import connection

    tables = connection.init_db()
    print(f"БД инициализирована. Таблиц в схеме: {len(tables)}")
    for t in tables:
        print(f"  - {t}")


def cmd_seed_sources(args: argparse.Namespace) -> None:
    from oiltech_digest.ingestion.excel_seed import seed_sources_from_excel

    stats = seed_sources_from_excel()
    print(
        f"Seed источников: всего={stats['total']} "
        f"(вставлено={stats['inserted']}, обновлено={stats['updated']}, "
        f"telegram={stats['telegram_flagged']})"
    )


def cmd_discover_rss(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository
    from oiltech_digest.ingestion.rss_discovery import discover_all

    stats = discover_all(
        only_missing=not args.force,
        source_id=args.source_id,
        workers=args.workers,
        dry_run=args.dry_run,
    )
    print(
        f"discover-rss: проверено={stats['checked']}, "
        f"найден RSS={stats['rss']}, без RSS (request)={stats['request']}"
        + (" [dry-run, без записи]" if args.dry_run else "")
    )
    print("\nОтчёт применимости (распределение в БД):")
    for row in repository.sources_by_strategy():
        print(f"  {row['parse_strategy'] or '—'}: {row['n']}")


def cmd_parse(args: argparse.Namespace) -> None:
    from oiltech_digest.ingestion.rss_parser import parse_all

    stats = parse_all(
        max_age_days=args.max_age_days, workers=args.workers, source_id=args.source_id
    )
    print(
        f"parse: добавлено={stats['added']}, дублей={stats['duplicates']}, "
        f"пропущено по возрасту={stats['skipped_old']}, "
        f"источников ок={stats['sources_ok']}, ошибок={stats['errors']}"
    )


def cmd_stats(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    print(f"Источников: {repository.count_sources()}")
    for row in repository.sources_by_strategy():
        print(f"  {row['parse_strategy'] or '—'}: {row['n']}")
    print(f"Статей: {repository.count_articles()}")
    print(f"Кандидатов кросс-дублей (один content_hash у разных URL): {repository.cross_dup_candidates()}")
    top = repository.top_sources()
    if top:
        print("Топ источников по числу статей:")
        for row in top:
            print(f"  {row['name']}: {row['n']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oiltech_digest.cli", description="OilTech Digest — сбор RSS")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог (INFO)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="создать схему БД").set_defaults(func=cmd_init_db)
    sub.add_parser("seed-sources", help="загрузить источники из Excel").set_defaults(func=cmd_seed_sources)

    p_disc = sub.add_parser("discover-rss", help="автообнаружение RSS-лент")
    p_disc.add_argument("--force", action="store_true", help="перепроверить все, а не только без rss_url")
    p_disc.add_argument("--source-id", type=int, default=None, help="только указанный источник")
    p_disc.add_argument("--workers", type=int, default=10)
    p_disc.add_argument("--dry-run", action="store_true", help="не записывать в БД")
    p_disc.set_defaults(func=cmd_discover_rss)

    p_parse = sub.add_parser("parse", help="спарсить ленты в articles")
    p_parse.add_argument("--max-age-days", type=int, default=None, help="не сохранять статьи старше N дней")
    p_parse.add_argument("--source-id", type=int, default=None)
    p_parse.add_argument("--workers", type=int, default=10)
    p_parse.set_defaults(func=cmd_parse)

    sub.add_parser("stats", help="диагностика БД").set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
