"""CLI: init-db / seed-sources / discover-rss / parse / stats.

Запуск: python -m oiltech_digest.cli <command> [options]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def _setup_logging(verbose: bool) -> None:
    from oiltech_digest.logging_utils import setup_logging

    setup_logging("cli", verbose=verbose, force=True)


def cmd_init_db(args: argparse.Namespace) -> None:
    from oiltech_digest.db import connection, repository

    tables = connection.init_db()
    print(f"БД инициализирована. Таблиц в схеме: {len(tables)}")
    for t in tables:
        print(f"  - {t}")
    # Если админов нет, а пользователи есть — назначить админом первого (бутстрап #12).
    admin_id = repository.ensure_admin_bootstrap()
    if admin_id is not None:
        print(f"Бутстрап ролей: пользователь id={admin_id} назначен администратором")


def cmd_create_user(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    role = "admin" if args.admin else "user"
    user = repository.create_user(args.email, args.password, role)
    print(f"Создан пользователь id={user['id']} {user['email']} роль={user['role']}")


def cmd_set_role(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    target = next((u for u in repository.list_users() if u["email"].lower() == args.email.strip().lower()), None)
    if target is None:
        print(f"Пользователь {args.email} не найден")
        return
    repository.set_user_role(int(target["id"]), args.role)
    print(f"Роль пользователя {args.email} → {args.role}")


def cmd_list_users(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    for u in repository.list_users():
        print(f"  id={u['id']:>3}  {u['role']:<6}  {u['email']}")


def cmd_admission_code(args: argparse.Namespace) -> None:
    """Запасной путь допуска (#113): новый код учётке — при сбое отправки в Telegram или до
    включения отправки. Заменяет прежний код, интервал в 10 минут не действует."""
    from oiltech_digest import admission, auth
    from oiltech_digest.db import repository

    if args.user_id is not None:
        target = repository.get_user_by_id(args.user_id)
    else:
        email = auth.normalize_email(args.email)
        target = next((u for u in repository.list_users() if u["email"] == email), None)
    if target is None:
        print("Учётка не найдена")
        raise SystemExit(1)
    code, expires_at = admission.issue_code(int(target["id"]), force=True)
    print(admission.notify_text(int(target["id"]), target["email"], code, expires_at))


def _utc_period_from_days(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    return end - timedelta(days=days), end


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
    from oiltech_digest.processing import fulltext_recompute

    stats = fetch_full_text(
        limit=args.limit,
        min_chars=args.min_chars,
        retry_too_short=args.retry_too_short,
    )
    # Суть, посчитанная по обрывку до дозагрузки, идёт в пересчёт один раз, если тело
    # дало модели заметно больше текста (как перерасчёт у repair-article-bodies).
    recompute = fulltext_recompute.after_bodies(stats.pop("replaced", []))
    print(
        f"fetch-full-text: проверено={stats['processed']}, обновлено={stats['updated']}, "
        f"слишком коротких={stats['too_short']}, "
        # Отдельно от «слишком коротких»: текст у статьи ЕСТЬ и он не хуже нового,
        # перезапись просто не нужна. Под общим именем это читалось как поломка.
        f"без прироста={stats.get('no_gain', 0)}, "
        # Отдельной строкой: это не сбой, а сработавшая защита от подмены текста (№24).
        # Без своего счётчика она была невидима и терялась в общей арифметике.
        f"отклонено стражем={stats.get('mismatch', 0)}, ошибок={stats['failed']}, "
        f"пересчёт ИИ={recompute}"
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
    # 13.09: перешли с 18 направлений D01–D18 из xlsx на 13 тематик заказчика
    # (data/seed/tags_13_tematik.md). Прежние теги выключаются, а не удаляются:
    # на них ссылается article_tags со всей историей классификации.
    from oiltech_digest.processing.seed import seed_tags_13

    stats = seed_tags_13()
    print(f"Seed тегов: заведено {stats['tags']}, выключено прежних {stats['disabled']}")


def cmd_seed_scoring(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.seed import seed_default_scoring_criteria

    stats = seed_default_scoring_criteria()
    for profile, row in stats.items():
        if row["added"]:
            print(f"Seed критериев скоринга, {profile}: заведено {row['added']}, сумма весов={row['weight_sum']}")
        else:
            print(f"Seed критериев скоринга, {profile}: не тронут — в профиле уже есть критерии")


def cmd_apply_scoring_preset(args: argparse.Namespace) -> None:
    """Поставить именованный набор критериев активным набором профиля (решение владельца 29.09).

    Сид непустой профиль не трогает — на проде набор меняется только этой командой. По умолчанию
    сухой прогон: «до» и «после»; --apply — одной транзакцией, критерии не удаляются."""
    from oiltech_digest.db import repository
    from oiltech_digest.scoring_profiles import SCORING_PRESETS

    try:
        result = repository.apply_scoring_preset(args.profile, SCORING_PRESETS[args.preset][args.profile],
                                                 apply=args.apply)
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(f"apply-scoring-preset: {exc}") from exc

    def listed(pairs: list[tuple[str, float]]) -> str:
        return "; ".join(f"{name} {weight:g}" for name, weight in pairs) or "—"

    def total(pairs: list[tuple[str, float]]) -> str:
        return f"{sum(weight for _, weight in pairs):g}"

    mode = "запись" if args.apply else "сухой прогон (записать: --apply)"
    print(f"apply-scoring-preset: профиль {result['profile']}, набор {args.preset} — {mode}")
    print(f"  до (активны, сумма {total(result['before'])}): {listed(result['before'])}")
    print(f"  после (сумма {total(result['after'])}): {listed(result['after'])}")
    if not result["changed"]:
        print("  изменений нет — набор уже применён")
        return
    # Прежний набор после записи выключен, не удалён: вернуть его — один UPDATE (ранбук ADR 0002).
    ids = ", ".join(str(item) for item in result["before_ids"]) or "—"
    print(f"  id прежнего набора: {ids}; откат после --apply: UPDATE scoring_criteria SET enabled = id IN "
          f"({ids}), updated_at = now() WHERE profile = '{result['profile']}';")
    plan = result["plan"]
    for key, label in (("add", "заведутся"), ("enable", "включатся"), ("update", "изменятся вес, описание или порядок"),
                       ("disable", "выключатся (не удаляются: подпункты старых баллов остаются целыми)")):
        print(f"  {label}: {', '.join(plan[key]) or '—'}")
    if result["applied"]:
        # Балл статьи снят 08.11 (баллы ставит второй вызов «Карточки» у сигнала): прежние баллы не пересчитываются.
        print("  применено. Баллы, посчитанные прежним набором, не пересчитаны: новый набор возьмут новые баллы "
              "сигналов")


def cmd_apply_source_overrides(args: argparse.Namespace) -> None:
    from oiltech_digest.ingestion.source_overrides import apply_overrides

    stats = apply_overrides()
    print(f"Оверрайды источников: изменено={stats['changed']}, без изменений={stats['unchanged']}, "
          f"не найдено={stats['not_found']}, неоднозначно={stats['ambiguous']}")
    # Промах по имени = «починил, а источник всё так же молчит». В bootstrap вызов обёрнут
    # в `|| true`, поэтому кода возврата никто не увидит — единственный сигнал деплою это
    # напечатанное ИМЯ, а не счётчик, который легко прочитать как 0.
    problems = (
        [(name, "не найден в БД (проверьте sources.name)") for name in stats["missing_names"]]
        + [(name, "несколько строк с этим именем (добавьте source_type в запись реестра)")
           for name in stats["ambiguous_names"]]
    )
    for name, reason in problems:
        print(f"  ВНИМАНИЕ: оверрайд НЕ применён — {name}: {reason}")


def cmd_summarize(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_summaries

    stats = process_summaries(limit=args.limit, offline=args.offline)
    print(f"summary: обработано={stats['processed']}, ошибок={stats['errors']}")


def cmd_translate(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_translations

    stats = process_translations(limit=args.limit, offline=args.offline)
    print(
        f"translate-titles: обработано={stats['processed']}, переведено AI={stats['ai']}, "
        f"ошибок={stats['errors']}"
    )


def _terminology_audit_rows(limit: int, article_id: int | None = None) -> list[dict]:
    from oiltech_digest.db import repository
    from oiltech_digest.processing.domain_glossary import terminology_warnings

    rows = []
    for article in repository.list_article_texts_for_terminology_audit(limit=limit, article_id=article_id):
        for field in ("title_ru", "summary"):
            value = article.get(field) or ""
            warnings = terminology_warnings(value, article)
            if warnings:
                rows.append({
                    "article_id": int(article["id"]),
                    "field": field,
                    "title": article.get("title"),
                    "source": article.get("source_name"),
                    "warnings": warnings,
                    "text": value,
                })
    return rows


def cmd_audit_terminology(args: argparse.Namespace) -> None:
    rows = _terminology_audit_rows(args.limit, article_id=args.article_id)
    if args.json:
        print(json.dumps({"issues": len(rows), "rows": rows}, ensure_ascii=False, default=str))
        return
    print(f"terminology-audit: проблемных полей={len(rows)}")
    for row in rows[: args.show]:
        warning = row["warnings"][0]
        print(
            f"  article={row['article_id']} field={row['field']} source={row.get('source') or '—'} "
            f"bad={warning['forbidden_ru']} -> {warning['preferred_ru']}"
        )
        print(f"    {row['text'][:220]}")


def cmd_repair_terminology(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository
    from oiltech_digest.processing.domain_glossary import enforce_glossary_text

    if getattr(args, "scripts_only", False):
        # Только алфавит буквы и пробел на стыке — безопасно по всему корпусу. Полный
        # словарь по старым карточкам без ревью не гоняем: его замены ломали падеж.
        from oiltech_digest.processing import mixed_script

        report = mixed_script.repair_cards(apply=not args.dry_run, article_ids=[args.article_id] if args.article_id else None)
        if args.json:
            # Полный список: по нему откатывают (ревью 27.09), --show — только для экрана.
            print(json.dumps(report, ensure_ascii=False, default=str))
            return
        suffix = " [dry-run]" if args.dry_run else ""
        print(f"terminology-repair --scripts-only{suffix}: полей к исправлению={report['changed_fields']}")
        for item in report["changes"][: args.show]:
            print(f"  article={item['article_id']} field={item['field']}")
            print(f"    before: {str(item['before'])[:180]}")
            print(f"    after:  {str(item['after'])[:180]}")
        return

    scanned = changed = 0
    changes = []
    for article in repository.list_article_texts_for_terminology_audit(limit=args.limit, article_id=args.article_id):
        scanned += 1
        updates: dict[str, str] = {}
        for field in ("title_ru", "summary"):
            before = article.get(field)
            if not before:
                continue
            after = enforce_glossary_text(str(before), article)
            if after != before:
                updates[field] = after
                changes.append({
                    "article_id": int(article["id"]),
                    "field": field,
                    "before": before,
                    "after": after,
                })
        if updates:
            changed += len(updates)
            if not args.dry_run:
                repository.update_article_terminology_texts(
                    int(article["id"]),
                    summary=updates.get("summary"),
                    title_ru=updates.get("title_ru"),
                )
    if args.json:
        print(json.dumps({"dry_run": args.dry_run, "scanned": scanned, "changed_fields": changed, "changes": changes}, ensure_ascii=False, default=str))
        return
    suffix = " [dry-run]" if args.dry_run else ""
    print(f"terminology-repair{suffix}: статей проверено={scanned}, полей к исправлению={changed}")
    for item in changes[: args.show]:
        print(f"  article={item['article_id']} field={item['field']}")
        print(f"    before: {str(item['before'])[:180]}")
        print(f"    after:  {str(item['after'])[:180]}")


def cmd_validate_terminology(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.domain_glossary import (
        GLOSSARY,
        PHRASE_REPAIRS,
        glossary_golden_cases,
        validate_glossary,
    )

    errors = validate_glossary()
    payload = {
        "ok": not errors,
        "terms": len(GLOSSARY),
        "phrase_repairs": len(PHRASE_REPAIRS),
        "golden_cases": len(glossary_golden_cases()),
        "errors": errors,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return
    print(
        f"terminology-validate: ok={payload['ok']} terms={payload['terms']} "
        f"phrase_repairs={payload['phrase_repairs']} golden_cases={payload['golden_cases']}"
    )
    for error in errors:
        print(f"  - {error}")
    if errors:
        raise SystemExit(1)


def cmd_eval_terminology(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.domain_glossary import run_terminology_eval

    report = run_terminology_eval(limit=args.limit)
    rows = report["rows"]
    if args.csv_path:
        csv_path = Path(args.csv_path)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "number",
                    "source",
                    "case",
                    "original_en",
                    "before",
                    "after",
                    "must_have",
                    "must_not",
                    "status",
                    "issues",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)
    if args.markdown_path:
        markdown_path = Path(args.markdown_path)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        sample_rows = rows[: min(args.show, len(rows))]
        table = [
            "| # | Кейс | Оригинал EN | Было плохо | Стало хорошо | Статус |",
            "|---:|---|---|---|---|---|",
        ]
        for row in sample_rows:
            table.append(
                "| {number} | {case} | {original_en} | {before} | {after} | {status} |".format(
                    **{key: _markdown_cell(value) for key, value in row.items()}
                )
            )
        markdown_path.write_text(
            "\n".join(
                [
                    "# Отчет по нефтегазовой терминологии",
                    "",
                    "## Итог",
                    "",
                    f"- Проверено примеров: {report['total']}",
                    f"- Успешно: {report['passed']}",
                    f"- Ошибок: {report['failed']}",
                    "",
                    "## Что сделано",
                    "",
                    "- Нефтегазовый словарь вынесен в `oiltech_digest/processing/domain_glossary.json`.",
                    "- Summary и перевод заголовков получают компактный блок релевантных терминов в prompt.",
                    "- После ответа модели включен детерминированный слой исправления плохих терминов.",
                    "- Сборка дайджеста дополнительно нормализует старые summary/title перед PDF/DOCX/HTML.",
                    "- Добавлены команды `validate-terminology`, `audit-terminology`, `repair-terminology`, `eval-terminology`.",
                    "- Добавлены golden-кейсы и автоматическая проверка словаря в тестах.",
                    "",
                    "## Примеры",
                    "",
                    *table,
                    "",
                ]
            ),
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, default=str))
        return
    print(
        f"terminology-eval: total={report['total']} passed={report['passed']} failed={report['failed']}"
    )
    if args.csv_path:
        print(f"csv: {args.csv_path}")
    if args.markdown_path:
        print(f"markdown: {args.markdown_path}")
    if report["failed"]:
        raise SystemExit(1)


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def cmd_retire_tags(args: argparse.Namespace) -> None:
    """Разовая миграция таксономии: выключить теги, которых нет в списке заказчика."""
    from oiltech_digest.processing.seed import retire_old_tags

    stats = retire_old_tags()
    print(f"retire-tags: выключено прежних тегов={stats['disabled']}")


def cmd_process_full(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_full

    stats = process_full(limit=args.limit, offline=args.offline)
    print(
        f"pipeline: статей={stats['processed']}, full-text={stats['fulltext']}, "
        f"суть={stats['summary']}, перевод={stats['translated']}, отклонено={stats['rejected']}, "
        f"ошибок={stats['errors']}"
    )


def cmd_process(args: argparse.Namespace) -> None:
    from oiltech_digest.processing.pipeline import process_full

    # Конвейер ленты: full-text → summary → translate у статей, принятых «Отбором» (до него гейтом). Гейт, тег и
    # балл статьи сняты 08.11: решает «Отбор», тему и баллы ставит «Карточка».
    stats = process_full(limit=args.limit, offline=args.offline)
    print(
        "process: "
        f"статей={stats['processed']}, full-text={stats['fulltext']}, отклонено={stats['rejected']}, "
        f"summary={stats['summary']}, translate={stats['translated']}, "
        f"errors={stats['errors']}"
    )


def cmd_process_articles(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository
    from oiltech_digest.processing.pipeline import (
        make_client,
        process_pipeline_articles,
    )

    client = make_client(args.offline)
    articles = repository.get_articles_by_ids(args.article_id, include_summary=True)
    stats = process_pipeline_articles(articles, client, fetch_full=True)
    print(
        "process-articles: "
        f"статей={stats['processed']}, full-text={stats['fulltext']}, отклонено={stats['rejected']}, "
        f"summary={stats['summary']}, translate={stats['translated']}, "
        f"errors={stats['errors']}"
    )


def cmd_enqueue_process(args: argparse.Namespace) -> None:
    """Поставить AI-обработку в очередь, НЕ выполняя её локально.

    route_ai_processing() решает куда: при AI_EXECUTION_REGION=external и включённом
    внешнем контуре — в очередь external-ai (её разбирает зарубежный worker), иначе —
    в локальную очередь ai. Пустой article_ids → worker берёт следующие N статей без
    суммы (выбор делается на стороне core в момент claim). Используется scheduler'ом,
    чтобы РФ-core не звал OpenAI напрямую.
    """
    from oiltech_digest import network_policy
    from oiltech_digest.db import repository

    decision = network_policy.route_ai_processing()
    payload = {"limit": int(args.limit), "offline": bool(args.offline)}
    job = repository.create_background_job(
        "process_articles",
        payload,
        queue_name=decision.queue_name,
        execution_region=decision.execution_region,
        capability=decision.capability,
    )
    print(
        f"enqueue-process: job id={job['id']} queue={decision.queue_name} "
        f"region={decision.execution_region} limit={args.limit} ({decision.reason})"
    )


def cmd_recheck_marked(args: argparse.Namespace) -> None:
    """Показать помеченные на удаление (pending_deletion): счётчик + выборка с причинами.
    Ничего не удаляет: только показывает, что накопилось на удаление. Перепроверку гейтом, которая
    помечала (recheck --mark), сняли 08.11; пометки остались от неё и от склейки ключей адреса 13.09
    (их возвращает repair-url-keys)."""
    from oiltech_digest.db import repository

    total = repository.count_pending_deletion()
    print(f"помечено на удаление: {total}")
    if not total:
        return
    print(f"--- первые {args.limit} (заголовок · источник · причина) ---")
    for r in repository.list_pending_deletion(args.limit):
        title = (r.get("title") or "")[:90]
        print(f"  [{r.get('source_name') or '—'}] {title}\n      причина: {r.get('deletion_reason') or '—'}")


def cmd_recheck_purge(args: argparse.Namespace) -> None:
    """РАЗОВО физически удалить все помеченные (pending_deletion) статьи. Необратимо.
    Статьи из выпусков (сохранённый черновик или отметка «в дайджест») пропускаются (без --force)."""
    from oiltech_digest.db import repository

    total = repository.count_pending_deletion()
    if total == 0:
        print("recheck-purge: помеченных нет — нечего удалять")
        return
    if not args.yes:
        raise SystemExit(f"recheck-purge: будет физически удалено {total} статей. "
                         "Перепроверь `recheck-marked`, затем повтори с флагом --yes.")
    deleted = repository.purge_pending_deletion(force=bool(args.force))
    print(f"recheck-purge: физически удалено={deleted} из помеченных={total} "
          f"(пропущены в дайджесте={total - deleted}, force={bool(args.force)})")


def cmd_recheck_unmark(args: argparse.Namespace) -> None:
    """Снять пометку «на удаление» со ВСЕХ статей (вернуть в ленту)."""
    from oiltech_digest.db import repository

    n = repository.unmark_all_pending_deletion()
    print(f"recheck-unmark: возвращено в строй={n}")


def cmd_enqueue_translate(args: argparse.Namespace) -> None:
    """Поставить в очередь бэкфилл перевода заголовков (статьи без title_ru) батчами."""
    from oiltech_digest import network_policy
    from oiltech_digest.db import repository

    decision = network_policy.route_ai_bulk()  # пересчёт — своя полоса (lanes.py)
    ids = repository.article_ids_needing_title_ru()
    if args.limit:
        ids = ids[: args.limit]
    batch = max(1, args.batch_size)
    chunks = [ids[i : i + batch] for i in range(0, len(ids), batch)]
    job_ids = []
    for chunk in chunks:
        job = repository.create_background_job(
            "translate_titles",
            {"article_ids": chunk},
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        job_ids.append(job["id"])
    print(
        f"enqueue-translate: без перевода={len(ids)}, задач={len(job_ids)}, батч={batch}, "
        f"queue={decision.queue_name} region={decision.execution_region} ({decision.reason})"
    )


def cmd_enqueue_resummarize(args: argparse.Namespace) -> None:
    """Перегенерировать суть и перевод заголовка у статей со словами из двух алфавитов.

    Двойники и склейку чинит `repair-terminology --scripts-only` без ИИ; здесь — полуперевод
    («управляego», «наshore»), который лечит только новый ответ модели. Ядро запишет только
    суть и перевод (прежние вердикт гейта, тег и балл статьи не трогаются). По умолчанию только выборка.
    """
    from oiltech_digest.processing import mixed_script

    selection = mixed_script.resummarize_selection(args.article_id)
    summary_ids, title_ids = selection["summary"], selection["title"]
    if args.limit:
        summary_ids, title_ids = summary_ids[: args.limit], title_ids[: args.limit]
    print(
        f"enqueue-resummarize: суть — {len(summary_ids)} статей, только заголовок — {len(title_ids)}; "
        f"брак в самом исходном заголовке (переводом не лечится) — {len(selection['source_title'])}"
    )
    if args.dry_run:
        print(f"  [dry-run] суть: {summary_ids[:50]}")
        print(f"  [dry-run] заголовок: {title_ids[:50]}")
        return
    try:
        jobs = mixed_script.enqueue_resummarize(summary_ids, title_ids, batch_size=args.batch_size)
    except RuntimeError as exc:
        raise SystemExit(f"enqueue-resummarize: {exc}") from exc
    print(f"  задач: {len(jobs)} ({jobs})")


def cmd_enqueue_title_edit(args: argparse.Namespace) -> None:
    """Правка русских заголовков, написанных до правки (решение владельца 03.11): принятые и отмеченные «в дайджест»
    статьи последних двух недель в открытых месяцах, заголовок копией не по правилам эксперта. По умолчанию — сухой
    прогон с N и ценой; задачи — только с --no-dry-run, после пересборки NL."""
    from oiltech_digest.processing import rescore, title_edit

    selection = title_edit.selection()
    ids = selection["article_ids"][: args.limit] if args.limit else selection["article_ids"]
    estimate = rescore.stage_cost_estimate("translation", len(ids))
    lane = rescore.rescore_lane()
    in_digest = sum(1 for article_id in ids if article_id in selection["digest_ids"])
    print(f"enqueue-title-edit: собраны с {selection['since']:%d.%m.%Y}, открытые месяцы {', '.join(selection['months'])}"
          f"; русских заголовков копией не по правилам: N={len(ids)} (из них в дайджест: {in_digest})")
    if estimate["model"] is None:
        print(f"  цену не оценить: вызовов translation за {estimate['days']} дней нет")
    else:
        print(f"  цена ≈ ${estimate['usd_total']:.2f}: модель {estimate['model']}, вызовов translation за "
              f"{estimate['days']} дней: {estimate['runs']}, в среднем in={estimate['avg_input_tokens']} / "
              f"out={estimate['avg_output_tokens']} токенов, ставки ${estimate['price_in']}/${estimate['price_out']} "
              f"за 1М → ${estimate['usd_per_call']:.5f} за вызов; сверять со счётом провайдера")
    workers = ", ".join(f"{name} (задачу просил {minutes} мин назад)" for name, minutes in lane["workers"]) or "нет"
    print(f"  полоса: {lane['queue']} ({lane['region']}); воркеры полосы: {workers}")
    for warning in lane["warnings"]:
        print(f"  ВНИМАНИЕ: {warning}")
    if args.dry_run:
        print("  [dry-run] задачи не поставлены (поставить: --no-dry-run)")
        return
    try:
        jobs = rescore.enqueue_stage(ids, title_edit.STAGES, what="правка заголовков", batch_size=args.batch_size,
                                     allow_live_lane=args.allow_live_lane)
    except RuntimeError as exc:
        raise SystemExit(f"enqueue-title-edit: {exc}") from exc
    print(f"  задач: {len(jobs)} ({jobs[:20]}{' …' if len(jobs) > 20 else ''})")


def _utc_datetime(value: str) -> datetime:
    """ISO-время из командной строки; без пояса — UTC (пояс сессии базы тут ни при чём)."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def cmd_repair_telegram_titles(args: argparse.Namespace) -> None:
    """Склеенные заголовки Telegram (до 25.09 парсер терял переносы строк) — без сети."""
    from oiltech_digest.ingestion import telegram_titles

    if not args.dry_run and not args.before:
        raise SystemExit("repair-telegram-titles: для записи укажите --before — время выката исправленного парсера")
    report = telegram_titles.repair(apply=not args.dry_run, collected_before=args.before)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, default=str))
        return
    suffix = " [dry-run]" if args.dry_run else ""
    print(f"repair-telegram-titles{suffix}: статей Telegram={report['scanned']}, заголовков к правке={report['changed']}")
    for change in report["changes"][: args.show]:
        print(f"  article={change['article_id']}")
        print(f"    before: {change['before'][:160]}")
        print(f"    after:  {change['after']}")


