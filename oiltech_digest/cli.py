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
    from oiltech_digest.config import RSS_PROBE_TIMEOUT
    from oiltech_digest.db import repository
    from oiltech_digest.ingestion.rss_discovery import discover_all

    stats = discover_all(
        only_missing=not args.force,
        source_id=args.source_id,
        workers=args.workers,
        dry_run=args.dry_run,
        limit=args.limit,
        timeout=args.timeout or RSS_PROBE_TIMEOUT,
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


def cmd_seed_tags(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.seed import seed_tags_from_directions

    stats = seed_tags_from_directions()
    print(f"Seed тегов: {stats['tags']}")


def cmd_seed_scoring(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.seed import seed_default_scoring_criteria

    stats = seed_default_scoring_criteria()
    print(f"Seed критериев скоринга: {stats['criteria']}, сумма весов={stats['weight_sum']}")


def cmd_summarize(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_summaries

    stats = process_summaries(limit=args.limit, offline=args.offline)
    print(f"summary: обработано={stats['processed']}, ошибок={stats['errors']}")


def cmd_tag(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_tags

    stats = process_tags(limit=args.limit, offline=args.offline)
    print(f"tagging: обработано={stats['processed']}, ошибок={stats['errors']}")


def cmd_score(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_scores

    stats = process_scores(limit=args.limit, offline=args.offline)
    print(f"scoring: обработано={stats['processed']}, ошибок={stats['errors']}")


def cmd_process(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_scores, process_summaries, process_tags

    summaries = process_summaries(limit=args.limit, offline=args.offline)
    tags = process_tags(limit=args.limit, offline=args.offline)
    scores = process_scores(limit=args.limit, offline=args.offline)
    print(
        "process: "
        f"summary={summaries['processed']}/{summaries['errors']} errors, "
        f"tagging={tags['processed']}/{tags['errors']} errors, "
        f"scoring={scores['processed']}/{scores['errors']} errors"
    )


def cmd_process_articles(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository
    from oiltech_digest.processing.pipeline import (
        make_client,
        process_score_articles,
        process_summary_articles,
        process_tag_articles,
    )

    client = make_client(args.offline)
    articles = repository.get_articles_by_ids(args.article_id, include_summary=False)
    summaries = process_summary_articles(articles, client)
    articles_with_summary = repository.get_articles_by_ids(args.article_id, include_summary=True)
    tags = process_tag_articles(articles_with_summary, client)
    scores = process_score_articles(articles_with_summary, client)
    print(
        "process-articles: "
        f"summary={summaries['processed']}/{summaries['errors']} errors, "
        f"tagging={tags['processed']}/{tags['errors']} errors, "
        f"scoring={scores['processed']}/{scores['errors']} errors"
    )


def cmd_ai_cost_report(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    rows = repository.ai_cost_report()
    if not rows:
        print("Метрик AI-обработки пока нет.")
        return
    for row in rows:
        print(
            f"{row['stage']} · {row['language']}: runs={row['runs']}, "
            f"input={row['input_tokens']}, output={row['output_tokens']}, "
            f"total={row['total_tokens']}, avg={row['avg_tokens_per_run']}, "
            f"cost=${row['cost_usd']}"
        )


def cmd_ai_article_cost_report(args: argparse.Namespace) -> None:
    from decimal import Decimal

    from oiltech_digest.db import repository

    rows = repository.ai_article_cost_report(limit=args.limit, complete_only=not args.include_partial)
    if not rows:
        print("Полных AI-циклов по статьям пока нет.")
        return

    total_cost = Decimal("0")
    total_tokens = 0
    for row in rows:
        cost = row["cost_usd"] or Decimal("0")
        tokens = row["total_tokens"] or 0
        total_cost += cost
        total_tokens += tokens
        title = row["title"]
        if len(title) > 90:
            title = title[:87] + "..."
        print(
            f"article={row['article_id']} · {row['language'] or 'unknown'} · "
            f"stages={row['stages']}/3 · tokens={tokens} · cost=${cost} · {title}"
        )

    avg_cost = total_cost / len(rows)
    avg_tokens = total_tokens / len(rows)
    print(f"\nСредний полный прогон 1 статьи: tokens={avg_tokens:.1f}, cost=${avg_cost:.6f}")


def cmd_sources(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    for row in repository.list_sources(search=args.search, limit=args.limit):
        status = "on" if row["enabled"] else "off"
        print(
            f"{row['id']:>4} {status:<3} {row.get('parse_strategy') or '-':<8} "
            f"{row['name']} · {row.get('update_frequency') or 'частота —'} · "
            f"{row.get('rss_url') or row.get('url') or '-'}"
        )


def cmd_article_candidates(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    rows = repository.find_article_candidates(args.query, limit=args.limit)
    if not rows:
        print("Кандидатов не найдено.")
        return
    for row in rows:
        print(f"{row['id']:>5} · {row['language'] or 'unknown'} · {row['source_name']} · {row['title']}")
        if row.get("snippet"):
            print(f"      {row['snippet']}")


def cmd_source_enable(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    repository.set_source_enabled(args.source_id, args.enabled)
    print(f"Источник {args.source_id}: {'включён' if args.enabled else 'выключен'}")


def cmd_source_add_rss(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    source_id = repository.add_rss_source(
        name=args.name,
        rss_url=args.rss_url,
        url=args.url,
        priority=args.priority,
        category=args.category,
        update_frequency=args.frequency,
    )
    print(f"RSS-источник сохранён: id={source_id}")


def cmd_digest_content(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.digest import write_digest_content

    stats = write_digest_content(
        path=args.output,
        month=args.month,
        limit=args.limit,
        min_score=args.min_score,
    )
    print(f"digest-content: файл={stats['path']}, статей={stats['items']}")


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
    p_disc.add_argument("--limit", type=int, default=None, help="проверить только первые N кандидатов")
    p_disc.add_argument("--timeout", type=int, default=None, help="таймаут одного probe-запроса, сек")
    p_disc.add_argument("--dry-run", action="store_true", help="не записывать в БД")
    p_disc.set_defaults(func=cmd_discover_rss)

    p_parse = sub.add_parser("parse", help="спарсить ленты в articles")
    p_parse.add_argument("--max-age-days", type=int, default=None, help="не сохранять статьи старше N дней")
    p_parse.add_argument("--source-id", type=int, default=None)
    p_parse.add_argument("--workers", type=int, default=10)
    p_parse.set_defaults(func=cmd_parse)

    sub.add_parser("stats", help="диагностика БД").set_defaults(func=cmd_stats)

    sub.add_parser("seed-tags", help="загрузить теги из направлений D01-D18").set_defaults(func=cmd_seed_tags)
    sub.add_parser("seed-scoring", help="создать базовые критерии скоринга").set_defaults(func=cmd_seed_scoring)

    def add_ai_args(p):
        p.add_argument("--limit", type=int, default=20)
        p.add_argument("--offline", action="store_true", help="детерминированная локальная заглушка без OpenAI API")

    p_summary = sub.add_parser("summarize", help="сформировать AI-суть статей")
    add_ai_args(p_summary)
    p_summary.set_defaults(func=cmd_summarize)

    p_tag = sub.add_parser("tag", help="присвоить статьи тегам")
    add_ai_args(p_tag)
    p_tag.set_defaults(func=cmd_tag)

    p_score = sub.add_parser("score", help="рассчитать скоринг статей")
    add_ai_args(p_score)
    p_score.set_defaults(func=cmd_score)

    p_process = sub.add_parser("process", help="summary → tagging → scoring")
    add_ai_args(p_process)
    p_process.set_defaults(func=cmd_process)

    p_process_articles = sub.add_parser("process-articles", help="summary → tagging → scoring для выбранных article_id")
    p_process_articles.add_argument("article_id", nargs="+", type=int)
    p_process_articles.add_argument("--offline", action="store_true", help="детерминированная локальная заглушка без OpenAI API")
    p_process_articles.set_defaults(func=cmd_process_articles)

    sub.add_parser("ai-cost-report", help="отчёт по токенам/стоимости AI-этапов").set_defaults(func=cmd_ai_cost_report)

    p_article_cost = sub.add_parser("ai-article-cost-report", help="стоимость полного AI-прогона одной статьи")
    p_article_cost.add_argument("--limit", type=int, default=20)
    p_article_cost.add_argument("--include-partial", action="store_true", help="показывать статьи с неполным циклом")
    p_article_cost.set_defaults(func=cmd_ai_article_cost_report)

    p_sources = sub.add_parser("sources", help="список источников")
    p_sources.add_argument("--search", default=None)
    p_sources.add_argument("--limit", type=int, default=50)
    p_sources.set_defaults(func=cmd_sources)

    p_candidates = sub.add_parser("article-candidates", help="найти статьи-кандидаты по ключевым словам")
    p_candidates.add_argument("query", help="ключевые слова через пробел")
    p_candidates.add_argument("--limit", type=int, default=20)
    p_candidates.set_defaults(func=cmd_article_candidates)

    p_source_enable = sub.add_parser("source-enable", help="включить/выключить источник")
    p_source_enable.add_argument("source_id", type=int)
    p_source_enable.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    p_source_enable.set_defaults(func=cmd_source_enable)

    p_source_add = sub.add_parser("source-add-rss", help="добавить RSS-источник вручную")
    p_source_add.add_argument("name")
    p_source_add.add_argument("rss_url")
    p_source_add.add_argument("--url", default=None)
    p_source_add.add_argument("--priority", type=float, default=1.0)
    p_source_add.add_argument("--category", default=None)
    p_source_add.add_argument("--frequency", default=None, help="частота мониторинга")
    p_source_add.set_defaults(func=cmd_source_add_rss)

    p_digest = sub.add_parser("digest-content", help="собрать digest_content.json из обработанных статей")
    p_digest.add_argument("month", help="YYYY-MM")
    p_digest.add_argument("--output", default="digest_content.generated.json")
    p_digest.add_argument("--limit", type=int, default=20)
    p_digest.add_argument("--min-score", type=float, default=60)
    p_digest.set_defaults(func=cmd_digest_content)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
