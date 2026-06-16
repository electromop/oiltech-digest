"""CLI: init-db / seed-sources / discover-rss / parse / stats.

Запуск: python -m oiltech_digest.cli <command> [options]
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time


def _setup_logging(verbose: bool) -> None:
    from oiltech_digest.logging_utils import setup_logging

    setup_logging("cli", verbose=verbose, force=True)


def cmd_init_db(args: argparse.Namespace) -> None:
    from oiltech_digest.db import connection

    tables = connection.init_db()
    print(f"БД инициализирована. Таблиц в схеме: {len(tables)}")
    for t in tables:
        print(f"  - {t}")


def cmd_schema_check(args: argparse.Namespace) -> None:
    from oiltech_digest.readiness import schema_check

    report = schema_check()
    if report["ok"]:
        print(f"schema-check: ok, required_tables={len(report['required_tables'])}")
        return
    print(
        "schema-check: missing tables: "
        + ", ".join(report["missing_tables"])
    )
    raise SystemExit(1)


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
        f"отсеяно как шум={stats['skipped_irrelevant']}, "
        f"источников ок={stats['sources_ok']}, ошибок={stats['errors']}"
    )


def cmd_fetch_full_text(args: argparse.Namespace) -> None:
    from oiltech_digest.ingestion.article_fetcher import fetch_full_text

    stats = fetch_full_text(
        limit=args.limit,
        min_chars=args.min_chars,
        retry_too_short=args.retry_too_short,
    )
    print(
        f"fetch-full-text: проверено={stats['processed']}, обновлено={stats['updated']}, "
        f"слишком коротких={stats['too_short']}, ошибок={stats['failed']}"
    )


def cmd_backfill_images(args: argparse.Namespace) -> None:
    from oiltech_digest.ingestion.article_fetcher import backfill_images

    stats = backfill_images(limit=args.limit)
    print(
        f"backfill-images: проверено={stats['processed']}, обновлено={stats['updated']}, "
        f"без картинки={stats['no_image']}, ошибок={stats['failed']}"
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


def cmd_cleanup_future_dates(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    n = repository.clear_future_published_dates(tolerance_days=args.tolerance_days)
    print(f"Обнулено будущих дат публикации (анонсы-события): {n}")


def cmd_seed_tags(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.seed import seed_tags_from_directions

    stats = seed_tags_from_directions()
    print(f"Seed тегов: {stats['tags']}")


def cmd_seed_scoring(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.seed import seed_default_scoring_criteria

    stats = seed_default_scoring_criteria()
    print(f"Seed критериев скоринга: {stats['criteria']}, сумма весов={stats['weight_sum']}")


def cmd_apply_source_overrides(args: argparse.Namespace) -> None:
    from oiltech_digest.ingestion.source_overrides import apply_overrides

    stats = apply_overrides()
    print(f"Оверрайды источников: изменено={stats['changed']}, без изменений={stats['unchanged']}, "
          f"не найдено={stats['not_found']}")


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


def cmd_process_full(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_full

    stats = process_full(limit=args.limit, offline=args.offline)
    print(
        f"pipeline: статей={stats['processed']}, full-text={stats['fulltext']}, "
        f"суть={stats['summary']}, релевантно={stats['relevant']}, отсев={stats['rejected']}, "
        f"теги={stats['tagged']}, скоринг={stats['scored']}, ошибок={stats['errors']}"
    )


def cmd_relevance(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_relevance

    stats = process_relevance(limit=args.limit, offline=args.offline)
    print(
        f"relevance: обработано={stats['processed']}, релевантно={stats['relevant']}, "
        f"отклонено={stats['rejected']}, ошибок={stats['errors']}"
    )


def cmd_process(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import (
        process_relevance,
        process_scores,
        process_summaries,
        process_tags,
    )

    # Порядок: суть → релевантность (отсев) → теги → скоринг.
    # tag/score автоматически пропускают нерелевантные (см. get_articles_needing_*).
    summaries = process_summaries(limit=args.limit, offline=args.offline)
    relevance = process_relevance(limit=args.limit, offline=args.offline)
    tags = process_tags(limit=args.limit, offline=args.offline)
    scores = process_scores(limit=args.limit, offline=args.offline)
    print(
        "process: "
        f"summary={summaries['processed']}/{summaries['errors']} err, "
        f"relevance={relevance['processed']} (отклонено={relevance['rejected']})/{relevance['errors']} err, "
        f"tagging={tags['processed']}/{tags['errors']} err, "
        f"scoring={scores['processed']}/{scores['errors']} err"
    )


def cmd_process_articles(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository
    from oiltech_digest.processing.pipeline import (
        make_client,
        process_relevance_articles,
        process_score_articles,
        process_summary_articles,
        process_tag_articles,
    )

    client = make_client(args.offline)
    articles = repository.get_articles_by_ids(args.article_id, include_summary=False)
    summaries = process_summary_articles(articles, client)
    articles_with_summary = repository.get_articles_by_ids(args.article_id, include_summary=True)
    relevance = process_relevance_articles(articles_with_summary, client)
    tags = process_tag_articles(articles_with_summary, client)
    scores = process_score_articles(articles_with_summary, client)
    print(
        "process-articles: "
        f"summary={summaries['processed']}/{summaries['errors']} err, "
        f"relevance={relevance['processed']} (отклонено={relevance['rejected']})/{relevance['errors']} err, "
        f"tagging={tags['processed']}/{tags['errors']} err, "
        f"scoring={scores['processed']}/{scores['errors']} err"
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


def cmd_source_health(args: argparse.Namespace) -> None:
    from collections import Counter

    from oiltech_digest.db import repository

    rows = repository.source_health_report(stale_days=args.stale_days, limit=args.limit, verdict=args.verdict)
    counts = Counter(row["verdict"] for row in rows)
    print(
        "source-health: "
        + ", ".join(f"{name}={counts.get(name, 0)}" for name in ("no_articles", "stale", "ok", "disabled"))
    )
    for row in rows:
        last = row.get("last_article_at")
        last_s = last.date().isoformat() if hasattr(last, "date") else "—"
        print(
            f"{row['id']:>4} {row['verdict']:<11} {row.get('parse_strategy') or '-':<8} "
            f"{int(row['articles'] or 0):>5} last={last_s} · {row['name']}"
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


def cmd_source_diagnose(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository
    from oiltech_digest.ingestion.source_diagnostics import diagnose_source

    source = repository.get_source(args.source_id)
    if source is None:
        raise SystemExit(f"Источник не найден: {args.source_id}")
    result = diagnose_source(source, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def cmd_parse_process(args: argparse.Namespace) -> None:
    """Стриминг-пайплайн: parse идёт в фоне, process обрабатывает новые статьи по мере их появления."""
    from oiltech_digest.db import repository
    from oiltech_digest.ingestion.rss_parser import parse_all
    from oiltech_digest.processing.pipeline import make_client, process_pipeline_articles

    logger = logging.getLogger(__name__)
    client = make_client(args.offline)

    # Checkpoint: статьи с ID строго больше этого значения считаем «новыми».
    checkpoint_id: int = repository.max_article_id() or 0
    logger.info("parse-process: checkpoint article_id=%d", checkpoint_id)

    parse_stats: dict = {}
    parse_done = threading.Event()

    def _run_parse() -> None:
        parse_stats.update(
            parse_all(
                max_age_days=args.max_age_days,
                workers=args.workers,
                source_id=getattr(args, "source_id", None),
            )
        )
        parse_done.set()

    parse_thread = threading.Thread(target=_run_parse, daemon=True)
    parse_thread.start()

    process_totals = {"processed": 0, "fulltext": 0, "summary": 0,
                      "relevant": 0, "rejected": 0, "tagged": 0, "scored": 0, "errors": 0}
    poll_interval = getattr(args, "poll_interval", 10)
    batch_limit = getattr(args, "process_limit", 20)

    while not parse_done.is_set() or True:
        new_articles = repository.get_articles_needing_summary_after(
            after_id=checkpoint_id, limit=batch_limit
        )
        if new_articles:
            logger.info("parse-process: обрабатываю %d новых статей", len(new_articles))
            batch_stats = process_pipeline_articles(new_articles, client, fetch_full=True)
            for key in process_totals:
                process_totals[key] += batch_stats.get(key, 0)
            # Двигаем checkpoint чтобы не перечитывать уже обработанные.
            checkpoint_id = max(int(a["id"]) for a in new_articles)

        if parse_done.is_set() and not new_articles:
            break
        if not parse_done.is_set():
            time.sleep(poll_interval)

    parse_thread.join(timeout=5)
    print(
        f"parse-process parse: добавлено={parse_stats.get('added', '?')}, "
        f"ошибок={parse_stats.get('errors', '?')}"
    )
    print(
        f"parse-process pipeline: статей={process_totals['processed']}, "
        f"full-text={process_totals['fulltext']}, суть={process_totals['summary']}, "
        f"релевантно={process_totals['relevant']}, отсев={process_totals['rejected']}, "
        f"теги={process_totals['tagged']}, скоринг={process_totals['scored']}, "
        f"ошибок={process_totals['errors']}"
    )


def cmd_source_retry(args: argparse.Namespace) -> None:
    """Force-parse sources with verdict stale or no_articles."""
    from oiltech_digest.db import repository
    from oiltech_digest.ingestion.rss_parser import parse_all

    verdicts = set(args.verdict) if args.verdict else {"stale", "no_articles"}
    rows = repository.source_health_report(stale_days=args.stale_days, limit=1000)
    source_ids = [r["id"] for r in rows if r["verdict"] in verdicts]
    if not source_ids:
        print("source-retry: нет источников с указанными вердиктами.")
        return
    print(f"source-retry: источников для повтора = {len(source_ids)} ({', '.join(sorted(verdicts))})")
    total = {"added": 0, "duplicates": 0, "skipped_old": 0,
             "skipped_irrelevant": 0, "sources_ok": 0, "errors": 0}
    for sid in source_ids:
        stats = parse_all(
            max_age_days=args.max_age_days,
            workers=1,
            source_id=sid,
        )
        for key in total:
            total[key] += stats.get(key, 0)
    print(
        f"source-retry итог: добавлено={total['added']}, дублей={total['duplicates']}, "
        f"источников ок={total['sources_ok']}, ошибок={total['errors']}"
    )


def cmd_digest_content(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.digest import write_digest_content

    stats = write_digest_content(
        path=args.output,
        month=args.month,
        limit=args.limit,
        min_score=args.min_score,
        html_path=args.html_output,
    )
    suffix = f", html={stats['html_path']}" if stats.get("html_path") else ""
    print(f"digest-content: файл={stats['path']}, статей={stats['items']}{suffix}")


def cmd_digest_save(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.digest import save_digest_draft

    stats = save_digest_draft(month=args.month, limit=args.limit, min_score=args.min_score)
    print(
        f"digest-save: id={stats['id']}, month={stats['month']}, "
        f"items={stats['items']}, status={stats['status']}"
    )


def cmd_jobs_worker(args: argparse.Namespace) -> None:
    from oiltech_digest import background_jobs

    background_jobs.worker_loop(
        poll_seconds=args.poll_seconds,
        once=args.once,
        stale_minutes=args.stale_minutes,
        queue_names=args.queue,
    )


def cmd_external_worker(args: argparse.Namespace) -> None:
    from oiltech_digest import external_worker

    external_worker.run_loop(
        core_api_url=args.core_api_url,
        token=args.token,
        worker_id=args.worker_id,
        queues=args.queue,
        capabilities=args.capability,
        poll_seconds=args.poll_seconds,
        once=args.once,
    )


def cmd_jobs_requeue_stale(args: argparse.Namespace) -> None:
    from oiltech_digest import config
    from oiltech_digest.db import repository

    stale_minutes = (
        config.BACKGROUND_JOB_STALE_MINUTES
        if args.stale_minutes is None
        else args.stale_minutes
    )
    requeued = repository.requeue_stale_background_jobs(stale_minutes)
    print(f"jobs-requeue-stale: requeued={requeued}, stale_minutes={stale_minutes}")


def cmd_external_queues_status(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    status = repository.external_queue_status()
    if args.json:
        print(json.dumps(status, ensure_ascii=False, default=str, indent=2))
        return
    totals = status["totals"]
    print(
        "external-queues: "
        f"queued={totals.get('queued') or 0}, "
        f"running={totals.get('running') or 0}, "
        f"failed={totals.get('failed') or 0}, "
        f"expired_leases={totals.get('expired_leases') or 0}, "
        f"oldest_queued_at={totals.get('oldest_queued_at') or '-'}, "
        f"last_heartbeat_at={totals.get('last_heartbeat_at') or '-'}"
    )
    for row in status["queues"]:
        print(
            f"  {row['queue_name']}: "
            f"queued={row.get('queued') or 0}, "
            f"running={row.get('running') or 0}, "
            f"failed={row.get('failed') or 0}, "
            f"oldest_queued_at={row.get('oldest_queued_at') or '-'}, "
            f"last_heartbeat_at={row.get('last_heartbeat_at') or '-'}"
        )


def cmd_maintenance_cleanup(args: argparse.Namespace) -> None:
    from oiltech_digest import config
    from oiltech_digest.db import repository

    background_job_days = (
        config.BACKGROUND_JOB_RETENTION_DAYS
        if args.background_job_days is None
        else args.background_job_days
    )
    export_job_days = (
        config.EXPORT_JOB_RETENTION_DAYS
        if args.export_job_days is None
        else args.export_job_days
    )
    deleted_sessions = repository.delete_expired_user_sessions()
    deleted_background_jobs = repository.cleanup_finished_background_jobs(background_job_days)
    deleted_export_jobs = repository.cleanup_finished_export_jobs(export_job_days)
    print(
        "maintenance-cleanup: "
        f"expired_sessions={deleted_sessions}, "
        f"background_jobs={deleted_background_jobs}, "
        f"background_job_days={background_job_days}, "
        f"export_jobs={deleted_export_jobs}, "
        f"export_job_days={export_job_days}"
    )


def cmd_bench_readiness(args: argparse.Namespace) -> None:
    from oiltech_digest.benchmarks import format_benchmark_report, run_readiness_benchmark

    report = run_readiness_benchmark(
        iterations=args.iterations,
        articles_limit=args.articles_limit,
        source_limit=args.source_limit,
        jobs_limit=args.jobs_limit,
        month=args.month,
        digest_limit=args.digest_limit,
        min_score=args.min_score,
        warn_ms=args.warn_ms,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_benchmark_report(report))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oiltech_digest.cli", description="OilTech Digest — сбор RSS")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог (INFO)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="создать схему БД").set_defaults(func=cmd_init_db)
    sub.add_parser("schema-check", help="проверить наличие обязательных таблиц").set_defaults(func=cmd_schema_check)
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

    p_full = sub.add_parser("fetch-full-text", help="дозагрузить полный текст статей по URL")
    p_full.add_argument("--limit", type=int, default=50)
    p_full.add_argument("--min-chars", type=int, default=800)
    p_full.add_argument("--retry-too-short", action="store_true",
                        help="повторить попытку для статей с full_text_status='too_short'")
    p_full.set_defaults(func=cmd_fetch_full_text)

    p_backfill_images = sub.add_parser("backfill-images", help="дозаполнить og:image у статей без картинки")
    p_backfill_images.add_argument("--limit", type=int, default=200)
    p_backfill_images.set_defaults(func=cmd_backfill_images)

    sub.add_parser("stats", help="диагностика БД").set_defaults(func=cmd_stats)

    p_cf = sub.add_parser("cleanup-future-dates",
                          help="обнулить published_at из будущего (анонсы-события календаря)")
    p_cf.add_argument("--tolerance-days", type=int, default=2,
                      help="сколько дней вперёд считать допустимыми")
    p_cf.set_defaults(func=cmd_cleanup_future_dates)

    sub.add_parser("seed-tags", help="загрузить теги из направлений D01-D18").set_defaults(func=cmd_seed_tags)
    sub.add_parser("seed-scoring", help="создать базовые критерии скоринга").set_defaults(func=cmd_seed_scoring)
    sub.add_parser("apply-source-overrides", help="применить playwright/listing-оверрайды источников").set_defaults(func=cmd_apply_source_overrides)

    def add_ai_args(p):
        p.add_argument("--limit", type=int, default=20)
        p.add_argument("--offline", action="store_true", help="детерминированная локальная заглушка без OpenAI API")

    p_summary = sub.add_parser("summarize", help="сформировать AI-суть статей")
    add_ai_args(p_summary)
    p_summary.set_defaults(func=cmd_summarize)

    p_relevance = sub.add_parser("relevance", help="AI-фильтр релевантности (отсев нерелевантных)")
    add_ai_args(p_relevance)
    p_relevance.set_defaults(func=cmd_relevance)

    p_tag = sub.add_parser("tag", help="присвоить статьи тегам")
    add_ai_args(p_tag)
    p_tag.set_defaults(func=cmd_tag)

    p_score = sub.add_parser("score", help="рассчитать скоринг статей")
    add_ai_args(p_score)
    p_score.set_defaults(func=cmd_score)

    p_process = sub.add_parser("process", help="summary → tagging → scoring")
    add_ai_args(p_process)
    p_process.set_defaults(func=cmd_process)

    p_process_full = sub.add_parser("process-full", help="по-статейный конвейер: full-text→суть→релевантность→тег→скоринг")
    add_ai_args(p_process_full)
    p_process_full.set_defaults(func=cmd_process_full)

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

    p_source_health = sub.add_parser("source-health", help="вердикты покрытия источников: ok/stale/no_articles/disabled")
    p_source_health.add_argument("--stale-days", type=int, default=3)
    p_source_health.add_argument("--limit", type=int, default=300)
    p_source_health.add_argument("--verdict", choices=["ok", "stale", "no_articles", "disabled"], default=None)
    p_source_health.set_defaults(func=cmd_source_health)

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

    p_source_diag = sub.add_parser("source-diagnose", help="read-only диагностика источника по source_id")
    p_source_diag.add_argument("source_id", type=int)
    p_source_diag.add_argument("--limit", type=int, default=5, help="сколько кандидатов/постов проверить")
    p_source_diag.set_defaults(func=cmd_source_diagnose)

    p_digest = sub.add_parser("digest-content", help="собрать digest_content.json из обработанных статей")
    p_digest.add_argument("month", help="YYYY-MM")
    p_digest.add_argument("--output", default="digest_content.generated.json")
    p_digest.add_argument("--html-output", default=None, help="дополнительно собрать email-ready HTML")
    p_digest.add_argument("--limit", type=int, default=20)
    p_digest.add_argument("--min-score", type=float, default=60)
    p_digest.set_defaults(func=cmd_digest_content)

    p_digest_save = sub.add_parser("digest-save", help="сохранить draft monthly_digest из текущих digest-кандидатов")
    p_digest_save.add_argument("month", help="YYYY-MM")
    p_digest_save.add_argument("--limit", type=int, default=20)
    p_digest_save.add_argument("--min-score", type=float, default=60)
    p_digest_save.set_defaults(func=cmd_digest_save)

    p_jobs_worker = sub.add_parser("jobs-worker", help="обрабатывать background_jobs из БД")
    p_jobs_worker.add_argument("--poll-seconds", type=float, default=None)
    p_jobs_worker.add_argument("--stale-minutes", type=int, default=None)
    p_jobs_worker.add_argument("--queue", action="append", default=None,
                               help="очередь для обработки; можно указать несколько раз")
    p_jobs_worker.add_argument("--once", action="store_true", help="забрать одну доступную пачку и выйти, если задач нет")
    p_jobs_worker.set_defaults(func=cmd_jobs_worker)

    p_external_worker = sub.add_parser("external-worker", help="обрабатывать external-* задачи через HTTP API core")
    p_external_worker.add_argument("--core-api-url", default=None)
    p_external_worker.add_argument("--token", default=None)
    p_external_worker.add_argument("--worker-id", default=None)
    p_external_worker.add_argument("--queue", action="append", default=None)
    p_external_worker.add_argument("--capability", action="append", default=None)
    p_external_worker.add_argument("--poll-seconds", type=float, default=None)
    p_external_worker.add_argument("--once", action="store_true")
    p_external_worker.set_defaults(func=cmd_external_worker)

    p_jobs_requeue = sub.add_parser("jobs-requeue-stale", help="вернуть зависшие running-задачи обратно в queued")
    p_jobs_requeue.add_argument("--stale-minutes", type=int, default=None)
    p_jobs_requeue.set_defaults(func=cmd_jobs_requeue_stale)

    p_external_status = sub.add_parser("external-queues-status", help="показать состояние external-* очередей")
    p_external_status.add_argument("--json", action="store_true")
    p_external_status.set_defaults(func=cmd_external_queues_status)

    p_maintenance_cleanup = sub.add_parser(
        "maintenance-cleanup",
        help="удалить истекшие сессии и старые terminal-записи служебных таблиц",
    )
    p_maintenance_cleanup.add_argument("--background-job-days", type=int, default=None)
    p_maintenance_cleanup.add_argument("--export-job-days", type=int, default=None)
    p_maintenance_cleanup.set_defaults(func=cmd_maintenance_cleanup)

    p_bench = sub.add_parser(
        "bench-readiness",
        help="read-only benchmark основных prod-запросов без парсинга и AI",
    )
    p_bench.add_argument("--iterations", type=int, default=5)
    p_bench.add_argument("--articles-limit", type=int, default=1000)
    p_bench.add_argument("--source-limit", type=int, default=300)
    p_bench.add_argument("--jobs-limit", type=int, default=100)
    p_bench.add_argument("--month", default=None, help="YYYY-MM для digest_candidates; пусто = все")
    p_bench.add_argument("--digest-limit", type=int, default=100)
    p_bench.add_argument("--min-score", type=float, default=0)
    p_bench.add_argument("--warn-ms", type=float, default=800)
    p_bench.add_argument("--json", action="store_true", help="вывести машинно-читаемый JSON")
    p_bench.set_defaults(func=cmd_bench_readiness)

    p_source_retry = sub.add_parser(
        "source-retry",
        help="форс-парсинг источников с вердиктом stale/no_articles",
    )
    p_source_retry.add_argument(
        "--verdict", nargs="+", choices=["stale", "no_articles"],
        default=None, help="вердикты для обработки (по умолчанию: stale no_articles)",
    )
    p_source_retry.add_argument("--stale-days", type=int, default=3)
    p_source_retry.add_argument("--max-age-days", type=int, default=None)
    p_source_retry.set_defaults(func=cmd_source_retry)

    p_pp = sub.add_parser(
        "parse-process",
        help="стриминг-пайплайн: parse + AI-обработка новых статей параллельно",
    )
    p_pp.add_argument("--max-age-days", type=int, default=None)
    p_pp.add_argument("--source-id", type=int, default=None)
    p_pp.add_argument("--workers", type=int, default=5, help="воркеры парсинга (осторожно с RAM)")
    p_pp.add_argument("--process-limit", type=int, default=20, help="статей за один батч обработки")
    p_pp.add_argument("--poll-interval", type=int, default=10, help="секунд между опросами новых статей")
    p_pp.add_argument("--offline", action="store_true")
    p_pp.set_defaults(func=cmd_parse_process)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