def cmd_reprints(args: argparse.Namespace) -> None:
    """Показать помеченные перепечатки или снять пометку.

    Снятие обязательно: решение принимает модель, и у человека должен быть способ
    её отменить — иначе ошибка ИИ необратима так же, как удаление."""
    from oiltech_digest.db import repository

    if args.unmark:
        ok = repository.unmark_article_reprint(args.unmark)
        print(f"пометка со статьи {args.unmark}: {'снята, статья вернулась в ленту' if ok else 'не найдена'}")
        return
    rows = repository.list_article_reprints(limit=args.limit)
    stats = repository.reprint_stats()
    print(f"перепечаток помечено: {stats['total']} в {stats['groups']} группах")
    for r in rows:
        sim = f"{float(r['similarity']):.0%}" if r.get("similarity") is not None else "—"
        print(f"  {r['article_id']} ← дубль {r['primary_id']} ({sim}, {r['decided_by']})")
        print(f"    копия:   {str(r['duplicate_source'])[:18]} · {str(r['duplicate_title'])[:58]}")
        print(f"    главная: {str(r['primary_source'])[:18]} · {str(r['primary_title'])[:58]}")
        if r.get("reason"):
            print(f"    почему:  {str(r['reason'])[:90]}")


def cmd_find_reprints(args: argparse.Namespace) -> None:
    """Найти перепечатки: правило даёт кандидатов, модель решает (№21).

    По умолчанию СУХОЙ прогон — ничего не помечается. Так задумано: схлопывание
    убирает материал из ленты, а заказчик уже жаловался на исчезновение статей.
    Сначала он смотрит список, потом --apply."""
    from oiltech_digest.db import repository
    from oiltech_digest.processing import reprints
    from oiltech_digest.processing.pipeline import make_client

    if not 0 < args.min_overlap <= 1:
        raise SystemExit("--min-overlap задаётся долей от 0 до 1 (напр. 0.35)")
    if not 0 < args.max_overlap <= 1 or args.max_overlap < args.min_overlap:
        raise SystemExit("--max-overlap задаётся долей от 0 до 1 и не меньше --min-overlap")
    if args.days < 1 or args.limit < 1 or args.max_days_apart < 0:
        raise SystemExit("--days и --limit должны быть положительными, --max-days-apart неотрицательным")
    # Для планировщика: срок — от прошлого прогона с записью в базе, а не от счётчика
    # циклов, который обнуляется при каждом перезапуске (19.09: 16,5 ч без прогона).
    min_interval = getattr(args, "min_interval_hours", 0) or 0
    if args.apply and min_interval > 0 and repository.has_recent_background_job(
        kind="reprint_review",
        payload_subset={"dry_run": False},
        lookback_hours=min_interval,
        statuses=("queued", "running", "finalizing", "ok", "failed"),
    ):
        print(f"find-reprints: пропуск — прогон с записью был менее {min_interval:g} ч назад")
        return

    candidates = reprints.find_candidates(
        days=args.days, min_overlap=args.min_overlap, max_overlap=args.max_overlap,
        max_days_apart=args.max_days_apart, limit=args.limit,
    )
    band = f"{args.min_overlap:.0%}" if args.max_overlap >= 1 else f"{args.min_overlap:.0%}–{args.max_overlap:.0%}"
    print(f"кандидатов по правилу: {len(candidates)} "
          f"(окно {args.days} дн., полоса {band}, разрыв ≤{args.max_days_apart} дн.)")
    if not candidates or args.candidates_only:
        for c in candidates[:args.show]:
            print(f"  {c['overlap']:.0%}  {str(c['a_title'])[:52]} || {str(c['b_title'])[:52]}")
        return

    # ЛЮБОЙ прогон с моделью идёт через контур, а не только --apply: сухой прогон
    # тоже зовёт OpenAI, и с РФ-адреса он ловит 403 по географии. Именно на этом
    # первый прогон 17.09 дал 12 ошибок из 12 — а сухой прогон здесь умолчание,
    # то есть сломан был основной путь.
    if not args.local:
        # Судья зовёт OpenAI, а с РФ-адреса OpenAI отвечает 403 по географии —
        # первый прогон 17.09 дал 12 ошибок из 12. Поэтому запись идёт через тот же
        # внешний контур, что и остальные ИИ-стадии, а не прямым вызовом.
        from oiltech_digest import network_policy

        decision = network_policy.route_ai_processing()
        pairs = [{"a_id": int(c["a_id"]), "b_id": int(c["b_id"]), "overlap": c.get("overlap")}
                 for c in candidates]
        job = repository.create_background_job(
            "reprint_review", {"pairs": pairs, "dry_run": not args.apply},
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        mode = "СУХОЙ ПРОГОН (ничего не помечается)" if not args.apply else "С ЗАПИСЬЮ пометок"
        print(f"find-reprints: задача {job['id']} в очереди {job['queue_name']}, пар={len(pairs)}, режим: {mode}")
        print(f"Смотреть результат: cli jobs-show {job['id']} или таблица article_reprints")
        return

    client = make_client(offline=args.offline)
    try:
        result = reprints.review_candidates(candidates, client, dry_run=not args.apply)
    except reprints.ReprintSetupError as exc:
        raise SystemExit(f"find-reprints: {exc}") from exc
    st = result["stats"]
    print(f"проверено моделью: {st['checked']}, перепечаток: {st['reprints']}, "
          f"разные события: {st['distinct']}, ошибок: {st['errors']}")
    if not args.apply:
        print("СУХОЙ ПРОГОН — ничего не помечено. Для записи добавьте --apply")
    for d in result["decisions"][:args.show]:
        mark = "ДУБЛЬ" if d["same_event"] else "разные"
        print(f"  [{mark}] {d['overlap']:.0%}  {str(d['a_title'])[:44]} || {str(d['b_title'])[:44]}")
        print(f"          {d['reason'][:110]}")
    if args.apply:
        stats = repository.reprint_stats()
        print(f"в базе помечено перепечаток: {stats['total']} в {stats['groups']} группах")


def cmd_repair_article_bodies(args: argparse.Namespace) -> None:
    """Перекачать тела статей с дефектом (чужое/кракозябры/простыня) новым извлечением, срезать хвосты.

    По умолчанию сухой прогон. С --apply тела заменяются, а по заменённым ставится
    перерасчёт сути (посчитана по старому тексту) тем же путём, что пересчёт после полного текста
    (fulltext_recompute.enqueue_after_body): решение ленты за «Отбором» (правило владельца 02.11),
    мимо выбранного «в дайджест», черновика выпуска, скрытого и отклонённого, по полосе пересчётов.

    --tails: только хвосты (#140), по всем источникам или по названному. В конце по источникам:
    сколько статей и знаков уйдёт. --all-pages: качать страницу и там, где хвост не виден по тексту.

    Перерасчёт ставится только статьям открытых месяцев окна ленты: у статьи закрытого месяца новая суть
    переписала бы закрытый выпуск, а он только на просмотр (решение владельца 30.09). Тело
    такой статьи меняется (выпуск его не показывает); пропущенные — в отчёте."""
    from oiltech_digest import feed_window
    from oiltech_digest.db import repository
    from oiltech_digest.ingestion import body_repair
    from oiltech_digest.processing import fulltext_recompute

    ids = [int(item) for item in args.ids.split(",") if item.strip()] if args.ids else None
    statuses = [item.strip() for item in args.statuses.split(",") if item.strip()] if args.statuses else None
    if not ids and args.source_id is None and not statuses and not args.tails:
        raise SystemExit("repair-article-bodies: нужен --ids, --source-id, --statuses или --tails")
    if args.all_pages and not args.tails:
        raise SystemExit("repair-article-bodies: --all-pages только вместе с --tails")
    articles = body_repair.candidate_articles(
        source_id=args.source_id, days=args.days, ids=ids, statuses=statuses, all_sources=args.tails,
    )
    if args.limit:
        articles = articles[: args.limit]
    result = body_repair.repair_bodies(articles, apply=args.apply, pause_seconds=args.pause,
                                       find_tail=body_repair.stored_repeated_tail, only_tails=args.tails,
                                       all_pages=args.all_pages)
    jobs: list[int] = []
    archive: list[int] = []
    left_out: list[int] = []
    if args.apply and args.reprocess and result["replaced_ids"]:
        archive = repository.closed_month_article_ids(result["replaced_ids"], feed_window.current())
        replaced = [article_id for article_id in result["replaced_ids"] if article_id not in archive]
        recount = fulltext_recompute.enqueue_after_body(replaced, batch_size=args.batch)
        jobs = recount["jobs"]
        left_out = sorted(set(replaced) - set(recount["articles"]))
        result["reprocess_skipped"] = recount["skipped"]
    result["reprocess_jobs"] = jobs
    result["reprocess_skipped_archive"] = archive
    result["reprocess_left_out"] = left_out
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    print(
        f"repair-article-bodies: checked={result['checked']} replaced={result['replaced']} "
        f"apply={result['apply']} reprocess_jobs={len(jobs)}"
    )
    for key, count in result["stats"].items():
        print(f"  {key}: {count}")
    if result["tails_by_source"]:
        print("  хвосты по источникам:" if args.apply else "  хвосты по источникам (уйдёт при --apply):")
        for row in result["tails_by_source"]:
            print(f"    {row['source']} (id {row['source_id']}): статей {row['articles']}, знаков {row['chars']}")
    if archive:
        print(f"  без перерасчёта ИИ — закрытый месяц, архив только на просмотр: {len(archive)} "
              f"(id: {', '.join(str(article_id) for article_id in archive)})")
    if left_out:
        print(f"  без перерасчёта ИИ: выбрано в выпуск, в черновике выпуска, скрыто или не принято: {len(left_out)} "
              f"(id: {', '.join(str(article_id) for article_id in left_out[:50])})")
    if result.get("reprocess_skipped"):
        print(f"  перерасчёт не поставлен: {result['reprocess_skipped']}")


def cmd_repair_article_dates(args: argparse.Namespace) -> None:
    """Дата публикации, противоречащая дате в адресе статьи (#139), по новому разбору страницы.

    Сухой прогон (--ids или --source-id) заново скачивает и разбирает страницы, ничего не пишет и
    печатает по строке на статью: план «id: было → стало», переезды по месяцам ленты (у закрытого
    месяца меняется архив) и остальные статьи с тем, что даёт их страница. --save-plan сохраняет
    план. --apply ПЛАН пишет ровно его, без новой перекачки: дату публикации и её копию у публикации
    сигнала ленты; перерасчёт ИИ не ставится: он платный, решение отдельное."""
    from oiltech_digest import feed_window
    from oiltech_digest.ingestion import date_repair

    def day(value) -> str:
        return value.date().isoformat() if value else "пусто"

    ids = [int(item) for item in args.ids.split(",") if item.strip()] if args.ids else None
    if args.apply:
        if ids or args.source_id is not None or args.save_plan:
            raise SystemExit("repair-article-dates: --apply пишет план сухого прогона; --ids, --source-id и "
                             "--save-plan с ним не задаются")
        try:
            changes = date_repair.load_plan(args.apply)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"repair-article-dates: {exc}") from exc
        written = date_repair.apply_plan(changes)
        if args.json:
            print(json.dumps(written, ensure_ascii=False, indent=2, default=str))
            return
        print(f"repair-article-dates: в плане={written['planned']} записано={len(written['written_ids'])} "
              f"копий даты у публикаций сигналов ленты={written['publication_copies']}")
        if written["not_written_ids"]:
            print("  не записано, дату после сухого прогона поменяли (новое согласование): "
                  + ", ".join(str(item) for item in written["not_written_ids"]))
        return

    if not ids and args.source_id is None:
        raise SystemExit("repair-article-dates: нужен --ids, --source-id или --apply ПЛАН")
    if args.limit < 0 or args.pause < 0 or args.days < 1:
        raise SystemExit("repair-article-dates: --limit и --pause не меньше 0, --days не меньше 1")
    articles = date_repair.candidate_articles(source_id=args.source_id, days=args.days, ids=ids)
    if args.limit:
        articles = articles[: args.limit]
    result = date_repair.repair_dates(articles, pause_seconds=args.pause)
    if args.save_plan:
        Path(args.save_plan).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                                        encoding="utf-8")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    scope = (f"статьи {args.ids}" if ids else
             f"источник {args.source_id}, собранные с {(datetime.now(timezone.utc) - timedelta(days=args.days)).date()}")
    print(f"repair-article-dates: {scope}; проверено={result['checked']} в плане={result['changed']} "
          "[сухой прогон, без записи]")
    for key, count in result["stats"].items():
        print(f"  {key}: {count}")
    window = feed_window.current()
    for move in result["months"]:
        state = "открыт" if window.is_open(move["new_month"]) else "закрыт"
        print(f"  месяц ленты {move['old_month']} → {move['new_month']} ({state}): {move['count']}")
    for item in result["changes"]:
        print(f"  {item['article_id']}: {day(item['old'])} → {day(item['new'])}  {item['url']}")
    for item in result["skipped"]:
        page = day(item["page"]) if item["page_status"] == "read" else item["page_status"]
        print(f"  {item['article_id']}: было {day(item['old'])}, страница {page}; не в плане ({item['reason']})"
              f"  {item['url']}")
    if args.save_plan:
        print(f"  план: {args.save_plan}")


def cmd_repair_url_keys(args: argparse.Namespace) -> None:
    """Ключ адреса 13.09 срезал query целиком и склеил статьи, у которых номер — в query
    (Минэнерго, EIA, Новатэк, Лукойл, РГУ Губкина); схема тогда же спрятала «копии».
    По умолчанию сухой прогон: сколько ключей пересчитается и сколько статей вернётся."""
    from oiltech_digest.ingestion import url_key_repair

    result = url_key_repair.repair_url_keys(apply=args.apply)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"repair-url-keys: ключей пересчитать={result['key_updates']} вернуть в ленту={result['unhidden']} "
          f"остаются скрытыми={sum(result['kept_hidden'].values())} {result['kept_hidden']} "
          + ("[записано]" if result["apply"] else "[сухой прогон, без записи]"))
    for source, counts in result["by_source"].items():
        print(f"  {source}: {counts}")
    for item in result["key_conflicts"]:
        print(f"  ДУБЛЬ В ЛЕНТЕ: статья {item['id']} ({item['url']}) — её новый ключ уже у статьи "
              f"{item['holder_id']}; ключ не менялся, решить вручную")


def cmd_enqueue_external_refetch(args: argparse.Namespace) -> None:
    """Дозаполнить тело статей-обрывков у источников зарубежного контура.

    Локальная дозагрузка их не берёт намеренно: с РФ-адреса эти сайты отдают 403, а
    попытка там одна и навсегда. Замер 17.09 — у Oil & Gas Journal и Offshore Magazine
    25 статей из 25 короче 600 знаков, средняя длина 183; у McKinsey 76 из 78.

    Ставится пакетами: воркер качает страницы, ядро записывает тела, пропуская
    подменённые через стража принадлежности (задача №24)."""
    from oiltech_digest import config, network_policy
    from oiltech_digest.db import repository

    if not (config.EXTERNAL_WORKERS_ENABLED and config.FETCH_EXTERNAL_ENABLED):
        print("enqueue-external-refetch: внешний фетч-контур выключен — пропуск")
        return

    rows = repository.external_refetch_candidates(limit=args.limit)
    if not rows:
        print("enqueue-external-refetch: обрывков у внешних источников нет")
        return

    ids = [int(r["id"]) for r in rows]
    # Маршрут спрашиваем как у ФЕТЧ-задачи, а не у ИИ: это скачивание страниц.
    # Раньше здесь стоял route_ai_processing и хардкод очереди, из-за чего при
    # неexternal-решении задача уезжала в 'default' с execution_region='external' —
    # несогласованная пара, которую никто бы не разобрал.
    probe = {"parse_strategy": "request", "network_region": "external", "network_profile": "direct"}
    decision = network_policy.route_source_task(probe, task_kind="refetch")
    enq = 0
    for start in range(0, len(ids), args.batch):
        chunk = ids[start:start + args.batch]
        repository.create_background_job(
            "refetch_text",
            {"article_ids": chunk, "min_chars": config.MIN_FULL_TEXT_CHARS},
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        enq += 1
    print(f"enqueue-external-refetch: статей={len(ids)}, задач={enq}, очередь={decision.queue_name}")


def cmd_enqueue_external_scrape(args: argparse.Namespace) -> None:
    """Поставить в очередь фетч источников network_region='external' через зарубежный воркер.

    Эти источники недоступны с РФ-сервера (WAF/таймаут/гео-блок) — их рендер/фетч идёт
    из NL. Действует ТОЛЬКО при EXTERNAL_WORKERS_ENABLED=1 и FETCH_EXTERNAL_ENABLED=1,
    иначе no-op (источники парсятся локально как обычно). Вызывается планировщиком
    каждый цикл; можно и руками для разового прогона."""
    from oiltech_digest import config, network_policy
    from oiltech_digest.db import repository

    if not (config.EXTERNAL_WORKERS_ENABLED and config.FETCH_EXTERNAL_ENABLED):
        print("enqueue-external-scrape: внешний фетч-контур выключен "
              "(нужны EXTERNAL_WORKERS_ENABLED=1 и FETCH_EXTERNAL_ENABLED=1) — пропуск")
        return

    sources = [
        s for s in repository.get_enabled_sources()
        if str(s.get("network_region") or "auto").strip().lower() == "external"
        and s.get("parse_strategy") in {"request", "playwright", "rss"}
    ]
    enq = 0
    queues: dict[str, int] = {}
    for source in sources:
        decision = network_policy.route_source_task(source, task_kind="scrape")
        if decision.execution_region != "external":
            continue
        repository.create_background_job(
            "scrape_source",
            {"source_id": source["id"], "max_age_days": args.max_age_days},
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        enq += 1
        queues[decision.queue_name] = queues.get(decision.queue_name, 0) + 1
    detail = ", ".join(f"{q}={n}" for q, n in sorted(queues.items())) or "—"
    print(f"enqueue-external-scrape: external-источников={len(sources)}, задач={enq} ({detail})")


def cmd_set_source_region(args: argparse.Namespace) -> None:
    """Проставить network_region (auto|ru|external) источникам по списку id.

    Для гео-падающих западных источников из source-audit, чьи имена в БД неточны для
    реестра (Deloitte/QatarEnergy/Weatherford и т.п.). external берётся в работу
    enqueue-external-scrape при включённом внешнем контуре."""
    from oiltech_digest.db import repository

    region = args.region.strip().lower()
    if region not in {"auto", "ru", "external"}:
        raise SystemExit("region должен быть auto|ru|external")
    ids = [int(x) for x in str(args.ids).split(",") if x.strip()]
    if not ids:
        raise SystemExit("укажите --ids 1,2,3")
    n = repository.set_sources_network_region(ids, region)
    print(f"set-source-region: обновлено={n}, region={region}, ids={ids}")


def cmd_source_dump_listing(args: argparse.Namespace) -> None:
    """Выгрузить анкеры листинга источника — для подбора listing_selector у no_candidates.

    Уважает стратегию (playwright → рендер, иначе http_client.fetch с боевыми SSL-фоллбэками).
    Печатает href + текст + контейнер (тег.class родителя) — этого достаточно, чтобы понять,
    каким селектором цеплять ссылки на статьи."""

    from oiltech_digest.db import repository
    from oiltech_digest.ingestion import http_client, normalize

    source = repository.get_source(args.source_id)
    if source is None:
        raise SystemExit(f"источник {args.source_id} не найден")
    listing_url = getattr(args, "url", None) or source.get("listing_url") or source.get("rss_url") or source.get("url")
    strategy = (source.get("parse_strategy") or "").lower()
    render = getattr(args, "render", False)
    mode = "playwright-render" if (render or strategy == "playwright") else strategy
    print(f"#{source['id']} {source.get('name')} [{mode}] → {listing_url}")
    if render or strategy == "playwright":
        from oiltech_digest.ingestion import playwright_parser
        if not playwright_parser.is_available():
            raise SystemExit("playwright недоступен в этом контейнере")
        # больший settle — JS-SPA успевают дорисовать листинг
        content = playwright_parser.fetch_rendered(listing_url, settle_ms=8000)
    else:
        content = http_client.fetch(listing_url)
    if not content:
        raise SystemExit("листинг не получен (см. логи fetch выше)")
    print(f"получено байт: {len(content)}")

    doc = normalize.parse_html(content)
    try:
        doc.make_links_absolute(listing_url)
    except Exception:  # noqa: BLE001 — относительные ссылки тоже информативны
        pass
    anchors = doc.xpath("//a[@href]")
    print(f"всего <a> с href: {len(anchors)}; показываю до {args.limit} с непустым текстом:")
    shown = 0
    for a in anchors:
        text = " ".join(a.text_content().split())[:70]
        if not text:
            continue
        parent = a.getparent()
        ctx = f"{parent.tag}.{(parent.get('class') or '')[:45]}" if parent is not None else "?"
        print(f"  [{ctx}] {text}  →  {a.get('href')}")
        shown += 1
        if shown >= args.limit:
            break


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

    from oiltech_digest import config, feed_window
    from oiltech_digest.db import repository

    # Вердикты проверяются здесь, а не choices= парсера: сборка парсера не импортирует
    # репозиторий (NL-воркер зовёт cli без базы), а список живёт в одном месте.
    if args.verdict and args.verdict not in repository.SOURCE_HEALTH_VERDICTS:
        raise SystemExit(f"--verdict: одно из {', '.join(repository.SOURCE_HEALTH_VERDICTS)}")
    # Порог печатается в шапке: число stale без него не сравнить с прошлыми замерами (до 28.09 — 3 дня).
    stale_days = config.SOURCE_STALE_DAYS if args.stale_days is None else args.stale_days
    rows = repository.source_health_report(stale_days=stale_days, limit=args.limit, verdict=args.verdict)
    counts = Counter(row["verdict"] for row in rows)
    print(
        f"source-health: stale_days={stale_days}, "
        + ", ".join(f"{name}={counts.get(name, 0)}" for name in repository.SOURCE_HEALTH_VERDICTS)
    )
    for row in rows:
        last = row.get("last_article_at")
        # Дата по Москве, как в правиле stale: в поясе сессии БД (на проде UTC) загрузка
        # с 00:00 до 03:00 МСК печаталась днём раньше, и дни не сходились с вердиктом.
        last_s = last.astimezone(feed_window.MSK).date().isoformat() if hasattr(last, "astimezone") else "—"
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


def cmd_source_probe(args: argparse.Namespace) -> None:
    """Проба источника путём сбора: вердикт рубежей вставки по каждому кандидату.

    Отвечает на «почему источник молчит» там, где source-diagnose останавливается на
    «скачалось и прошло предфильтр»: ключ адреса занят той же или другой статьёй, то же
    тело, адрес уже в базе. Рубежи — те же функции, что у вставки. Ничего не пишет: все
    соединения процесса — только для чтения, запись отклонит сама база."""
    from oiltech_digest.db import connection, repository
    from oiltech_digest.ingestion import source_probe

    with connection.read_only_process():
        source = repository.get_source(args.source_id)
        if source is None:
            raise SystemExit(f"Источник не найден: {args.source_id}")
        report = source_probe.probe_source(source)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return
    print(source_probe.format_report(report, rows_per_verdict=args.rows))


def _audit_row(source: dict, diag: dict) -> dict:
    """Нормализовать вывод diagnose_source в одну компактную строку аудита."""
    probe = (diag.get("listing_probe") or diag.get("rss_probe")
             or diag.get("preview_probe") or {})
    count = (diag.get("candidate_count") if diag.get("candidate_count") is not None
             else diag.get("entry_count") if diag.get("entry_count") is not None
             else diag.get("post_count") or 0)
    error = diag.get("error") or probe.get("error")
    if not error:
        for check in diag.get("article_checks") or []:
            ap = check.get("article_probe") or {}
            if ap.get("error"):
                error = ap["error"]
                break
    return {
        "id": source.get("id"),
        "name": source.get("name"),
        "enabled": bool(source.get("enabled")),
        "strategy": source.get("parse_strategy") or "—",
        "link": diag.get("url") or source.get("listing_url") or source.get("rss_url") or source.get("url"),
        "verdict": diag.get("verdict"),
        "count": count or 0,
        "probe_status": probe.get("status"),
        "probe_bytes": probe.get("bytes"),
        "error": (error or "")[:200] or None,
    }


def cmd_source_audit(args: argparse.Namespace) -> None:
    """Аудит ВСЕХ источников: какая ссылка установлена сейчас и почему не парсится.

    По каждому источнику — read-only diagnose_source (живая проба + кандидаты). Запускать
    там, где правильное гео и установлен playwright (прод), иначе иностранные/JS-источники
    дадут ложные вердикты. Сетевые пробы идут параллельно."""
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor

    from oiltech_digest.db import repository
    from oiltech_digest.ingestion.source_diagnostics import diagnose_source

    sources = repository.list_sources(limit=args.limit)
    if args.strategy:
        sources = [s for s in sources if (s.get("parse_strategy") or "") == args.strategy]
    if args.enabled_only:
        sources = [s for s in sources if s.get("enabled")]

    def run(source: dict) -> dict:
        try:
            diag = diagnose_source(source, limit=args.probe_limit)
        except Exception as exc:  # noqa: BLE001 - одна поломка не валит аудит
            diag = {"verdict": "diagnose_error", "url": source.get("url"), "error": str(exc)}
        return _audit_row(source, diag)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(run, sources))

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return

    counts = Counter(row["verdict"] for row in rows)
    print("source-audit: " + ", ".join(f"{verdict}={n}" for verdict, n in counts.most_common()))
    for row in sorted(rows, key=lambda r: (r["verdict"] or "", r["id"])):
        flag = "on " if row["enabled"] else "off"
        probe = f"{row['probe_status']}/{row['probe_bytes']}b" if row["probe_status"] is not None else "—"
        line = (f"{row['id']:>4} {flag} {row['strategy']:<10} {str(row['verdict']):<22} "
                f"cand={row['count']:<3} probe={probe:<14} {row['name']}")
        print(line)
        print(f"      link: {row['link'] or '—'}")
        if row["error"]:
            print(f"      err:  {row['error']}")


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

    process_totals = {"processed": 0, "fulltext": 0, "summary": 0, "translated": 0, "rejected": 0, "errors": 0}
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
        f"перевод={process_totals['translated']}, отклонено={process_totals['rejected']}, "
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


def _print_issue_months(months: list[dict]) -> None:
    """Состав выпуска закрытых месяцев: прежнее правило против общих отметок."""
    if not months:
        print("  закрытых месяцев с выпуском нет")
    for month in months:
        after = month["after"]
        line = f"  {month['month']}: статей {len(after['article_ids'])}, карточек радара {len(after['signal_ids'])}"
        if month["match"]:
            print(f"{line} — совпадает с прежним выпуском")
        else:
            print(f"{line} — РАСХОЖДЕНИЕ: было {month['before']}, стало бы {after}")


def cmd_migrate_shared_marks(args: argparse.Namespace) -> None:
    """Перенос личных отметок и черновиков учётки-основы в общие (#111). Пробный прогон по
    умолчанию; --write — записать; --check — сверка после переноса только чтением. Расхождение
    состава закрытого месяца — выход с кодом 1."""
    from oiltech_digest.db.marks_migration import check_shared_marks, migrate_shared_marks

    if args.check:
        try:
            report = check_shared_marks()
        except ValueError as exc:
            print(f"migrate-shared-marks --check: {exc}")
            raise SystemExit(1)
        print(f"migrate-shared-marks --check (только чтение): перенос {report['done_at']:%d.%m.%Y %H:%M},"
              f" основа — учётка №{report['base_user_id']}, месяцы до {report['open_from']}")
        _print_issue_months(report["months"])
        if not report["ok"]:
            raise SystemExit(1)
        return
    if args.base_user_id is None:
        print("migrate-shared-marks: нужен --base-user-id (или --check)")
        raise SystemExit(2)
    report = migrate_shared_marks(args.base_user_id, write=args.write)
    if report["already_done"]:
        print(f"migrate-shared-marks: перенос уже сделан {report['done_at']:%d.%m.%Y %H:%M}"
              f" (основа — учётка №{report['base_user_id']}) — повторный ничего не меняет; сверка — --check")
        return
    mode = "ЗАПИСЬ" if args.write else "пробный прогон (ничего не записано; --write — записать)"
    print(f"migrate-shared-marks: основа — учётка №{report['base_user_id']}, {mode}")
    for kind, label in (("articles", "статей"), ("signals", "карточек радара")):
        part = report[kind]
        statuses = ", ".join(f"{status}={count}" for status, count in part["by_status"].items()) or "нет"
        print(f"  отметки {label} у основы: {statuses}; в общие — {part['inserted']}")
    print(f"  черновики основы — в общий черновик месяца: {', '.join(report['drafts_copied']) or 'нет'}")
    skipped = report["skipped"]
    print(f"  не переносятся (проверочные других учёток): отметок статей {skipped['articles']['rows']}"
          f" у {skipped['articles']['users']} учёток, отметок карточек {skipped['signals']['rows']}"
          f" у {skipped['signals']['users']}, личных черновиков {skipped['drafts']['rows']};"
          f" их примеров «выбран в дайджест» гаснет {report['examples_superseded']}")
    print(f"  сверка закрытых месяцев (до {report['open_from']}):")
    _print_issue_months(report["months"])
    if not report["ok"]:
        print("  ОСТАНОВЛЕНО: состав закрытого месяца изменился бы — ничего не записано")
        raise SystemExit(1)
    print("  записано" if report["written"] else "  сверка сошлась")


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
        concurrency=args.concurrency,
    )


def cmd_tg_materials_bot(args: argparse.Namespace) -> None:
    """Бот «Материалы коллег» (NL): приём материалов из рабочей группы Telegram в ядро."""
    from oiltech_digest import tg_materials_bot

    tg_materials_bot.run(core_api_url=args.core_api_url, token=args.token, worker_id=args.worker_id)


def cmd_jobs_requeue_stale(args: argparse.Namespace) -> None:
    from oiltech_digest import config
    from oiltech_digest.db import repository

    stale_minutes = (
        config.BACKGROUND_JOB_STALE_MINUTES
        if args.stale_minutes is None
        else args.stale_minutes
    )
    local = repository.requeue_stale_background_jobs(stale_minutes)
    external = repository.requeue_expired_external_leases()
    print(
        f"jobs-requeue-stale: requeued={local.requeued}, exhausted={local.exhausted}, "
        f"stale_minutes={stale_minutes}"
    )
    print(
        f"external-leases: requeued={external.requeued}, exhausted={external.exhausted}"
    )


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
    for alert in status.get("alerts") or []:
        print(f"  ТРЕВОГА: {alert['message']}")


def cmd_check_lanes(args: argparse.Namespace) -> None:
    """Сторож полос: застой очереди, очередь без живого воркера, истёкшие аренды.

    Шаг планировщика каждый цикл. При тревоге — строки в лог и код 2: run_step пишет
    «FAIL check-lanes», цикл продолжается. 20.09 208 задач без потребителя было видно
    только по последствиям через 8,5 ч; здесь — через ~15 мин."""
    from oiltech_digest.db import repository

    logger = logging.getLogger(__name__)
    status = repository.external_queue_status()
    # Версии потребителей — каждый цикл в лог: «пересобран ли NL» видно без раскопок.
    print(f"check-lanes: контракт ядра {status.get('contract')}")
    for line in _consumer_lines(status.get("consumers") or []):
        print(f"check-lanes: {line}")
    alerts = status.get("alerts") or []
    if not alerts:
        print("check-lanes: ok")
        return
    for alert in alerts:
        logger.warning("lane_alert kind=%s queue=%s count=%s", alert["kind"], alert.get("queue"), alert.get("count"))
        print(f"check-lanes: ТРЕВОГА {alert['message']}")
    raise SystemExit(2)


def cmd_live_ai_leases(args: argparse.Namespace) -> None:
    """Страж выката ядра: ИИ-задачи в работе. Код 3, если есть, — scripts/deploy-core.sh
    тогда отказывается перезапускать ядро без --force."""
    from oiltech_digest.db import repository

    rows = repository.live_ai_leases()
    if not rows:
        print("live-ai-leases: ИИ-задач в работе нет")
        return
    for row in rows:
        print(
            f"live-ai-leases: задача {row['id']} {row['kind']} [{row['queue_name']}] "
            f"у {row.get('claimed_by') or '—'}, {row['status']}, аренда до {row.get('lease_expires_at') or '—'}"
        )
    raise SystemExit(3)


def _ago(value) -> str:
    if value is None:
        return "никогда"
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    seconds = max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))
    if seconds < 120:
        return f"{seconds} с назад"
    if seconds < 7200:
        return f"{seconds // 60} мин назад"
    return f"{seconds // 3600} ч назад"


def _consumer_lines(consumers: list[dict]) -> list[str]:
    return [
        f"{row.get('consumer')} [{', '.join(row.get('queues') or []) or '—'}] "
        f"сборка {row.get('build') or '—'}, контракт {'—' if row.get('contract') is None else row.get('contract')}, "
        f"запрос {_ago(row.get('last_seen_at'))}"
        for row in consumers
    ]


def _fetch_consumer_versions() -> dict:
    from oiltech_digest import config, external_worker

    return external_worker.ExternalWorkerClient(
        core_api_url=config.CORE_API_URL, token=config.EXTERNAL_WORKER_TOKEN, worker_id=config.EXTERNAL_WORKER_ID,
        queues=config.EXTERNAL_WORKER_QUEUES, capabilities=config.EXTERNAL_WORKER_CAPABILITIES,
    ).consumers()


def cmd_worker_versions(args: argparse.Namespace) -> None:
    """Версии контейнеров NL глазами ядра — на NL, где базы нет.

    --self: этот контейнер обязан уже отметиться в ядре нужной сборкой и контрактом ядра
    (код 1, если нет). Так скрипт выката NL убеждается, что перезапущенный воркер жив и
    новый, прежде чем трогать следующий."""
    from oiltech_digest import config, contract

    data = _fetch_consumer_versions()
    expected = data.get("contract")
    consumers = data.get("consumers") or []
    print(f"worker-versions: контракт ядра {expected}")
    for line in _consumer_lines(consumers):
        print(f"worker-versions: {line}")
    if not args.self_check:
        return
    me = contract.consumer_of(config.EXTERNAL_WORKER_ID)
    row = next((item for item in consumers if item.get("consumer") == me), None)
    problems = []
    if row is None:
        problems.append(f"{me} ещё не обращался к ядру")
    else:
        if args.expect_build and row.get("build") != args.expect_build:
            problems.append(f"{me}: сборка {row.get('build') or '—'}, ждём {args.expect_build}")
        if row.get("mismatch"):  # правило одно — lanes.consumer_mismatch на ядре
            problems.append(f"{me}: контракт {row.get('contract')}, у ядра {expected}")
    for problem in problems:
        print(f"worker-versions: НЕ ГОТОВО — {problem}")
    if problems:
        raise SystemExit(1)
    print(f"worker-versions: {me} — новая сборка на месте")


def cmd_scheduler_lock(args: argparse.Namespace) -> None:
    """Запустить команду под замком планировщика: второй экземпляр ждёт, а не дублирует."""
    from oiltech_digest import singleton

    command = list(args.command or [])
    if command[:1] == ["--"]:
        command = command[1:]
    key = singleton.SCHEDULER_LOCK_KEY if args.key is None else args.key
    raise SystemExit(singleton.run_exclusive(
        command, key=key, poll_seconds=args.poll_seconds, check_seconds=args.check_seconds,
    ))


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


def cmd_seed_signal_topics(args: argparse.Namespace) -> None:
    from oiltech_digest.signal_discovery import seed_default_radar_topics

    changed = seed_default_radar_topics()
    print(f"seed-signal-topics: topics={changed}")


def cmd_enqueue_signal_discovery(args: argparse.Namespace) -> None:
    from oiltech_digest import signal_discovery
    from oiltech_digest.db import repository

    try:
        # Модель поиска GPT и прочие его настройки (#138) — решение ядра, едут в задаче.
        research = signal_discovery.research_settings(offline=args.offline, days=args.days)
    except signal_discovery.SearchSetupError as exc:
        raise SystemExit(f"enqueue-signal-discovery: {exc}") from exc
    payload = {
        **research,
        "topic": args.topic,
        "days": args.days,
        "offline": args.offline,
        "dry_run": args.dry_run,
    }
    # См. background_jobs.enqueue_daily_signal_discovery: маршрут решает политика,
    # иначе задача уезжает на РФ-адрес и OpenAI отвечает 403 по географии.
    from oiltech_digest import network_policy

    decision = network_policy.route_ai_processing()
    job = repository.create_background_job(
        "signal_discovery",
        payload,
        queue_name=decision.queue_name if not args.offline else "default",
        execution_region=decision.execution_region if not args.offline else "ru",
        capability=decision.capability if not args.offline else None,
        # Одна попытка, как у ежедневной: прогон «режима ChatGPT» оплачивается целиком
        # (≈$7,6), а упавший или зависший повтор оплатил бы его ещё дважды.
        max_attempts=1,
    )
    print(f"enqueue-signal-discovery: job id={job['id']} queue={job['queue_name']}")


def cmd_enqueue_daily_signal_discovery(args: argparse.Namespace) -> None:
    from oiltech_digest import background_jobs, signal_discovery

    try:
        result = background_jobs.enqueue_daily_signal_discovery(force=args.force)
    except signal_discovery.SearchSetupError as exc:
        raise SystemExit(f"enqueue-daily-signal-discovery: {exc}") from exc
    if not result["enqueued"]:
        print(f"enqueue-daily-signal-discovery: skipped reason={result['reason']}")
        return
    job = result["job"]
    print(f"enqueue-daily-signal-discovery: job id={job['id']} queue={job['queue_name']}")


def cmd_enqueue_signal_cards(args: argparse.Namespace) -> None:
    """Шаг планировщика «заполнить карточки» (#96): новые сигналы без карточки — в задачи агента
    «Карточка сигнала» по полосе ежедневной обработки. За флагом SIGNAL_CARD_ENABLED. С --ids —
    пересчёт названных сигналов по полосе пересчётов (без флага: это решение оператора)."""
    from oiltech_digest import signal_card

    try:
        if args.ids:
            ids = [int(value) for value in args.ids.split(",") if value.strip()]
            result = {"signals": len(ids), "jobs": signal_card.enqueue_cards(ids, trigger="manual", bulk=True)}
        else:
            result = signal_card.enqueue_new_signals()
    except signal_card.CardSetupError as exc:
        raise SystemExit(f"enqueue-signal-cards: {exc}") from exc
    if result.get("reason"):
        print(f"enqueue-signal-cards: skipped reason={result['reason']}")
        return
    queues = sorted({job["queue_name"] for job in result["jobs"]})
    print(f"enqueue-signal-cards: сигналов {result['signals']}, задач {len(result['jobs'])}"
          + (f", очередь {', '.join(queues)}" if queues else ""))


def cmd_enqueue_otbor(args: argparse.Namespace) -> None:
    """Шаг планировщика «Отбор» (#130, #131): новые статьи, находки поиска и материалы коллег, которые решает агент
    «Отбор», ставит в его задачи по полосе ежедневной обработки. С --ids (статьи) и
    --signal-ids (находки и материалы) — названные публикации «Отбора» по полосе пересчётов: повтор после разбора
    сбоя, в том числе ответа не по правилам (его шаг не повторяет)."""
    from oiltech_digest import otbor
    from oiltech_digest.db import otbor_store

    try:
        if args.ids or args.signal_ids:
            named = [int(value) for value in args.ids.split(",") if value.strip()]
            ids = otbor_store.own_article_ids(named)
            chosen = otbor_store.chosen_for_digest(named)
            if chosen:
                print(f"enqueue-otbor: статьи {chosen[:20]} выбраны в дайджест, их решение «Отбор» не пересматривает")
            other = sorted(set(named) - set(ids) - set(chosen))
            if other:
                print(f"enqueue-otbor: статьи {other[:20]} решает не «Отбор» — пропущены")
            named_signals = [int(value) for value in args.signal_ids.split(",") if value.strip()]
            signal_ids = otbor_store.own_signal_ids(named_signals)
            skipped = sorted(set(named_signals) - set(signal_ids))
            if skipped:
                print(f"enqueue-otbor: сигналы {skipped[:20]} «Отбор» не пересматривает (не его или выбраны в дайджест)"
                      " — пропущены")
            result = {"articles": len(ids), "signals": len(signal_ids),
                      "jobs": otbor.enqueue(ids, signal_ids=signal_ids, trigger="manual", bulk=True)}
        else:
            result = otbor.scheduler_step()
    except otbor.OtborSetupError as exc:
        raise SystemExit(f"enqueue-otbor: {exc}") from exc
    queues = sorted({job["queue_name"] for job in result["jobs"]})
    print(f"enqueue-otbor: статей {result['articles']}, находок и материалов {result['signals']},"
          f" задач {len(result['jobs'])}"
          + (f", очередь {', '.join(queues)}" if queues else "")
          + (f", без ответа после попыток {result['unanswered']}" if result.get("unanswered") else ""))


def cmd_otbor_materials(args: argparse.Namespace) -> None:
    """Накопленные материалы коллег (#131): присланные до включения «Отбора» и не ставшие сигналом проходят конвейер
    один раз. По умолчанию сухой прогон — сколько материалов и оценка цены «Отбора», ничего не пишет; --apply —
    сигналы материалов и задачи «Отбора» по полосе пересчётов; с --silent итог авторам
    бот не сообщает (решение владельца 04.11 для присланных до включения)."""
    from oiltech_digest import material_signals, otbor

    try:
        result = material_signals.convert_accumulated(apply=args.apply, silent=args.silent)
    except otbor.OtborSetupError as exc:
        raise SystemExit(f"otbor-materials: {exc}") from exc
    price = (f", «Отбор» около ${result['cost_usd']:.4f} ({result['model']})" if result["model"]
             else ", модель «Отбора» не задана (OTBOR_MODEL)")
    if not result["applied"]:
        print(f"otbor-materials: накопленных материалов {result['materials']}{price}; сухой прогон, ничего не записано"
              " (--apply — провести через конвейер)")
        return
    print(f"otbor-materials: сигналов материалов {len(result['signal_ids'])}, задач «Отбора» {result['jobs']}{price}"
          + ("; итог авторам бот не сообщает" if args.silent else ""))


def cmd_otbor_shadow(args: argparse.Namespace) -> None:
    """Проверка «Отбора» на реальных данных (#134): статьи ленты с --since, решённые гейтом до «Отбора», без решения
    человека и в открытом месяце, проходят «Отбор»; решения пишутся рядом, витрины не меняются. По умолчанию сухой
    прогон: число статей и цена, ничего не пишет; --apply ставит задачи по полосе пересчётов. Флаг не нужен."""
    from oiltech_digest import otbor, otbor_shadow

    try:
        plan = otbor_shadow.plan(date.fromisoformat(args.since), model=args.model or None)
    except otbor.OtborSetupError as exc:
        raise SystemExit(f"otbor-shadow: {exc}") from exc
    estimate, count = plan["estimate"], len(plan["article_ids"])
    print(f"otbor-shadow: модель {plan['model']}, инструкция {plan['prompt_version']}, справочники "
          f"{plan['dictionaries_version']}")
    print(f"  статьи ленты с {plan['since']}, решённые гейтом, без решения человека, в открытом месяце: статей {count}")
    print(f"  цена около {_usd(estimate['cost_usd'])}: вызовов {estimate['calls']}, токенов около "
          f"{estimate['input_tokens'] // 1000} тыс. входа и {estimate['output_tokens'] // 1000} тыс. выхода (ответ "
          f"{otbor.ESTIMATED_OUTPUT_TOKENS} токенов на вызов, знаков на токен {otbor.CHARS_PER_TOKEN})")
    print(f"  полоса {plan['queue']}: задач {-(-count // plan['batch'])} по {plan['batch']} статей")
    if not plan["bulk_lane"]:
        print("  ВНИМАНИЕ: полоса пересчётов выключена (AI_BULK_LANE_ENABLED): задачи встали бы в поток дня перед "
              "новыми статьями; --apply откажет")
    if not args.apply:
        print("  сухой прогон: задачи не поставлены и не оплачены (поставить: --apply)")
        return
    try:
        jobs = otbor_shadow.enqueue(plan)
    except otbor.OtborSetupError as exc:
        raise SystemExit(f"otbor-shadow: {exc}") from exc
    print(f"  поставлено задач {len(jobs)}")


def cmd_otbor_shadow_report(args: argparse.Namespace) -> None:
    """Отчёт по проверке «Отбора» на реальных данных (#134), только чтение: разделы отказа, категории, технологии из
    сбора на радар, гейт против «Отбора» с примерами, повод публикации. --json — всё как есть."""
    from oiltech_digest import otbor_shadow

    report = otbor_shadow.report(date.fromisoformat(args.since), examples=args.examples)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return
    print(f"otbor-shadow-report: статьи с {report['since']}: строк {report['publications']}, решено "
          f"{report['decided']}, сбоев {sum(report['failed'].values())} {report['failed'] or ''}".rstrip())
    print("  инструкция / справочники: " + "; ".join(f"{key}: {count}" for key, count in report["versions"].items()))
    print("  гейт против «Отбора» (строка: гейт, столбцы: «Отбор» брать / не брать):")
    for side, label in (("accept", "гейт принял"), ("reject", "гейт отклонил")):
        cell = report["gate"].get(side) or {"accept": 0, "reject": 0}
        print(f"    {label}: брать {cell['accept']}, не брать {cell['reject']}")
    print("  принятое по категориям: " + ", ".join(f"{key}: {count}" for key, count in report["categories"].items()))
    print(f"  технологий из сбора на радар: {report['technology_from_feed']} ({report['technology_per_week']} в неделю);"
          f" радар находил до окна {report['radar_before_per_week']} в неделю, за окно {report['radar_now']}")
    print(f"  разделы отказа (спорных решений всего {report['disputed']}):")
    for row in report["reasons"]:
        print(f"    {row['label']}: {row['count']}")
    print("  повод публикации (брать / не брать):")
    for row in report["occasions"]:
        print(f"    {row['label']}: {row['accept']} / {row['reject']}")
    for key, title in (("gate_accepted_otbor_rejected", "гейт принял, «Отбор» не взял"),
                       ("gate_rejected_otbor_accepted", "гейт отклонил, «Отбор» взял")):
        print(f"  примеры: {title}")
        for row in report["examples"][key]:
            why = row["reason"] if key == "gate_accepted_otbor_rejected" else row["category"]
            print(f"    {row['article_id']} {row['title']} ({row['source']}): {why}; {row['justification'] or ''}")


def cmd_otbor_switch(args: argparse.Namespace) -> None:
    """Переключение витрин на «Отбор» (#134): решения проверки на реальных данных в сигналы, принятые идут дальше по
    конвейеру. По умолчанию сухой прогон: что перенесётся и куда, что пропущено, что дальше и почём; --json — то же с
    номерами статей (поимённый список для дампа и согласования). --apply только при включённой «Карточке», когда задачи
    проверки дошли и у всех статей окна, решённых гейтом, есть решение проверки."""
    from oiltech_digest import otbor, otbor_shadow
    from oiltech_digest.otbor_pack_format import load_dictionaries as load_otbor_dictionaries

    try:
        result = otbor_shadow.switch(date.fromisoformat(args.since), apply=args.apply)
    except otbor_shadow.SwitchError as exc:
        raise SystemExit(f"otbor-switch: {exc}") from exc
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    def price(value):
        return f"около {_usd(value)}" if value is not None else "цены на этой базе нет"

    ids = result["ids"]
    print(f"otbor-switch: решения проверки с {result['since']}: перенести {result['transfer']}: в бизнес-ленту "
          f"{len(ids['business'])}, на радар {len(ids['technology'])}, «Другое» {len(ids['other'])}, отклонено "
          f"{len(ids['rejected'])}")
    if result["versions"]:
        print("  инструкция / справочники решений: "
              + "; ".join(f"{key}: {count}" for key, count in result["versions"].items())
              + f" (сейчас {otbor.PROMPT_VERSION} / {load_otbor_dictionaries().get('version')})")
    print(f"  пропущено: сбой проверки {len(result['failed'])}, решение человека, закрытый месяц или иное после "
          f"проверки {len(result['not_eligible'])}")
    print(f"  статей окна, решённых гейтом, без решения проверки {len(result['missing'])}"
          + (": перенос откажет, сначала otbor-shadow --apply" if result["missing"] else "")
          + (f"; в идущих задачах проверки {result['pending']}" if result["pending"] else ""))
    print(f"  дальше конвейер: карточка заново {result['cards']} ({price(result['card_cost_usd'])}), баллы второго "
          f"вызова {result['scores']} ({price(result['scores_cost_usd'])}), суть и перевод ленты принятым без сути "
          f"{result['feed_stages']}")
    if not result["applied"]:
        print("  сухой прогон: ничего не перенесено (перенести: --apply; номера статей: --json)")
        return
    print(f"  перенесено {len(result['switched'])}, не подошло в момент переноса {len(result['skipped'])}; задач "
          f"«Карточки» заново {result['card_jobs']}")


def cmd_enqueue_signal_summary(args: argparse.Namespace) -> None:
    """Шаг планировщика «Суть и перевод» сигналов (#131): находки и материалы, принятые «Отбором», без сути или
    русского заголовка ставит в задачи по полосе ежедневной обработки. С --ids: названные
    сигналы по полосе пересчётов (повтор после сбоя модели), только принятые «Отбором»."""
    from oiltech_digest import signal_summary

    if args.ids:
        result = signal_summary.enqueue_named([int(value) for value in args.ids.split(",") if value.strip()])
    else:
        result = signal_summary.scheduler_step()
    queues = sorted({job["queue_name"] for job in result["jobs"]})
    print(f"enqueue-signal-summary: сигналов {result['signals']}, задач {len(result['jobs'])}"
          + (f", очередь {', '.join(queues)}" if queues else ""))
    if result.get("skipped"):
        print(f"  пропущены (не приняты «Отбором», дубль или нет такого): {', '.join(map(str, result['skipped']))}")


def cmd_enqueue_signal_scores(args: argparse.Namespace) -> None:
    """Шаг планировщика «баллы» — второй вызов «Карточки» (#132): сигналы после основного вызова без баллов и с
    баллами, которые «пересчитываются» после «не тот блок», ставит в задачи по полосе ежедневной обработки. Модель:
    SIGNAL_SCORES_MODEL. С --ids: названные сигналы по полосе пересчётов."""
    from oiltech_digest import signal_scores

    try:
        if args.ids:
            result = signal_scores.enqueue_named([int(value) for value in args.ids.split(",") if value.strip()])
        else:
            result = signal_scores.scheduler_step()
    except signal_scores.ScoresSetupError as exc:
        raise SystemExit(f"enqueue-signal-scores: {exc}") from exc
    queues = sorted({job["queue_name"] for job in result["jobs"]})
    print(f"enqueue-signal-scores: сигналов {result['signals']}, задач {len(result['jobs'])}"
          + (f", очередь {', '.join(queues)}" if queues else ""))
    if result.get("skipped"):
        print(f"  пропущены (не оцениваются: до «Отбора», отклонённые, «другое», без карточки или нет такого): "
              f"{', '.join(map(str, result['skipped']))}")


def cmd_enqueue_signal_card_history(args: argparse.Namespace) -> None:
    """Прогон по истории агента «Карточка сигнала» (#98): лента с августа 2026, весь радар и материалы коллег (дальше
    «Отбора» — только принятое им) — по полосе пересчётов, сначала текущее окно ленты, радар и материалы. --since
    ставит одну границу всей истории. По умолчанию сухой прогон: число сигналов и цена."""
    from oiltech_digest import signal_card, signal_card_history

    try:
        plan = signal_card_history.plan(date.fromisoformat(args.since) if args.since else None)
    except signal_card.CardSetupError as exc:
        raise SystemExit(f"enqueue-signal-card-history: {exc}") from exc
    estimate, count = plan["estimate"], len(plan["first"]) + len(plan["rest"])
    radar = (f"радар и материалы коллег с {plan['radar_since']} (по дате появления сигнала)" if plan["radar_since"]
             else "весь радар и материалы коллег")
    print(f"enqueue-signal-card-history: модель {plan['model']}, инструкция {plan['prompt_version']}, "
          f"справочники {plan['dictionaries_version']}")
    print(f"  история — лента с {plan['since']} (по дате сбора статьи), {radar}, дальше "
          f"«Отбора» — только принятое им, без дублей: сигналов {plan['signals']}; заполнены этим рецептом — "
          f"{plan['filled']}, уже в задачах агента — {plan['pending']}, без главной публикации — "
          f"{plan['no_publication']}")
    print(f"  к заполнению {count}: сначала текущее окно ленты ({', '.join(plan['months'])}), радар и материалы — "
          f"{len(plan['first'])}, затем остальное — {len(plan['rest'])}")
    print(f"  цена ≈ {_usd(estimate['cost_usd'])}: вызовов {estimate['calls']}, токенов ≈ "
          f"{estimate['input_tokens'] // 1000} тыс. входа и {estimate['output_tokens'] // 1000} тыс. выхода "
          f"(оценка: ответ — {signal_card.ESTIMATED_OUTPUT_TOKENS} токенов, знаков на токен — {signal_card.CHARS_PER_TOKEN})")
    print(f"  полоса {plan['queue']}: задач {-(-count // plan['batch'])} по {plan['batch']} сигналов")
    if not plan["bulk_lane"]:
        print("  ВНИМАНИЕ: полоса пересчётов выключена (AI_BULK_LANE_ENABLED) — задачи встали бы в поток дня перед "
              "новыми статьями; --apply откажет")
    if not args.apply:
        print("  сухой прогон — задачи не поставлены и не оплачены (поставить: --apply)")
        return
    try:
        jobs = signal_card_history.enqueue(plan)
    except signal_card.CardSetupError as exc:
        raise SystemExit(f"enqueue-signal-card-history: {exc}") from exc
    print(f"  поставлено задач {len(jobs)}")


def cmd_signals(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    rows = repository.list_signals(maturity=args.maturity, theme=args.theme, limit=args.limit)
    if args.json:
        payload = []
        for row in rows:
            evidence = repository.list_signal_evidence(int(row["id"]), limit=args.evidence_limit)
            payload.append({**row, "evidence": evidence})
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    if not rows:
        print("signals: пусто")
        return
    for row in rows:
        print(
            f"#{row['id']} [{row['maturity']}] score={float(row['score']):.1f} "
            f"evidence={row['evidence_count']} theme={row['theme']}"
        )
        print(f"  {row['title']}")
        if row.get("thesis"):
            print(f"  {str(row['thesis'])[:220]}")
        for evidence in repository.list_signal_evidence(int(row["id"]), limit=args.evidence_limit):
            print(f"    - {evidence.get('publisher') or 'source'}: {evidence['source_url']}")


def cmd_signal_feedback(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    event_id = repository.record_signal_feedback_event(
        args.article_id,
        args.event_type,
        signal_id=args.signal_id,
        signal_evidence_id=args.signal_evidence_id,
        source_url=args.source_url,
        signal_title=args.signal_title,
        user_id=args.user_id,
        old_value=args.old_value,
        new_value=args.new_value,
        comment=args.comment,
        verdict=args.verdict,
        reason=args.reason,
        corrected_title=args.corrected_title,
        corrected_thesis=args.corrected_thesis,
        duplicate_of_signal_id=args.duplicate_of_signal_id,
    )
    target = args.article_id or args.signal_id or args.source_url
    print(f"signal-feedback: id={event_id} target={target} event={args.event_type}")


def cmd_import_signal_feedback(args: argparse.Namespace) -> None:
    from oiltech_digest.signal_feedback import import_signal_feedback_csv

    result = import_signal_feedback_csv(args.path, user_id=args.user_id, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    print(
        "import-signal-feedback: "
        f"rows={result['rows']} events={result['feedback_events']} "
        f"memories={result['memories']} dry_run={result['dry_run']}"
    )


def cmd_refresh_signal_evidence_counts(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    changed = repository.refresh_all_signal_evidence_counts()
    print(f"refresh-signal-evidence-counts: исправлено карточек {changed}")


VERDICT_RU = {
    "strong_signal": "сильный сигнал", "approved": "полезный", "watch_later": "наблюдать",
    "background_material": "фон", "reject": "шум", "wrong_domain": "не релевантно",
    "wrong_block": "не тот блок", "merge_duplicate": "дубль", "overrated": "оценка завышена",
}


def cmd_radar_weekly_report(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    report = repository.radar_weekly_summary(days=args.days)
    print(f"Технологический радар — итоги за {report['days']} дн.")
    print(f"На радаре новых: {report['on_radar']}; отсеяно и скрыто: {report['filtered']}; "
          f"ждут разбора: {report['awaiting_review']}; отзывов: {report['feedback']}")
    if report["top"]:
        print("Лучшие новые:")
        for row in report["top"]:
            print(f"  #{row['id']} {float(row['score'] or 0):.0f} — {row['title']} ({row['theme']})")
    if report["filtered_reasons"]:
        print("Почему отсеяно:")
        for row in report["filtered_reasons"]:
            print(f"  {row['reason']}: {row['count']}")
    if report["feedback_verdicts"]:
        print("Отзывы: " + ", ".join(f"{VERDICT_RU.get(row['verdict'], row['verdict'])} — {row['count']}"
                                     for row in report["feedback_verdicts"]))


def cmd_import_reference_signals(args: argparse.Namespace) -> None:
    from oiltech_digest import signal_reference

    rows = signal_reference.read_reference(args.path, sheets=args.sheet or None)
    result = signal_reference.import_reference(rows, apply=args.apply)
    print(f"import-reference-signals: строк {result['rows']}, листы {', '.join(result['sheets'])}, "
          f"applied={result['applied']}")
    for row in rows[:50]:
        print(f"  [{row['sheet']}] {row['title'][:90]}")
    if not args.apply:
        print("Сухой прогон: записать в память радара — тот же вызов с --apply.")


def cmd_radar_recall(args: argparse.Namespace) -> None:
    from oiltech_digest import signal_reference
    from oiltech_digest.db import repository

    rows = signal_reference.read_reference(args.path, sheets=args.sheet or None)
    result = signal_reference.recall(rows, repository.radar_cards_for_recall())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    print(f"radar-recall: найдено {result['found']} из {result['total']} ({result['share']:.0%}), "
          f"из них на экране {result['found_visible']}")
    for item in result["found_items"]:
        mark = "" if item["visible"] else " [скрыта]"
        print(f"  + #{item['signal_id']}{mark} {item['title'][:90]}")
    for item in result["missing_items"]:
        print(f"  - {item['title'][:90]}")


def cmd_radar_search_trial(args: argparse.Namespace) -> None:
    """Пробный прогон широкого поиска (#138): сухой прогон по умолчанию — темы, вызовы и цена, ничего не пишет; --run —
    один прогон, находки пройдут «Отбор», в витрины не попадёт ничего; --report N — отчёт по задаче N."""
    from oiltech_digest import config, signal_reference, signal_search_trial

    try:
        if args.report:
            reference = (signal_reference.read_reference(args.reference, sheets=args.sheet or None)
                         if args.reference else None)
            result = signal_search_trial.report(args.report, reference=reference)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            else:
                _print_search_trial_report(result)
            return
        until = date.fromisoformat(args.until) if args.until else date.today()
        since = date.fromisoformat(args.since) if args.since else until - timedelta(days=config.SIGNAL_RESEARCH_DAYS)
        trial = signal_search_trial.plan(since=since, until=until, per_topic=args.per_topic, model=args.model,
                                         reasoning=args.reasoning, otbor_model=args.otbor_model,
                                         otbor_reasoning=args.otbor_reasoning)
        cost = signal_search_trial.estimate(trial)
        job = signal_search_trial.enqueue(trial) if args.run else None
    except signal_search_trial.TrialError as exc:
        raise SystemExit(f"radar-search-trial: {exc}") from exc
    print(f"radar-search-trial{'' if job else ' [сухой прогон]'}: тем {len(trial['topics'])}, период с {trial['since']} "
          f"по {trial['until']}, поиск {trial['model']} {trial['reasoning']}, до {trial['per_topic']} материалов на "
          f"тему, «Отбор» {trial['otbor_model']} {trial['otbor_reasoning']}")
    print("  Темы: " + "; ".join(trial["topics"]))
    print(f"  Вызовов поиска {cost['search_calls']} (до {cost['search_calls_max']} с повтором пустого ответа), "
          f"«Отбора» от {cost['otbor_calls'][0]} до {cost['otbor_calls'][1]}")
    print(f"  Оценка цены, USD: поиск от {cost['search_usd'][0]} до {cost['search_usd'][1]}, «Отбор» от "
          f"{cost['otbor_usd'][0]} до {cost['otbor_usd'][1]}, всего от {cost['total_usd'][0]} до {cost['total_usd'][1]}"
          + (" (вызов поиска по прошлым прогонам радара)" if cost["history"] else " (вызов поиска по замеру 18.10)"))
    if job is None:
        print("Ничего не записано. Запуск: тот же вызов с --run.")
        return
    print(f"Задача id={job['id']}, очередь {job['queue_name']}. Находки пройдут «Отбор» после поиска; в витрины не "
          f"попадёт ничего. Отчёт: radar-search-trial --report {job['id']} [--reference эталон.xlsx]")


def _print_search_trial_report(result: dict) -> None:
    otbor = result["otbor"]
    print(f"radar-search-trial: задача {result['job_id']} ({result['status']}), поиск {result['model']}, до "
          f"{result['per_topic']} на тему, период с {result['trial'].get('since')} по {result['trial'].get('until')}")
    for topic in result["topics"]:
        print(f"  {topic['topic']}: принёс поиск {topic['found']}, технический отсев {topic['dropped']}, ссылок "
              f"{topic['links']}, из них в сборе {topic['already_collected']}, новых находок {topic['new']}, «Отбор» "
              f"принял {topic['accepted']}, отклонил {topic['rejected']}, найдено снова {topic['found_again']}"
              + (f", сбой: {topic['error']}" if topic.get("error") else ""))
    print(f"Материалов (ссылок после технических проверок) {result['materials']}, уже в сборе "
          f"{result['already_collected']} ({result['already_collected_share']:.0%}), новых находок "
          f"{result['new_findings']}, сигналов платформы найдено снова {result['found_again']}")
    print(f"«Отбор»: решил {otbor['decided']}, принял {otbor['accepted']} ({otbor['accepted_share']:.0%}), сбоев "
          f"{otbor['failed']}, ждут решения {otbor['waiting']}, спорных {otbor['disputed']}")
    print("  Принятое по категориям: " + (", ".join(f"{key} {value}" for key, value in otbor["by_category"].items())
                                          or "нет"))
    print("  Причины отказа: " + (", ".join(f"{key} {value}" for key, value in otbor["reject_reasons"].items())
                                  or "нет"))
    titles = result["russian_titles"]
    print(f"Русский заголовок страницы у принятых: {titles['russian']} из {titles['accepted']} ({titles['share']:.0%})")
    for title in titles["examples"]:
        print(f"  {title}")
    cost = result["cost_usd"]
    print("Фактическая цена, USD: " + ", ".join(f"{stage} {value}" for stage, value in cost.items()))
    recall = result.get("recall")
    if recall:
        print(f"Полнота по эталону: поиск {recall['search']} из {recall['total']} ({recall['search_share']:.0%}), сбор "
              f"{recall['collection']}, поиск или сбор {recall['either']} ({recall['either_share']:.0%})")
        for direction, counts in recall["by_direction"].items():
            print(f"  {direction}: поиск {counts['search']} из {counts['total']}, поиск или сбор {counts['either']}")
        for item in recall["items"]:
            if item["search"]:
                print(f"  + поиск{'' if item['collection'] else ', только поиск'}: {item['title'][:100]}")
            elif item["collection"]:
                print(f"  = только сбор: {item['title'][:100]}")
        for title in recall["missed"]:
            print(f"  - не нашёл никто: {title[:100]}")


def _iso_date(value: str) -> str:
    """День ГГГГ-ММ-ДД из командной строки: опечатка — ошибка разбора, а не трассировка базы."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"нужна дата ГГГГ-ММ-ДД, получено {value!r}") from exc


def cmd_signal_card_expert_pack(args: argparse.Namespace) -> None:
    from oiltech_digest import signal_card_pack

    try:
        result = signal_card_pack.export_pack(args.out, seed=args.seed, since=args.since, until=args.until,
                                              apply=args.apply, ids_file=args.ids_file,
                                              overrides_file=args.overrides)
    except (signal_card_pack.PackError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"signal-card-expert-pack: {exc}") from exc
    origin = result["composition"]["origin"]
    print(f"signal-card-expert-pack: сигналов {result['count']} — лента {origin.get('feed', 0)}, "
          f"радар {origin.get('radar', 0)}; отбор: {result['selection']['rule']}"
          + (f" ({result['selection']['ids_file']})" if result["selection"].get("ids_file") else "")
          + (f", поправки: {result['selection']['overrides_file']}" if result["selection"].get("overrides_file") else ""))
    print("Доли: " + ", ".join(f"{signal_card_pack.STRATUM_RU[stratum]} — {count}"
                               for stratum, count in result["composition"]["stratum"].items()))
    for key, title in (("category", "Категории"), ("theme", "Темы")):
        print(f"{title}: " + ", ".join(f"{name} — {count}" for name, count in result["composition"][key].items()))
    if result["shortfall"]:
        print("Для экзамена не хватает: " + "; ".join(result["shortfall"]))
    print(f"Параметры выборки: --seed {result['seed']} --since {result['since']} --until {result['until']} "
          "(тот же состав — пока не менялись баллы и отметки статей окна; точный состав — в манифесте)")
    if result["applied"]:
        print(f"Таблица: {result['workbook']}\nМанифест: {result['manifest']}")
    else:
        print("Сухой прогон: записать таблицу и манифест — тот же вызов с --apply и --out вне репозитория.")


def cmd_signal_card_exam(args: argparse.Namespace) -> None:
    """Экзамен агента «Карточка сигнала» (#98) на разметке эксперта: модели лестницей по цене."""
    from oiltech_digest import signal_card, signal_card_exam
    from oiltech_digest.signal_card_exam_report import PART_RU
    from oiltech_digest.signal_card_pack import STRATUM_RU

    if args.rescore and (args.dry_run or args.models or args.runs is not None or args.part):
        raise SystemExit("signal-card-exam: --rescore берёт модели, прогоны и часть из отчёта — без --dry-run, "
                         "--models, --runs и --part")
    models = tuple(model.strip() for model in (args.models or "").split(",") if model.strip()) or signal_card_exam.MODELS
    runs = signal_card_exam.RUNS if args.runs is None else args.runs
    options = {"manifest_path": args.manifest, "models": models, "runs": runs, "part": args.part or "exam"}
    try:
        if args.rescore:
            result = signal_card_exam.rescore(args.rescore, args.markup, manifest_path=args.manifest)
        elif args.dry_run:
            plan = signal_card_exam.plan(args.markup, **options)
        else:
            result = signal_card_exam.run(args.markup, **options)
    except (signal_card_exam.ExamError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"signal-card-exam: {exc}") from exc
    if args.rescore or not args.dry_run:
        decision = result["decision"]
        print("signal-card-exam: решение — " + (f"{decision['model']} — " if decision["model"] else "")
              + decision["reason"])
        print(f"Отчёт: {result['readable']}\nОтветы и метрики: {result['report']}")
        return
    print("signal-card-exam: сухой прогон — ничего не записано и не оплачено")
    print(f"Набор: строк {plan['rows']}; деление " + ("новое — запишется при первом прогоне" if plan["split_new"]
                                                    else f"из {signal_card_exam.SPLIT_NAME}"))
    for key in ("tuning", "exam"):
        counts = plan["split"][key]
        print(f"  {PART_RU[key]} {sum(counts.values())}: " + ", ".join(
            f"{STRATUM_RU.get(stratum, stratum)} {count}" for stratum, count in counts.items()))
    print(f"Часть «{PART_RU[plan['part']]}»: сигналов {plan['signals']}, "
          f"прогонов на сигнал {plan['runs']}")
    for number, one in enumerate(plan["models"]):
        print(f"  {one['model']}: вызовов {one['calls']}, токенов ≈ {one['input_tokens'] // 1000} тыс. входа и "
              f"{one['output_tokens'] // 1000} тыс. выхода, ≈ {_usd(one['cost_usd'])}"
              + (" — если не пройдёт предыдущая" if number and plan["part"] == "exam" else ""))
    total = sum(one["cost_usd"] for one in plan["models"])
    print(("Не больше" if plan["part"] == "exam" else "Всего") + f": вызовов {sum(one['calls'] for one in plan['models'])}, "
          f"≈ {_usd(total)} (оценка: ответ — {signal_card.ESTIMATED_OUTPUT_TOKENS} токенов, "
          f"знаков на токен — {signal_card.CHARS_PER_TOKEN})")


def cmd_signal_card_exam_judge(args: argparse.Namespace) -> None:
    """Значения судьи радара у радарных строк манифеста (#98) — JSON в stdout: экзамен идёт там, где есть
    модель, а база — на ядре. Манифест — путь или «-» (stdin)."""
    from oiltech_digest import signal_card_exam

    try:
        raw = sys.stdin.read() if args.manifest == "-" else Path(args.manifest).read_text(encoding="utf-8")
        values = signal_card_exam.judge_values(json.loads(raw))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"signal-card-exam-judge: {exc}") from exc
    print(json.dumps(values, ensure_ascii=False, indent=1))


def cmd_otbor_expert_pack(args: argparse.Namespace) -> None:
    """Пакет эксперту для «Отбора» (#129): справочники и линейки на утверждение, разметка вслепую."""
    from oiltech_digest import otbor_pack

    try:
        result = otbor_pack.export_pack(args.out, seed=args.seed, until=args.until, apply=args.apply,
                                        card_manifest=args.card_manifest, card_markup=args.card_markup)
    except (otbor_pack.PackError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"otbor-expert-pack: {exc}") from exc
    groups, kinds = result["composition"]["group"], result["composition"]["kind"]
    parts = []
    for group, label in otbor_pack.GROUPS.items():
        detail = ", ".join(f"{otbor_pack.FILTERED_KINDS[kind]} {count}" for kind, count in kinds.items())
        parts.append(f"{label} {groups.get(group, 0)}" + (f" ({detail})" if group == "radar_filtered" and detail else ""))
    print(f"otbor-expert-pack: строк {result['count']}" + ("" if result["applied"] else ", сухой прогон"))
    print("Группы: " + "; ".join(parts))
    print(f"Окно ленты: с {result['since']} по {result['until']}, последний день не входит; зерно {result['seed']}")
    if result["shortfall"]:
        print("Не хватает: " + "; ".join(result["shortfall"]))
    if result["blockers"]:
        print("Мешает записи: " + "; ".join(result["blockers"]))
    if result["applied"]:
        print(f"Таблица: {result['workbook']}\nМанифест: {result['manifest']}")
    else:
        print("Записать таблицу и манифест: тот же вызов с --apply и --out вне репозитория.")


def cmd_otbor_markup_check(args: argparse.Namespace) -> None:
    """Проверка размеченного пакета «Отбора» (#129): отчёт о нарушениях; есть нарушения — код выхода не 0."""
    from oiltech_digest import otbor_markup
    from oiltech_digest.otbor_pack_format import PackError

    try:
        result = otbor_markup.check(args.markup, args.manifest)
    except (PackError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"otbor-markup-check: {exc}") from exc
    violations, approvals = result["violations"], result["approvals"]
    print(f"otbor-markup-check: строк {result['rows']}, нарушений {len(violations)}")
    for line in violations:
        print(line)
    print(f"Утверждение: «да» {approvals['approved']}, правки {len(approvals['changes'])}, "
          f"без решения {approvals['pending']}")
    for line in approvals["changes"]:
        print(line)
    if approvals["missing"]:
        print("Не найдено: " + "; ".join(approvals["missing"]))
    for note in result["versions"]:
        print(f"С выгрузки пакета сменились {note}")
    if violations:
        raise SystemExit(f"otbor-markup-check: разметку нужно поправить, нарушений {len(violations)}")


def cmd_otbor_exam(args: argparse.Namespace) -> None:
    """Экзамен агента «Отбор» (#133) на разметке эксперта: модели лестницей по цене."""
    from oiltech_digest import otbor_exam, signal_card
    from oiltech_digest.otbor_exam_report import PART_RU

    if args.rescore and (args.dry_run or args.models or args.runs is not None or args.part or args.all):
        raise SystemExit("otbor-exam: --rescore берёт модели, прогоны и часть из отчёта, без --dry-run, --models, "
                         "--runs, --part и --all")
    if args.workers < 1:
        raise SystemExit(f"otbor-exam: --workers {args.workers}: параллельных вызовов модели от одного")
    models = tuple(model.strip() for model in (args.models or "").split(",") if model.strip()) or otbor_exam.MODELS
    runs = otbor_exam.RUNS if args.runs is None else args.runs
    options = {"manifest_path": args.manifest, "models": models, "runs": runs, "part": args.part or "exam"}
    try:
        if args.rescore:
            result = otbor_exam.rescore(args.rescore, args.markup, manifest_path=args.manifest)
        elif args.dry_run:
            plan = otbor_exam.plan(args.markup, **options)
        else:
            result = otbor_exam.run(args.markup, **options, every=args.all, workers=args.workers)
    except (otbor_exam.ExamError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"otbor-exam: {exc}") from exc
    if args.rescore or not args.dry_run:
        decision = result["decision"]
        print("otbor-exam: решение: " + (f"{decision['model']}, " if decision["model"] else "") + decision["reason"])
        print(f"Отчёт: {result['readable']}\nОтветы и метрики: {result['report']}")
        return
    print("otbor-exam: сухой прогон, ничего не записано и не оплачено")
    print(f"Набор: строк {plan['rows']}; деление " + ("новое, запишется при первом прогоне" if plan["split_new"]
                                                    else f"из {otbor_exam.SPLIT_NAME}"))
    print("Утверждено: " + (plan["approved"] or f"нет, без отметки approved в {otbor_exam.ROWS_NAME} прогона не будет"))
    for key in ("tuning", "exam"):
        counts = plan["split"][key]
        print(f"  {PART_RU[key]} {sum(counts.values())}: " + "; ".join(
            f"{otbor_exam.stratum_label(stratum)} {count}" for stratum, count in counts.items()))
    print(f"Часть «{PART_RU[plan['part']]}»: публикаций {plan['publications']}, прогонов на публикацию {plan['runs']}")
    for number, one in enumerate(plan["models"]):
        print(f"  {one['model']}: вызовов {one['calls']}, токенов ≈ {one['input_tokens'] // 1000} тыс. входа и "
              f"{one['output_tokens'] // 1000} тыс. выхода, ≈ {_usd(one['cost_usd'])}"
              + (", если не пройдёт предыдущая" if number and plan["part"] == "exam" else ""))
    total = sum(one["cost_usd"] for one in plan["models"])
    calls = sum(one["calls"] for one in plan["models"])
    print(("Не больше" if plan["part"] == "exam" else "Всего") + f": вызовов {calls}, "
          f"≈ {_usd(total)} (оценка: ответ {signal_card.ESTIMATED_OUTPUT_TOKENS} токенов, знаков на токен "
          f"{signal_card.CHARS_PER_TOKEN}; повтор при конфликте шагов не учтён)")


def cmd_otbor_exam_control(args: argparse.Namespace) -> None:
    """Размеры страт ленты в окне пакета и категория судьи у радарных строк манифеста экзамена «Отбора» (#133) —
    JSON в stdout: экзамен идёт там, где есть модель, а база — на ядре. Манифест — путь или «-» (stdin)."""
    from oiltech_digest import otbor_exam

    try:
        raw = sys.stdin.read() if args.manifest == "-" else Path(args.manifest).read_text(encoding="utf-8")
        values = otbor_exam.control_values(json.loads(raw))
    except (otbor_exam.ExamError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"otbor-exam-control: {exc}") from exc
    print(json.dumps(values, ensure_ascii=False, indent=1))


def cmd_card_scores_exam(args: argparse.Namespace) -> None:
    """Экзамен «Карточки» (#133) на разметке эксперта: тема основного вызова, модели второго вызова по цене."""
    from oiltech_digest import card_scores_exam

    if args.rescore and (args.dry_run or args.models or args.runs is not None or args.part or args.all
                         or args.card_model or args.summary_model):
        raise SystemExit("card-scores-exam: --rescore берёт модели, прогоны и часть из отчёта, без --dry-run, "
                         "--models, --card-model, --summary-model, --runs, --part и --all")
    if args.workers < 1:
        raise SystemExit(f"card-scores-exam: --workers {args.workers}: параллельных вызовов модели от одного")
    models = (tuple(model.strip() for model in (args.models or "").split(",") if model.strip())
              or card_scores_exam.MODELS)
    options = {"manifest_path": args.manifest, "models": models, "part": args.part or "exam",
               "runs": card_scores_exam.RUNS if args.runs is None else args.runs, "card_model": args.card_model,
               "summary_model": args.summary_model}
    try:
        if args.rescore:
            result = card_scores_exam.rescore(args.rescore, args.markup, manifest_path=args.manifest,
                                              card_markup=args.card_markup, theme_before=args.theme_before)
        elif args.dry_run:
            _print_scores_plan(card_scores_exam.plan(args.markup, **options))
            return
        else:
            result = card_scores_exam.run(args.markup, **options, every=args.all, workers=args.workers,
                                          card_markup=args.card_markup, theme_before=args.theme_before)
    except (card_scores_exam.ExamError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"card-scores-exam: {exc}") from exc
    decision = result["decision"]
    print("card-scores-exam: решение: " + (f"{decision['model']}, " if decision["model"] else "") + decision["reason"])
    print(f"Отчёт: {result['readable']}\nОтветы и метрики: {result['report']}")


def _print_scores_plan(plan: dict) -> None:
    from oiltech_digest import card_scores_exam, otbor_exam, signal_card
    from oiltech_digest.card_scores_exam_report import PART_RU

    print("card-scores-exam: сухой прогон, ничего не записано и не оплачено")
    print("Набор: деление " + ("новое, запишется при первом прогоне" if plan["split_new"]
                               else f"из {otbor_exam.SPLIT_NAME}"))
    print("Утверждено: " + (plan["approved"] or f"нет, без отметки {card_scores_exam.APPROVAL_KEY} в "
                                               f"{otbor_exam.ROWS_NAME} экзамена не будет"))
    print(f"Часть «{PART_RU[plan['part']]}»: строк пакета {plan['pack']} (бизнес {plan['profiles']['business']}, "
          f"технология {plan['profiles']['tech_radar']}), «сильных сверх пакета» {plan['extra']}, прогонов на строку "
          f"{plan['runs']}")
    for title, one in (("Суть", plan["summary"]), ("Основной вызов", plan["card"])):
        print(f"{title} ({one['model']}): вызовов {one['calls']}, ≈ {_usd(one['cost_usd'])}"
              + ("" if one["calls"] else ", всё уже лежит рядом с манифестом"))
    print("Второй вызов, лестница по цене:")
    for number, one in enumerate(plan["models"]):
        print(f"  {one['model']}: вызовов {one['calls']}, ≈ {_usd(one['cost_usd'])}"
              + (", если не пройдёт предыдущая" if number and plan["part"] == "exam" else ""))
    stages = [plan["summary"], plan["card"], *plan["models"]]
    print(("Не больше" if plan["part"] == "exam" else "Всего") + f": вызовов {sum(one['calls'] for one in stages)}, "
          f"≈ {_usd(sum(one['cost_usd'] for one in stages))} (оценка: ответ {signal_card.ESTIMATED_OUTPUT_TOKENS} "
          f"токенов, знаков на токен {signal_card.CHARS_PER_TOKEN}; повтор после сбоя не учтён)")


def cmd_card_scores_exam_control(args: argparse.Namespace) -> None:
    """Выгрузка с ядра для экзамена «Карточки» (#133): критерии, теги, контроль, «сильные сверх пакета» — JSON в stdout:
    экзамен идёт там, где есть модель, а база — на ядре. Манифест — путь или «-» (stdin)."""
    from oiltech_digest import card_scores_exam_control

    try:
        raw = sys.stdin.read() if args.manifest == "-" else Path(args.manifest).read_text(encoding="utf-8")
        values = card_scores_exam_control.control_values(json.loads(raw))
    except (card_scores_exam_control.ControlError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"card-scores-exam-control: {exc}") from exc
    print(json.dumps(values, ensure_ascii=False, indent=1))


def _usd(value: float) -> str:
    return f"${value:.2f}" if value >= 1 else f"${value:.3f}"


def cmd_archive_signals(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    ids = [int(value) for value in (args.ids or "").split(",") if value.strip()]
    rows = repository.archive_signal_candidates(
        created_before=args.created_before, free_theme_only=args.free_theme_only, ids=ids or None,
    )
    reviewed = sum(1 for row in rows if row["reviewed"])
    print(f"archive-signals: карточек {len(rows)}, из них разобранных {reviewed}, "
          f"причина «{args.reason}», dry_run={not args.apply}")
    for row in rows:
        mark = " [разобрана]" if row["reviewed"] else ""
        print(f"  #{row['id']} {float(row['score'] or 0):>5.1f} {str(row['theme'])[:30]!r}: {str(row['title'])[:70]}{mark}")
    if args.apply:
        changed = repository.archive_signals([row["id"] for row in rows], reason=args.reason)
        print(f"В архиве: {changed}. Вернуть: unarchive-signals --reason {args.reason!r}")


def cmd_sync_feed_signals(args: argparse.Namespace) -> None:
    """Сигналы ленты для принятых статей по дате сбора (#95) — без ИИ, повторный запуск
    дублей не создаёт. По умолчанию сухой прогон: те же числа, запись откатывается."""
    from oiltech_digest.db import feed_signals

    since = args.since or feed_signals.FEED_SIGNALS_SINCE.isoformat()
    result = feed_signals.sync_feed_signals(
        collected_since=since, collected_until=args.until, dry_run=not args.apply,
    )
    print(f"sync-feed-signals: статьи с {since} по {args.until or 'сейчас'} (по дате сбора), "
          f"dry_run={result['dry_run']}")
    print(f"  новых сигналов: {result['signals_created']}; обновлено: {result['signals_updated']}; "
          f"публикаций записано: {result['publications']}; перепечаток склеено: {result['reprints_merged']}; "
          f"расклеено: {result['reprints_unmerged']}")
    if not args.apply:
        print("Сухой прогон: записать — тот же вызов с --apply.")


def cmd_unarchive_signals(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    ids = [int(value) for value in (args.ids or "").split(",") if value.strip()]
    changed = repository.unarchive_signals(signal_ids=ids or None, reason=args.reason)
    print(f"unarchive-signals: возвращено карточек {changed}")


def cmd_unmerge_signal(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    ok = repository.unmerge_signal(args.signal_id)
    print(f"unmerge-signal: #{args.signal_id} " + ("снова видна на радаре" if ok else "не была скрыта как дубль"))


def cmd_export_signal_training_jsonl(args: argparse.Namespace) -> None:
    from oiltech_digest.signal_training import export_signal_training_jsonl

    result = export_signal_training_jsonl(
        args.path,
        limit=args.limit,
        with_feedback_only=not args.all,
        verdict=args.verdict,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    print(
        "export-signal-training-jsonl: "
        f"examples={result['examples']} path={result['path']} "
        f"with_feedback_only={result['with_feedback_only']}"
    )


def cmd_export_signal_context(args: argparse.Namespace) -> None:
    from oiltech_digest.signal_training import export_signal_context_bundle

    result = export_signal_context_bundle(
        args.path,
        include_rejected=args.include_rejected,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    print(
        "export-signal-context: "
        f"path={result['path']} signals={result['signals']} evidence={result['evidence']} "
        f"memories={result['memories']} feedback_events={result['feedback_events']}"
    )


def cmd_import_signal_context(args: argparse.Namespace) -> None:
    from oiltech_digest.signal_training import import_signal_context_bundle

    result = import_signal_context_bundle(args.path, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    suffix = " [dry-run]" if result["dry_run"] else ""
    print(
        "import-signal-context"
        f"{suffix}: path={result['path']} signals={result['signals']} evidence={result['evidence']} "
        f"memories={result['memories']} feedback_events={result['feedback_events']}"
    )


def cmd_source_quality(args: argparse.Namespace) -> None:
    from oiltech_digest.db import repository

    period_from, period_to = _utc_period_from_days(args.days)
    rows = repository.compute_source_quality_rows(period_from, period_to)
    if args.snapshot:
        saved = repository.snapshot_source_quality(period_from, period_to)
        print(f"source-quality: snapshot saved rows={saved} period_days={args.days}")
    if args.json:
        print(json.dumps(rows[:args.limit], ensure_ascii=False, indent=2, default=str))
        return
    for row in rows[:args.limit]:
        print(
            f"{row['quality_score']:>5}  {row['source_name']}  "
            f"found={row['articles_found']} processed={row['articles_processed']} "
            f"relevant={row['relevant_count']} rejected={row['rejected_count']} "
            f"avg_score={row.get('avg_score') or '-'} digest={row['digest_count']} "
            f"noise={row['noise_count']} dup={row['duplicate_count']}"
        )


def build_parser() -> argparse.ArgumentParser:
    from oiltech_digest.scoring_profiles import ARTICLE_SCORING_PROFILE, SCORING_PRESETS, SCORING_PROFILES

    parser = argparse.ArgumentParser(prog="oiltech_digest.cli", description="OilTech Digest — сбор RSS")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог (DEBUG; адреса запросов urllib3 не пишутся)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="создать схему БД").set_defaults(func=cmd_init_db)
    sub.add_parser("schema-check", help="проверить наличие обязательных таблиц").set_defaults(func=cmd_schema_check)
    sub.add_parser("seed-sources", help="загрузить источники из Excel").set_defaults(func=cmd_seed_sources)

    p_cu = sub.add_parser("create-user", help="создать пользователя админки")
    p_cu.add_argument("--email", required=True)
    p_cu.add_argument("--password", required=True)
    p_cu.add_argument("--admin", action="store_true", help="назначить администратором")
    p_cu.set_defaults(func=cmd_create_user)

    p_sr = sub.add_parser("set-role", help="сменить роль пользователя (admin|user)")
    p_sr.add_argument("--email", required=True)
    p_sr.add_argument("--role", choices=["admin", "user"], required=True)
    p_sr.set_defaults(func=cmd_set_role)

    sub.add_parser("list-users", help="список пользователей и ролей").set_defaults(func=cmd_list_users)

    p_ac = sub.add_parser("admission-code", help="выдать учётке новый код допуска и напечатать его")
    p_ac_who = p_ac.add_mutually_exclusive_group(required=True)
    p_ac_who.add_argument("--user-id", type=int, help="номер учётки")
    p_ac_who.add_argument("--email")
    p_ac.set_defaults(func=cmd_admission_code)

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
                        help="повторить попытку для статей со статусом too_short или no_gain")
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

    sub.add_parser("seed-tags", help="загрузить 13 тематик заказчика").set_defaults(func=cmd_seed_tags)
    sub.add_parser(
        "seed-scoring", help="завести критерии по умолчанию в пустые профили скоринга (непустые не трогает)"
    ).set_defaults(func=cmd_seed_scoring)
    p_preset = sub.add_parser(
        "apply-scoring-preset",
        help="поставить именованный набор критериев активным набором профиля (по умолчанию — сухой прогон)")
    p_preset.add_argument("--profile", choices=SCORING_PROFILES, default=ARTICLE_SCORING_PROFILE)
    p_preset.add_argument("--preset", choices=sorted(SCORING_PRESETS), required=True)
    p_preset.add_argument("--apply", action="store_true", help="записать одной транзакцией")
    p_preset.set_defaults(func=cmd_apply_scoring_preset)
    sub.add_parser("apply-source-overrides", help="применить playwright/listing-оверрайды источников").set_defaults(func=cmd_apply_source_overrides)

    def add_ai_args(p):
        p.add_argument("--limit", type=int, default=20)
        p.add_argument("--offline", action="store_true", help="детерминированная локальная заглушка без OpenAI API")

    p_summary = sub.add_parser("summarize", help="сформировать AI-суть статей")
    add_ai_args(p_summary)
    p_summary.set_defaults(func=cmd_summarize)

    p_translate = sub.add_parser("translate-titles", help="перевести заголовки на русский (отдельная стадия, AI только для иностранных)")
    add_ai_args(p_translate)
    p_translate.set_defaults(func=cmd_translate)

    p_audit_terms = sub.add_parser("audit-terminology", help="найти плохие нефтегазовые термины в сохранённых title_ru/summary")
    p_audit_terms.add_argument("--limit", type=int, default=500)
    p_audit_terms.add_argument("--article-id", type=int, default=None)
    p_audit_terms.add_argument("--show", type=int, default=30)
    p_audit_terms.add_argument("--json", action="store_true")
    p_audit_terms.set_defaults(func=cmd_audit_terminology)

    p_repair_terms = sub.add_parser("repair-terminology", help="исправить плохие нефтегазовые термины в сохранённых title_ru/summary без OpenAI")
    p_repair_terms.add_argument("--limit", type=int, default=500)
    p_repair_terms.add_argument("--article-id", type=int, default=None)
    p_repair_terms.add_argument("--show", type=int, default=30)
    p_repair_terms.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    p_repair_terms.add_argument("--scripts-only", action="store_true",
                                help="только двойники и склейка алфавитов (без замен словаря) — для всего корпуса")
    p_repair_terms.add_argument("--json", action="store_true")
    p_repair_terms.set_defaults(func=cmd_repair_terminology)

    p_validate_terms = sub.add_parser("validate-terminology", help="проверить нефтегазовый словарь, regex и golden-кейсы")
    p_validate_terms.add_argument("--json", action="store_true")
    p_validate_terms.set_defaults(func=cmd_validate_terminology)

    p_eval_terms = sub.add_parser("eval-terminology", help="прогнать оценку нефтегазовой терминологии и собрать отчет")
    p_eval_terms.add_argument("--limit", type=int, default=None, help="не больше N случаев (по умолчанию все)")
    p_eval_terms.add_argument("--show", type=int, default=30)
    p_eval_terms.add_argument("--csv-path", default="exports/translation_terminology_100_eval.csv")
    p_eval_terms.add_argument("--markdown-path", default="exports/translation_terminology_work_report.md")
    p_eval_terms.add_argument("--json", action="store_true")
    p_eval_terms.set_defaults(func=cmd_eval_terminology)

    p_resummarize = sub.add_parser("enqueue-resummarize", help="перегенерировать суть у статей со словами из двух алфавитов (по умолчанию выборка)")
    p_resummarize.add_argument("--article-id", type=int, action="append", default=None)
    p_resummarize.add_argument("--limit", type=int, default=0)
    p_resummarize.add_argument("--batch-size", type=int, default=20)
    p_resummarize.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    p_resummarize.set_defaults(func=cmd_enqueue_resummarize)

    p_title_edit = sub.add_parser("enqueue-title-edit", help="править русские заголовки принятых статей последних двух "
                                  "недель по правилам эксперта (по умолчанию сухой прогон с ценой)")
    p_title_edit.add_argument("--limit", type=int, default=0, help="потолок числа статей (0 = все)")
    p_title_edit.add_argument("--batch-size", type=int, default=20)
    p_title_edit.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    p_title_edit.add_argument("--allow-live-lane", action="store_true",
                              help="ставить в поток дня, если полоса пересчётов выключена (обычно — нет)")
    p_title_edit.set_defaults(func=cmd_enqueue_title_edit)

    p_tg_titles = sub.add_parser("repair-telegram-titles", help="починить склеенные заголовки Telegram по сохранённым данным (по умолчанию dry-run)")
    p_tg_titles.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    p_tg_titles.add_argument("--before", type=_utc_datetime, default=None,
                             help="только статьи, собранные до этого момента (ISO, UTC) — время выката нового парсера")
    p_tg_titles.add_argument("--show", type=int, default=30)
    p_tg_titles.add_argument("--json", action="store_true")
    p_tg_titles.set_defaults(func=cmd_repair_telegram_titles)

    sub.add_parser(
        "retire-tags",
        help="разово выключить теги вне списка заказчика (смена таксономии)",
    ).set_defaults(func=cmd_retire_tags)

    p_process = sub.add_parser("process", help="суть и перевод заголовка принятых статей (местный конвейер)")
    add_ai_args(p_process)
    p_process.set_defaults(func=cmd_process)

    p_process_full = sub.add_parser("process-full", help="по-статейный конвейер: full-text→суть→перевод заголовка")
    add_ai_args(p_process_full)
    p_process_full.set_defaults(func=cmd_process_full)

    p_process_articles = sub.add_parser("process-articles", help="суть и перевод заголовка для выбранных article_id")
    p_process_articles.add_argument("article_id", nargs="+", type=int)
    p_process_articles.add_argument("--offline", action="store_true", help="детерминированная локальная заглушка без OpenAI API")
    p_process_articles.set_defaults(func=cmd_process_articles)

    p_enqueue_process = sub.add_parser("enqueue-process", help="поставить AI-обработку в очередь (external-ai при внешнем контуре), не выполняя локально")
    add_ai_args(p_enqueue_process)
    p_enqueue_process.set_defaults(func=cmd_enqueue_process)

    p_marked = sub.add_parser("recheck-marked", help="показать помеченные на удаление (pending_deletion): счётчик + выборка")
    p_marked.add_argument("--limit", type=int, default=80, help="сколько показать")
    p_marked.set_defaults(func=cmd_recheck_marked)

    p_purge = sub.add_parser("recheck-purge", help="РАЗОВО физически удалить все помеченные (pending_deletion) статьи")
    p_purge.add_argument("--yes", action="store_true", help="подтвердить необратимое удаление")
    p_purge.add_argument("--force", action="store_true", help="удалять даже статьи из выпусков (черновик или отметка «в дайджест»)")
    p_purge.set_defaults(func=cmd_recheck_purge)

    p_unmark = sub.add_parser("recheck-unmark", help="снять пометку «на удаление» со всех статей (вернуть в ленту)")
    p_unmark.set_defaults(func=cmd_recheck_unmark)

    p_enqueue_translate = sub.add_parser("enqueue-translate", help="бэкфилл перевода заголовков (title_ru) по всей базе батчами через воркер")
    p_enqueue_translate.add_argument("--batch-size", type=int, default=100, help="статей в одной задаче")
    p_enqueue_translate.add_argument("--limit", type=int, default=0, help="потолок числа статей (0 = все без перевода)")
    p_enqueue_translate.set_defaults(func=cmd_enqueue_translate)

    p_enqueue_external = sub.add_parser("enqueue-external-scrape", help="фетч источников network_region=external через зарубежный воркер (no-op без FETCH_EXTERNAL_ENABLED)")
    p_enqueue_external.add_argument("--max-age-days", type=int, default=None, help="окно свежести статей (по умолчанию без ограничения)")
    p_enqueue_external.set_defaults(func=cmd_enqueue_external_scrape)

    p_repr_list = sub.add_parser("reprints", help="показать помеченные перепечатки или снять пометку")
    p_repr_list.add_argument("--limit", type=int, default=20)
    p_repr_list.add_argument("--unmark", type=int, default=None,
                             help="снять пометку с article_id — статья вернётся в ленту")
    p_repr_list.set_defaults(func=cmd_reprints)

    p_reprints = sub.add_parser(
        "find-reprints", help="перепечатки между источниками: правило + ИИ-судья (№21)")
    p_reprints.add_argument("--days", type=int, default=14, help="окно поиска")
    p_reprints.add_argument("--min-overlap", type=float, default=0.35, help="порог пересечения слов")
    p_reprints.add_argument("--max-overlap", type=float, default=1.0,
                            help="верхняя граница пересечения — чтобы прицельно проверить "
                                 "спорную полосу (напр. --max-overlap 0.6)")
    p_reprints.add_argument("--max-days-apart", type=int, default=5, help="разрыв дат в паре")
    p_reprints.add_argument("--limit", type=int, default=100, help="сколько пар проверить")
    p_reprints.add_argument("--show", type=int, default=20, help="сколько строк показать")
    p_reprints.add_argument("--candidates-only", action="store_true", help="только правило, без модели")
    p_reprints.add_argument("--offline", action="store_true", help="заглушка вместо модели")
    p_reprints.add_argument("--apply", action="store_true", help="ЗАПИСАТЬ пометки (по умолчанию сухой прогон)")
    p_reprints.add_argument("--min-interval-hours", type=float, default=0,
                            help="с --apply: пропустить, если прогон с записью был недавно (для планировщика)")
    p_reprints.add_argument("--local", action="store_true",
                            help="считать здесь, а не через внешний воркер (только для offline-проверок: "
                                 "с РФ-адреса OpenAI отвечает 403)")
    p_reprints.set_defaults(func=cmd_find_reprints)

    p_ext_refetch = sub.add_parser(
        "enqueue-external-refetch",
        help="дозаполнить тело обрывков у источников network_region=external через зарубежный воркер")
    p_ext_refetch.add_argument("--limit", type=int, default=200, help="сколько статей взять за прогон")
    p_ext_refetch.add_argument("--batch", type=int, default=25, help="статей в одной задаче")
    p_ext_refetch.set_defaults(func=cmd_enqueue_external_refetch)

    p_repair_bodies = sub.add_parser(
        "repair-article-bodies",
        help="перекачать тела статей с дефектом (чужое тело, кракозябры, простыня) новым извлечением, срезать хвосты")
    p_repair_bodies.add_argument("--ids", default="", help="id статей через запятую")
    p_repair_bodies.add_argument("--source-id", type=int, default=None, help="все статьи источника за --days")
    p_repair_bodies.add_argument("--statuses", default="",
                                 help="обрывки всех источников с этим статусом дозагрузки, напр. too_short,mismatch")
    p_repair_bodies.add_argument("--tails", action="store_true",
                                 help="только хвосты в конце текста, по всем источникам или по --source-id")
    p_repair_bodies.add_argument("--all-pages", action="store_true",
                                 help="с --tails: качать страницу и там, где хвост не виден по тексту")
    p_repair_bodies.add_argument("--days", type=int, default=60)
    p_repair_bodies.add_argument("--limit", type=int, default=0, help="не больше N статей (0 — все)")
    p_repair_bodies.add_argument("--pause", type=float, default=0.5, help="пауза между страницами, с")
    p_repair_bodies.add_argument("--apply", action="store_true", help="записать тела (без флага — сухой прогон)")
    p_repair_bodies.add_argument("--reprocess", action=argparse.BooleanOptionalAction, default=True,
                                 help="после --apply поставить перерасчёт сути по заменённым (мимо выпуска)")
    p_repair_bodies.add_argument("--batch", type=int, default=25, help="статей в задаче перерасчёта")
    p_repair_bodies.add_argument("--json", action="store_true")
    p_repair_bodies.set_defaults(func=cmd_repair_article_bodies)

    p_repair_dates = sub.add_parser(
        "repair-article-dates",
        help="исправить дату публикации, противоречащую дате в адресе статьи, новым разбором страницы")
    p_repair_dates.add_argument("--ids", default="", help="id статей через запятую")
    p_repair_dates.add_argument("--source-id", type=int, default=None, help="все статьи источника за --days")
    p_repair_dates.add_argument("--days", type=int, default=180, help="окно по дате поступления, дней")
    p_repair_dates.add_argument("--limit", type=int, default=0, help="не больше N статей, 0 значит все")
    p_repair_dates.add_argument("--pause", type=float, default=0.5, help="пауза между страницами, с")
    p_repair_dates.add_argument("--save-plan", default="", help="сохранить план сухого прогона в файл")
    p_repair_dates.add_argument("--apply", default="", metavar="ПЛАН",
                                help="записать план сухого прогона из файла; без него ничего не пишется")
    p_repair_dates.add_argument("--json", action="store_true")
    p_repair_dates.set_defaults(func=cmd_repair_article_dates)

    p_repair_keys = sub.add_parser(
        "repair-url-keys",
        help="пересчитать ключ адреса (номер статьи из query) и вернуть в ленту спрятанное склейкой 13.09")
    p_repair_keys.add_argument("--apply", action="store_true", help="записать (без флага — сухой прогон)")
    p_repair_keys.add_argument("--json", action="store_true")
    p_repair_keys.set_defaults(func=cmd_repair_url_keys)

    p_set_region = sub.add_parser("set-source-region", help="проставить network_region (auto|ru|external) источникам по id")
    p_set_region.add_argument("--ids", required=True, help="список id через запятую, напр. 16,84,64")
    p_set_region.add_argument("--region", required=True, help="auto|ru|external")
    p_set_region.set_defaults(func=cmd_set_source_region)

    p_dump_listing = sub.add_parser("source-dump-listing", help="выгрузить анкеры листинга источника (для подбора listing_selector у no_candidates)")
    p_dump_listing.add_argument("source_id", type=int, help="id источника")
    p_dump_listing.add_argument("--limit", type=int, default=40, help="сколько анкеров показать")
    p_dump_listing.add_argument("--render", action="store_true", help="форсить playwright-рендер (проверить, даёт ли JS-SPA статьи)")
    p_dump_listing.add_argument("--url", default=None, help="протестить произвольный URL вместо listing_url источника (не меняет БД)")
    p_dump_listing.set_defaults(func=cmd_source_dump_listing)

    sub.add_parser("ai-cost-report", help="отчёт по токенам/стоимости AI-этапов").set_defaults(func=cmd_ai_cost_report)

    p_article_cost = sub.add_parser("ai-article-cost-report", help="стоимость полного AI-прогона одной статьи")
    p_article_cost.add_argument("--limit", type=int, default=20)
    p_article_cost.add_argument("--include-partial", action="store_true", help="показывать статьи с неполным циклом")
    p_article_cost.set_defaults(func=cmd_ai_article_cost_report)

    p_sources = sub.add_parser("sources", help="список источников")
    p_sources.add_argument("--search", default=None)
    p_sources.add_argument("--limit", type=int, default=50)
    p_sources.set_defaults(func=cmd_sources)

    sub.add_parser("seed-signal-topics", help="создать дефолтные темы радара сигналов").set_defaults(func=cmd_seed_signal_topics)

    p_enqueue_signals = sub.add_parser(
        "enqueue-signal-discovery",
        help="поставить прогон радара в очередь: поиск GPT, находки решает «Отбор»; проверка с --no-offline",
    )
    p_enqueue_signals.add_argument("--topic", default=None)
    p_enqueue_signals.add_argument("--days", type=int, default=14)
    p_enqueue_signals.add_argument(
        "--offline", action=argparse.BooleanOptionalAction, default=True,
        help="без «Дедупа» и на ядре РФ, где OpenAI отвечает 403; рабочий прогон: --no-offline")
    p_enqueue_signals.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False)
    p_enqueue_signals.set_defaults(func=cmd_enqueue_signal_discovery)

    p_enqueue_daily_signals = sub.add_parser(
        "enqueue-daily-signal-discovery",
        help="поставить прогон радара по расписанию: раз в сутки Москвы, с SIGNAL_DISCOVERY_DAILY_EVERY_DAYS реже",
    )
    p_enqueue_daily_signals.add_argument("--force", action="store_true", help="игнорировать daily-idempotency")
    p_enqueue_daily_signals.set_defaults(func=cmd_enqueue_daily_signal_discovery)

    p_enqueue_cards = sub.add_parser(
        "enqueue-signal-cards",
        help="шаг планировщика «заполнить карточки»: новые сигналы — агенту «Карточка сигнала» "
             "(за флагом SIGNAL_CARD_ENABLED)",
    )
    p_enqueue_cards.add_argument("--ids", default="",
                                 help="пересчитать карточки названных сигналов (номера через запятую) — "
                                      "по полосе пересчётов")
    p_enqueue_cards.set_defaults(func=cmd_enqueue_signal_cards)

    p_enqueue_otbor = sub.add_parser(
        "enqueue-otbor",
        help="шаг планировщика «Отбор»: новые статьи, находки поиска и материалы коллег в задачи агента «Отбор»",
    )
    p_enqueue_otbor.add_argument("--ids", default="",
                                 help="повторить «Отбор» названных статей (номера через запятую) — по полосе "
                                      "пересчётов")
    p_enqueue_otbor.add_argument("--signal-ids", default="",
                                 help="повторить «Отбор» названных находок и материалов (номера сигналов через "
                                      "запятую) — по полосе пересчётов")
    p_enqueue_otbor.set_defaults(func=cmd_enqueue_otbor)

    p_enqueue_summary = sub.add_parser(
        "enqueue-signal-summary",
        help="шаг планировщика «Суть и перевод» сигналов: принятые «Отбором» находки и материалы без сути или "
             "русского заголовка",
    )
    p_enqueue_summary.add_argument("--ids", default="",
                                   help="повторить для названных сигналов (номера через запятую) — по полосе "
                                        "пересчётов")
    p_enqueue_summary.set_defaults(func=cmd_enqueue_signal_summary)

    p_enqueue_scores = sub.add_parser(
        "enqueue-signal-scores",
        help="шаг планировщика «баллы» (второй вызов «Карточки»): сигналы после основного вызова без баллов и с "
             "баллами после «не тот блок» (модель SIGNAL_SCORES_MODEL)",
    )
    p_enqueue_scores.add_argument("--ids", default="",
                                  help="повторить для названных сигналов (номера через запятую) по полосе "
                                       "пересчётов")
    p_enqueue_scores.set_defaults(func=cmd_enqueue_signal_scores)

    p_otbor_materials = sub.add_parser(
        "otbor-materials",
        help="накопленные материалы коллег — один раз через «Отбор» (по умолчанию сухой прогон: число и цена)",
    )
    p_otbor_materials.add_argument("--apply", action="store_true",
                                   help="завести сигналы материалов и задачи «Отбора»; без флага ничего не пишет")
    p_otbor_materials.add_argument("--silent", action="store_true",
                                   help="итог «Отбора» авторам не сообщать (накопленные до включения, решение "
                                        "владельца 04.11)")
    p_otbor_materials.set_defaults(func=cmd_otbor_materials)

    p_otbor_shadow = sub.add_parser(
        "otbor-shadow",
        help="проверка «Отбора» на реальных данных (#134): статьи ленты, решённые гейтом, проходят «Отбор», решения "
             "пишутся рядом, витрины не меняются; по умолчанию сухой прогон: число и цена",
    )
    p_otbor_shadow.add_argument("--since", type=_iso_date, required=True,
                                help="начало окна ГГГГ-ММ-ДД по дате сбора статьи")
    p_otbor_shadow.add_argument("--model", default="",
                                help="модель «Отбора»; без флага OTBOR_MODEL")
    p_otbor_shadow.add_argument("--apply", action="store_true",
                                help="поставить задачи по полосе пересчётов; без флага ничего не пишет")
    p_otbor_shadow.set_defaults(func=cmd_otbor_shadow)

    p_otbor_shadow_report = sub.add_parser(
        "otbor-shadow-report",
        help="отчёт по проверке «Отбора» на реальных данных (только чтение): разделы отказа, категории, технологии "
             "из сбора на радар, гейт против «Отбора» с примерами",
    )
    p_otbor_shadow_report.add_argument("--since", type=_iso_date, required=True,
                                       help="начало окна ГГГГ-ММ-ДД по дате сбора статьи")
    p_otbor_shadow_report.add_argument("--examples", type=int, default=15,
                                       help="примеров в каждой клетке расхождения с гейтом")
    p_otbor_shadow_report.add_argument("--json", action="store_true", help="весь отчёт JSON")
    p_otbor_shadow_report.set_defaults(func=cmd_otbor_shadow_report)

    p_otbor_switch = sub.add_parser(
        "otbor-switch",
        help="переключение витрин на «Отбор» (#134): решения проверки на реальных данных в сигналы; по умолчанию "
             "сухой прогон",
    )
    p_otbor_switch.add_argument("--since", type=_iso_date, required=True,
                                help="начало окна ГГГГ-ММ-ДД по дате сбора статьи (как у проверки)")
    p_otbor_switch.add_argument("--apply", action="store_true",
                                help="перенести (только при включённой «Карточке»); без флага ничего не пишет")
    p_otbor_switch.add_argument("--json", action="store_true", help="всё с номерами статей, JSON")
    p_otbor_switch.set_defaults(func=cmd_otbor_switch)

    p_card_history = sub.add_parser(
        "enqueue-signal-card-history",
        help="прогон по истории агента «Карточка сигнала» (#98): лента с августа 2026, весь радар и материалы "
             "коллег — по полосе пересчётов; по умолчанию сухой прогон — число сигналов и цена",
    )
    p_card_history.add_argument("--apply", action="store_true",
                                help="поставить задачи (только при включённом агенте); без флага — число и цена")
    p_card_history.add_argument("--since", type=_iso_date, default=None,
                                help="граница истории ГГГГ-ММ-ДД: лента по дате сбора статьи, радар и материалы по дате "
                                     "появления сигнала; без флага лента с августа 2026 и весь радар")
    p_card_history.set_defaults(func=cmd_enqueue_signal_card_history)

    p_signals = sub.add_parser("signals", help="список найденных технологических сигналов")
    p_signals.add_argument("--maturity", choices=["watch", "shortlist", "proven", "reject"], default=None)
    p_signals.add_argument("--theme", default=None)
    p_signals.add_argument("--limit", type=int, default=50)
    p_signals.add_argument("--evidence-limit", type=int, default=3)
    p_signals.add_argument("--json", action="store_true")
    p_signals.set_defaults(func=cmd_signals)

    p_signal_feedback = sub.add_parser("signal-feedback", help="записать событие обратной связи по сигналу")
    p_signal_feedback.add_argument("event_type", choices=[
        "added_to_digest",
        "marked_noise",
        "marked_duplicate",
        "tag_changed",
        "score_changed",
        "status_changed",
        "comment_added",
    ])
    p_signal_feedback.add_argument("--article-id", type=int, default=None)
    p_signal_feedback.add_argument("--signal-id", type=int, default=None)
    p_signal_feedback.add_argument("--signal-evidence-id", type=int, default=None)
    p_signal_feedback.add_argument("--source-url", default=None)
    p_signal_feedback.add_argument("--signal-title", default=None)
    p_signal_feedback.add_argument("--user-id", type=int, default=None)
    p_signal_feedback.add_argument("--old-value", default=None)
    p_signal_feedback.add_argument("--new-value", default=None)
    p_signal_feedback.add_argument("--comment", default=None)
    p_signal_feedback.add_argument("--verdict", choices=["approved", "reject", "duplicate", "needs_context"], default=None)
    p_signal_feedback.add_argument("--reason", default=None)
    p_signal_feedback.add_argument("--corrected-title", default=None)
    p_signal_feedback.add_argument("--corrected-thesis", default=None)
    p_signal_feedback.add_argument("--duplicate-of-signal-id", type=int, default=None)
    p_signal_feedback.set_defaults(func=cmd_signal_feedback)

    p_import_signal_feedback = sub.add_parser("import-signal-feedback", help="импортировать ОС по сигналам из CSV")
    p_import_signal_feedback.add_argument("path")
    p_import_signal_feedback.add_argument("--user-id", type=int, default=None)
    p_import_signal_feedback.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    p_import_signal_feedback.add_argument("--json", action="store_true")
    p_import_signal_feedback.set_defaults(func=cmd_import_signal_feedback)

    p_unmerge = sub.add_parser("unmerge-signal", help="снять пометку дубля с карточки радара (ошибочный «Дубль #ID»)")
    p_unmerge.add_argument("signal_id", type=int)
    p_unmerge.set_defaults(func=cmd_unmerge_signal)

    sub.add_parser(
        "refresh-signal-evidence-counts",
        help="пересчитать число ссылок у карточек радара (у тех, чьи ссылки переехали в другие)",
    ).set_defaults(func=cmd_refresh_signal_evidence_counts)

    p_radar_weekly = sub.add_parser("radar-weekly-report", help="итоги недели радара: вышло, отсеяно и почему, отзывы")
    p_radar_weekly.add_argument("--days", type=int, default=7)
    p_radar_weekly.set_defaults(func=cmd_radar_weekly_report)

    p_import_reference = sub.add_parser(
        "import-reference-signals",
        help="эталон заказчика (xlsx ТОП-сигналов) — в память радара как одобренные; сухой прогон по умолчанию",
    )
    p_import_reference.add_argument("path")
    p_import_reference.add_argument("--sheet", action="append", help="лист; можно несколько, по умолчанию все")
    p_import_reference.add_argument("--apply", action="store_true")
    p_import_reference.set_defaults(func=cmd_import_reference_signals)

    p_radar_recall = sub.add_parser(
        "radar-recall", help="сколько событий эталона заказчика (xlsx) нашёл радар — замер полноты",
    )
    p_radar_recall.add_argument("path")
    p_radar_recall.add_argument("--sheet", action="append")
    p_radar_recall.add_argument("--json", action="store_true")
    p_radar_recall.set_defaults(func=cmd_radar_recall)

    p_search_trial = sub.add_parser(
        "radar-search-trial",
        help="пробный прогон широкого поиска (#138): без флага сухой прогон с вызовами и ценой; --run: один прогон, "
             "находки проходят «Отбор», витрины не меняются; --report N: отчёт",
    )
    p_search_trial.add_argument("--since", type=_iso_date, default=None,
                                help="период поиска с ГГГГ-ММ-ДД (по умолчанию SIGNAL_RESEARCH_DAYS до --until)")
    p_search_trial.add_argument("--until", type=_iso_date, default=None, help="… по ГГГГ-ММ-ДД (по умолчанию сегодня)")
    p_search_trial.add_argument("--per-topic", type=int, default=None,
                                help="до скольких материалов просить на тему (по умолчанию SIGNAL_RESEARCH_EVENTS_PER_TOPIC)")
    p_search_trial.add_argument("--model", default=None, help="модель поиска (по умолчанию SIGNAL_RESEARCH_MODEL)")
    p_search_trial.add_argument("--reasoning", default=None, help="уровень рассуждения поиска")
    p_search_trial.add_argument("--otbor-model", default=None, help="модель «Отбора» (по умолчанию OTBOR_MODEL)")
    p_search_trial.add_argument("--otbor-reasoning", default=None, help="уровень рассуждения «Отбора»")
    p_search_trial.add_argument("--run", action="store_true", help="поставить прогон; без флага сухой прогон")
    p_search_trial.add_argument("--report", type=int, default=None, help="отчёт по задаче пробного прогона N")
    p_search_trial.add_argument("--reference", default=None,
                                help="эталон эксперта (xlsx, как у radar-recall) для полноты в отчёте")
    p_search_trial.add_argument("--sheet", action="append")
    p_search_trial.add_argument("--json", action="store_true")
    p_search_trial.set_defaults(func=cmd_radar_search_trial)

    p_card_pack = sub.add_parser(
        "signal-card-expert-pack",
        help="пакет эксперту: справочники на утверждение и разметка 50 сигналов (xlsx + манифест); "
             "сухой прогон по умолчанию",
    )
    p_card_pack.add_argument("--out", default=None, help="папка вне репозитория (он публичный)")
    p_card_pack.add_argument("--seed", type=int, default=None,
                             help="зерно выборки: те же зерно и окно — те же сигналы (по умолчанию 20261014)")
    p_card_pack.add_argument("--since", type=_iso_date, default=None,
                             help="статьи ленты собраны с ГГГГ-ММ-ДД (по умолчанию 2026-08-01)")
    p_card_pack.add_argument("--until", type=_iso_date, default=None,
                             help="… и до ГГГГ-ММ-ДД, не включая; он же край поступления сигналов радара "
                                  "(по умолчанию сегодня, не позже 2026-11-01)")
    p_card_pack.add_argument("--ids-file", default=None,
                             help="ручной отбор: файл «номер доля» на строку (article:N useful, signal:N technology …)")
    p_card_pack.add_argument("--overrides", default=None,
                             help="ручные поправки JSON: {номер: {published, title, text, note}} — с первоисточника")
    p_card_pack.add_argument("--apply", action="store_true", help="записать; без флага — только состав")
    p_card_pack.set_defaults(func=cmd_signal_card_expert_pack)

    p_card_exam = sub.add_parser(
        "signal-card-exam",
        help="экзамен агента «Карточка сигнала» на разметке эксперта (#98): модели по порогам, отчёт и решение "
             "— рядом с манифестом",
    )
    p_card_exam.add_argument("--markup", required=True, help="таблица разметки, заполненная экспертом (вне репозитория)")
    p_card_exam.add_argument("--manifest", default=None,
                             help="манифест пакета (по умолчанию — signal_card_markup_manifest.json рядом с таблицей)")
    p_card_exam.add_argument("--models", default=None,
                             help="лестница моделей по цене, через запятую: следующая — только при провале предыдущей "
                                  "(по умолчанию gpt-5.4-nano, gpt-5.4-mini)")
    p_card_exam.add_argument("--runs", type=int, default=None,
                             help="прогонов на сигнал: на экзамене — только 3 (спека), на настройке — от 1")
    p_card_exam.add_argument("--dry-run", action="store_true",
                             help="только число вызовов и цена: ничего не записать и не вызвать модель")
    p_card_exam.add_argument("--rescore", default=None, metavar="REPORT",
                             help="пересчитать сохранённый отчёт без вызовов модели: те же ответы по нынешним "
                                  "проверкам, допускам и алиасам; модели, прогоны и часть — из отчёта")
    p_card_exam.add_argument("--part", choices=["exam", "tuning"], default=None,
                             help="часть набора: экзамен (35) или настройка (15) — по ней и только по ней правят инструкцию")
    p_card_exam.set_defaults(func=cmd_signal_card_exam)

    p_card_exam_judge = sub.add_parser(
        "signal-card-exam-judge",
        help="значения судьи радара у радарных строк манифеста экзамена (#98) — JSON в stdout, запуск на ядре",
    )
    p_card_exam_judge.add_argument("--manifest", required=True, help="манифест пакета эксперту или «-» — из stdin")
    p_card_exam_judge.set_defaults(func=cmd_signal_card_exam_judge)

    p_otbor_pack = sub.add_parser(
        "otbor-expert-pack",
        help="пакет эксперту для «Отбора» (#129): справочники и линейки на утверждение, разметка вслепую "
             "(xlsx + манифест); сухой прогон по умолчанию",
    )
    p_otbor_pack.add_argument("--out", default=None, help="папка вне репозитория (он публичный)")
    p_otbor_pack.add_argument("--card-manifest", default=None,
                              help="манифест прежней разметки карточки (#94), signal_card_markup_manifest.json")
    p_otbor_pack.add_argument("--card-markup", default=None,
                              help="заполненная экспертом таблица прежней разметки карточки: связь берётся оттуда")
    p_otbor_pack.add_argument("--seed", type=int, default=None,
                              help="зерно выборки: те же зерно и окно дают те же строки (по умолчанию 20261022)")
    p_otbor_pack.add_argument("--until", type=_iso_date, default=None,
                              help="конец окна ГГГГ-ММ-ДД, не включая: статьи ленты за 30 дней до него, находки и "
                                   "материалы до него (по умолчанию сегодня по Москве)")
    p_otbor_pack.add_argument("--apply", action="store_true", help="записать; без флага только состав")
    p_otbor_pack.set_defaults(func=cmd_otbor_expert_pack)

    p_otbor_check = sub.add_parser(
        "otbor-markup-check",
        help="проверка размеченного пакета «Отбора» (#129): номера и тексты сходятся с манифестом, значения из "
             "списков, у взятых строк баллы и обоснование; отчёт о нарушениях",
    )
    p_otbor_check.add_argument("--markup", required=True, help="таблица, заполненная экспертом")
    p_otbor_check.add_argument("--manifest", default=None,
                               help="манифест пакета (по умолчанию otbor_markup_manifest.json рядом с таблицей)")
    p_otbor_check.set_defaults(func=cmd_otbor_markup_check)

    p_otbor_exam = sub.add_parser(
        "otbor-exam",
        help="экзамен агента «Отбор» на разметке эксперта (#133): модели лестницей по цене, отчёт с таблицей "
             "качества и цены рядом с манифестом",
    )
    p_otbor_exam.add_argument("--markup", required=True, help="таблица разметки, заполненная экспертом (вне репозитория)")
    p_otbor_exam.add_argument("--manifest", default=None,
                              help="манифест пакета (по умолчанию otbor_markup_manifest.json рядом с таблицей)")
    p_otbor_exam.add_argument("--models", default=None,
                              help="лестница моделей через запятую, по цене: следующая только при провале предыдущей "
                                   "(по умолчанию gpt-6-luna, gpt-5.6-luna, gpt-5.4-nano, gpt-5.4-mini, gpt-5.5)")
    p_otbor_exam.add_argument("--runs", type=int, default=None,
                              help="прогонов на публикацию: на экзамене только 3 (спека), на настройке от 1")
    p_otbor_exam.add_argument("--dry-run", action="store_true",
                              help="только деление, число вызовов и цена: ничего не записать и не вызвать модель")
    p_otbor_exam.add_argument("--rescore", default=None, metavar="REPORT",
                              help="пересчитать сохранённый отчёт без вызовов модели: модели, прогоны и часть из отчёта")
    p_otbor_exam.add_argument("--part", choices=["exam", "tuning"], default=None,
                              help="часть набора: экзамен или настройка (по ней и только по ней правят инструкцию)")
    p_otbor_exam.add_argument("--all", action="store_true",
                              help="прогнать все модели лестницы, и после прошедшей: полная таблица качества и цены")
    p_otbor_exam.add_argument("--workers", type=int, default=1,
                              help="параллельных вызовов модели (по умолчанию 1); ответы и счёт те же")
    p_otbor_exam.set_defaults(func=cmd_otbor_exam)

    p_otbor_exam_control = sub.add_parser(
        "otbor-exam-control",
        help="контроль экзамена «Отбора» (#133): размеры страт ленты и категория судьи у радарных строк манифеста, "
             "JSON в stdout, запуск на ядре",
    )
    p_otbor_exam_control.add_argument("--manifest", required=True, help="манифест пакета эксперту или «-» из stdin")
    p_otbor_exam_control.set_defaults(func=cmd_otbor_exam_control)

    p_scores_exam = sub.add_parser(
        "card-scores-exam",
        help="экзамен «Карточки» на разметке эксперта (#133): тема основного вызова и баллы второго, модели второго "
             "вызова лестницей по цене, отчёт рядом с манифестом",
    )
    p_scores_exam.add_argument("--markup", required=True,
                               help="таблица разметки, заполненная экспертом (вне репозитория)")
    p_scores_exam.add_argument("--manifest", default=None,
                               help="манифест пакета (по умолчанию otbor_markup_manifest.json рядом с таблицей)")
    p_scores_exam.add_argument("--models", default=None,
                               help="лестница моделей второго вызова через запятую, по цене (по умолчанию gpt-6-luna, "
                                    "gpt-5.6-luna, gpt-5.4-nano, gpt-5.4-mini, gpt-6.1-terra, gpt-6-sol, gpt-6.1-sol, "
                                    "gpt-5.5)")
    p_scores_exam.add_argument("--card-model", default=None,
                               help="модель основного вызова (по умолчанию SIGNAL_CARD_MODEL; на проде gpt-6-sol)")
    p_scores_exam.add_argument("--summary-model", default=None,
                               help="модель «Сути и перевода» (по умолчанию OPENAI_SUMMARY_MODEL, как у агента)")
    p_scores_exam.add_argument("--card-markup", default=None,
                               help="разметка экзамена карточки #98 (манифест рядом): атрибуты основного вызова того "
                                    "же прогона сверяются с ней на строках его экзамена, по его порогам")
    p_scores_exam.add_argument("--theme-before", default=None, metavar="REPORT",
                               help="прежний отчёт экзамена на том же манифесте: тема основного вызова и ошибки "
                                    "«Рынок против темы отрасли» до и после (#149)")
    p_scores_exam.add_argument("--runs", type=int, default=None,
                               help="прогонов на строку: на экзамене только 3 (спека), на настройке от 1")
    p_scores_exam.add_argument("--dry-run", action="store_true",
                               help="только строки, число вызовов и цена: ничего не записать и не вызвать модель")
    p_scores_exam.add_argument("--rescore", default=None, metavar="REPORT",
                               help="пересчитать сохранённый отчёт без вызовов модели: модели, прогоны и часть из "
                                    "отчёта")
    p_scores_exam.add_argument("--part", choices=["exam", "tuning"], default=None,
                               help="часть набора: экзамен или настройка (деление общее с «Отбором»)")
    p_scores_exam.add_argument("--all", action="store_true",
                               help="прогнать все модели лестницы, и после прошедшей: полная таблица качества и цены")
    p_scores_exam.add_argument("--workers", type=int, default=1,
                               help="параллельных вызовов модели (по умолчанию 1); ответы и счёт те же")
    p_scores_exam.set_defaults(func=cmd_card_scores_exam)

    p_scores_exam_control = sub.add_parser(
        "card-scores-exam-control",
        help="выгрузка с ядра для экзамена «Карточки» (#133): критерии «Скоринга», теги тем, контроль у строк пакета и "
             "«сильные сверх пакета», JSON в stdout, запуск на ядре",
    )
    p_scores_exam_control.add_argument("--manifest", required=True, help="манифест пакета эксперту или «-» из stdin")
    p_scores_exam_control.set_defaults(func=cmd_card_scores_exam_control)

    p_archive_signals = sub.add_parser(
        "archive-signals",
        help="убрать карточки радара в архив (обратимо); по умолчанию — сухой прогон со списком",
    )
    p_archive_signals.add_argument("--created-before", default=None, help="поступили раньше даты ГГГГ-ММ-ДД")
    p_archive_signals.add_argument("--free-theme-only", action=argparse.BooleanOptionalAction, default=True,
                                   help="только с темой не из 13 тематик (ранние карточки 13.09)")
    p_archive_signals.add_argument("--ids", default="", help="номера карточек через запятую")
    p_archive_signals.add_argument("--reason", default="early-free-theme-2026-09")
    p_archive_signals.add_argument("--apply", action="store_true", help="записать; без флага — только список")
    p_archive_signals.set_defaults(func=cmd_archive_signals)

    p_sync_feed = sub.add_parser(
        "sync-feed-signals",
        help="сигналы ленты для принятых статей по дате сбора (без ИИ); по умолчанию сухой прогон",
    )
    p_sync_feed.add_argument("--since", default=None, help="собраны с даты ГГГГ-ММ-ДД; по умолчанию — 2026-08-01")
    p_sync_feed.add_argument("--until", default=None, help="собраны до даты ГГГГ-ММ-ДД (не включая)")
    p_sync_feed.add_argument("--apply", action="store_true", help="записать; без флага — только числа")
    p_sync_feed.set_defaults(func=cmd_sync_feed_signals)

    p_unarchive_signals = sub.add_parser("unarchive-signals", help="вернуть карточки радара из архива")
    p_unarchive_signals.add_argument("--ids", default="")
    p_unarchive_signals.add_argument("--reason", default=None)
    p_unarchive_signals.set_defaults(func=cmd_unarchive_signals)

    p_export_signal_training = sub.add_parser(
        "export-signal-training-jsonl",
        help="выгрузить snapshot'ы сигналов с ОС в JSONL для eval/training",
    )
    p_export_signal_training.add_argument("--path", default=None)
    p_export_signal_training.add_argument("--limit", type=int, default=1000)
    p_export_signal_training.add_argument("--all", action="store_true", help="включить примеры без человеческой ОС")
    p_export_signal_training.add_argument("--verdict", choices=["approved", "reject", "duplicate", "needs_context"], default=None)
    p_export_signal_training.add_argument("--json", action="store_true")
    p_export_signal_training.set_defaults(func=cmd_export_signal_training_jsonl)

    p_export_signal_context = sub.add_parser(
        "export-signal-context",
        help="выгрузить переносимый bundle сигналов/evidence/памяти/ОС для переноса на сервер",
    )
    p_export_signal_context.add_argument("--path", default=None)
    p_export_signal_context.add_argument("--include-rejected", action="store_true")
    p_export_signal_context.add_argument("--json", action="store_true")
    p_export_signal_context.set_defaults(func=cmd_export_signal_context)

    p_import_signal_context = sub.add_parser(
        "import-signal-context",
        help="импортировать переносимый bundle сигналов/evidence/памяти/ОС",
    )
    p_import_signal_context.add_argument("path")
    p_import_signal_context.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    p_import_signal_context.add_argument("--json", action="store_true")
    p_import_signal_context.set_defaults(func=cmd_import_signal_context)

    p_legacy_signal_feedback = sub.add_parser("article-signal-feedback", help="legacy: записать ОС по article_id")
    p_legacy_signal_feedback.add_argument("article_id", type=int)
    p_legacy_signal_feedback.add_argument("event_type", choices=[
        "added_to_digest",
        "marked_noise",
        "marked_duplicate",
        "tag_changed",
        "score_changed",
        "status_changed",
        "comment_added",
    ])
    p_legacy_signal_feedback.add_argument("--signal-id", type=int, default=None)
    p_legacy_signal_feedback.add_argument("--signal-evidence-id", type=int, default=None)
    p_legacy_signal_feedback.add_argument("--source-url", default=None)
    p_legacy_signal_feedback.add_argument("--signal-title", default=None)
    p_legacy_signal_feedback.add_argument("--user-id", type=int, default=None)
    p_legacy_signal_feedback.add_argument("--old-value", default=None)
    p_legacy_signal_feedback.add_argument("--new-value", default=None)
    p_legacy_signal_feedback.add_argument("--comment", default=None)
    p_legacy_signal_feedback.add_argument("--verdict", choices=["approved", "reject", "duplicate", "needs_context"], default=None)
    p_legacy_signal_feedback.add_argument("--reason", default=None)
    p_legacy_signal_feedback.add_argument("--corrected-title", default=None)
    p_legacy_signal_feedback.add_argument("--corrected-thesis", default=None)
    p_legacy_signal_feedback.add_argument("--duplicate-of-signal-id", type=int, default=None)
    p_legacy_signal_feedback.set_defaults(func=cmd_signal_feedback)

    p_source_quality = sub.add_parser("source-quality", help="посчитать качество источников за период")
    p_source_quality.add_argument("--days", type=int, default=30)
    p_source_quality.add_argument("--limit", type=int, default=30)
    p_source_quality.add_argument("--snapshot", action="store_true", help="сохранить срез в source_quality_snapshots")
    p_source_quality.add_argument("--json", action="store_true")
    p_source_quality.set_defaults(func=cmd_source_quality)

    p_source_health = sub.add_parser("source-health", help="вердикты покрытия источников: ok/stale/no_articles/disabled/archived")
    p_source_health.add_argument(
        "--stale-days", type=int, default=None, help="порог stale, сут.; по умолчанию config.SOURCE_STALE_DAYS",
    )
    p_source_health.add_argument("--limit", type=int, default=300)
    p_source_health.add_argument("--verdict", default=None, help="ok / stale / no_articles / disabled / archived")
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

    p_source_audit = sub.add_parser("source-audit", help="аудит ВСЕХ источников: текущая ссылка + почему не парсится (read-only)")
    p_source_audit.add_argument("--limit", type=int, default=1000, help="максимум источников")
    p_source_audit.add_argument("--probe-limit", type=int, default=3, help="сколько статей-кандидатов пробовать на источник")
    p_source_audit.add_argument("--workers", type=int, default=8, help="параллельных проб")
    p_source_audit.add_argument("--strategy", help="фильтр по стратегии (request/rss/playwright/telegram)")
    p_source_audit.add_argument("--enabled-only", action="store_true", help="только включённые источники")
    p_source_audit.add_argument("--json", action="store_true", help="полный JSON-отчёт")
    p_source_audit.set_defaults(func=cmd_source_audit)

    p_source_diag = sub.add_parser("source-diagnose", help="read-only диагностика источника по source_id")
    p_source_diag.add_argument("source_id", type=int)
    p_source_diag.add_argument("--limit", type=int, default=5, help="сколько кандидатов/постов проверить")
    p_source_diag.set_defaults(func=cmd_source_diagnose)

    p_source_probe = sub.add_parser(
        "source-probe",
        help="проба источника путём сбора: вердикт рубежей вставки по каждому кандидату (ничего не пишет)")
    p_source_probe.add_argument("source_id", type=int)
    p_source_probe.add_argument("--json", action="store_true", help="полный отчёт в JSON: все кандидаты")
    p_source_probe.add_argument("--rows", type=int, default=15,
                                help="в таблице — не больше N строк на вердикт (полный список — --json)")
    p_source_probe.set_defaults(func=cmd_source_probe)

    p_digest = sub.add_parser("digest-content", help="собрать digest_content.json выпуска месяца (общие отметки)")
    p_digest.add_argument("month", help="YYYY-MM")
    p_digest.add_argument("--output", default="digest_content.generated.json")
    p_digest.add_argument("--html-output", default=None, help="дополнительно собрать email-ready HTML")
    # Умолчания — как у экрана выпуска (DigestPage: до 500 материалов, порог балла 0): команда
    # собирает тот же выпуск, что экран (#111).
    p_digest.add_argument("--limit", type=int, default=500)
    p_digest.add_argument("--min-score", type=float, default=0)
    p_digest.set_defaults(func=cmd_digest_content)

    p_digest_save = sub.add_parser("digest-save", help="сохранить общий черновик выпуска месяца из отмеченного «в дайджест»")
    p_digest_save.add_argument("month", help="YYYY-MM")
    p_digest_save.add_argument("--limit", type=int, default=500)
    p_digest_save.add_argument("--min-score", type=float, default=0)
    p_digest_save.set_defaults(func=cmd_digest_save)

    p_shared_marks = sub.add_parser(
        "migrate-shared-marks",
        help="перенести отметки и черновики учётки-основы в общие (#111); по умолчанию пробный прогон",
    )
    p_shared_marks.add_argument("--base-user-id", type=int, default=None, help="номер учётки-основы")
    shared_marks_mode = p_shared_marks.add_mutually_exclusive_group()
    shared_marks_mode.add_argument("--write", action="store_true", help="записать (без флага — пробный прогон)")
    shared_marks_mode.add_argument("--check", action="store_true",
                                   help="после переноса: сверка закрытых месяцев только чтением")
    p_shared_marks.set_defaults(func=cmd_migrate_shared_marks)

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
    p_external_worker.add_argument("--concurrency", type=int, default=None,
                                   help="потоков выдачи в процессе (по умолчанию EXTERNAL_WORKER_CONCURRENCY)")
    p_external_worker.set_defaults(func=cmd_external_worker)

    p_tg_bot = sub.add_parser("tg-materials-bot", help="бот «Материалы коллег»: приём из Telegram в ядро (NL)")
    p_tg_bot.add_argument("--core-api-url", default=None)
    p_tg_bot.add_argument("--token", default=None)
    p_tg_bot.add_argument("--worker-id", default=None)
    p_tg_bot.set_defaults(func=cmd_tg_materials_bot)

    p_jobs_requeue = sub.add_parser("jobs-requeue-stale", help="вернуть зависшие running-задачи обратно в queued")
    p_jobs_requeue.add_argument("--stale-minutes", type=int, default=None)
    p_jobs_requeue.set_defaults(func=cmd_jobs_requeue_stale)

    p_external_status = sub.add_parser("external-queues-status", help="показать состояние external-* очередей")
    p_external_status.add_argument("--json", action="store_true")
    p_external_status.set_defaults(func=cmd_external_queues_status)

    p_check_lanes = sub.add_parser(
        "check-lanes", help="сторож внешних очередей: застой, нет воркера, истёкшие аренды (код 2 при тревоге)"
    )
    p_check_lanes.set_defaults(func=cmd_check_lanes)

    p_live_ai = sub.add_parser(
        "live-ai-leases", help="ИИ-задачи в работе (код 3, если есть) — страж scripts/deploy-core.sh"
    )
    p_live_ai.set_defaults(func=cmd_live_ai_leases)

    p_worker_versions = sub.add_parser(
        "worker-versions", help="сборки и контракты контейнеров NL глазами ядра (запускать на NL)"
    )
    p_worker_versions.add_argument("--self", dest="self_check", action="store_true",
                                   help="проверить свой контейнер (EXTERNAL_WORKER_ID); код 1, если не готов")
    p_worker_versions.add_argument("--expect-build", default=None, help="ожидаемая сборка (git SHA)")
    p_worker_versions.set_defaults(func=cmd_worker_versions)

    p_scheduler_lock = sub.add_parser(
        "scheduler-lock",
        help="выполнить команду под advisory lock планировщика; второй экземпляр пишет в лог и ждёт",
    )
    p_scheduler_lock.add_argument("--key", type=int, default=None,
                                  help="ключ замка (по умолчанию — ключ планировщика; другой — только в тестах)")
    p_scheduler_lock.add_argument("--poll-seconds", type=float, default=30.0, help="как часто пробовать взять замок")
    p_scheduler_lock.add_argument("--check-seconds", type=float, default=30.0,
                                  help="как часто проверять, что соединение с замком живо")
    p_scheduler_lock.add_argument("command", nargs=argparse.REMAINDER, help="-- команда и её аргументы")
    p_scheduler_lock.set_defaults(func=cmd_scheduler_lock)

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
    p_source_retry.add_argument(
        "--stale-days", type=int, default=None, help="порог stale, сут.; по умолчанию config.SOURCE_STALE_DAYS",
    )
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
