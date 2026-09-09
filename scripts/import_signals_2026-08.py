# -*- coding: utf-8 -*-
"""Добавить сигналы августа на платформу — только те, которых там ещё нет.

Запускается на проде внутри контейнера app. Репозиторий в контейнер не
смонтирован, поэтому скрипт подаётся в stdin:

    ssh root@109.68.213.12 'cd /root/oiltech-digest && \
      docker compose run --rm -T app python -' < scripts/import_signals_2026-08.py

Флаги дописываются в конец команды до кавычки: --apply, --process.
Сухой прогон (без --apply) ничего не пишет: только показывает, что уже есть
и что будет добавлено.
"""
from __future__ import annotations

import sys
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

APPLY = "--apply" in sys.argv
PROCESS = "--process" in sys.argv

# 15 сигналов из «Сигналы_август_2026_на_выбор_v3.xlsx».
# №3 и №13 — адреса, восстановленные при сборке выпуска (в файле стояли
# главная страница издания и пусто соответственно).
SIGNALS = [
    (1,  "Seismos — closed-loop ГРП по real-time акустике",
         "https://jpt.spe.org/case-study-closing-the-loop-on-fracture-execution-with-real-time-subsurface-measurements"),
    (2,  "Denholm Pipetech DSR — новая очистка ствола скважины",
         "https://www.oilfieldtechnology.com/product-news/19082026/new-wellbore-cleaning-solution-trials-at-heart-of-pipetechs-performance/"),
    (3,  "CorrosionRADAR — непрерывный мониторинг CUI",
         "https://www.oilfieldtechnology.com/special-reports/03082026/corrosionradar-monitoring-solution-selected-to-support-dows-alberta-project/"),
    (4,  "Presidio / FTW Technologies — AI-оптимизация зрелого фонда",
         "https://ir.bypresidio.com/sec-filings/all-sec-filings/content/0001213900-26-090532/ea030229001ex99-1.htm"),
    (5,  "Vertechs REALologyDR — real-time контроль бурового раствора",
         "https://vertechs.com/news-detail/featured-in-oilfield-technology-vertechs-realologydr-enables-automated-drilling-fluid-performance-maintenance"),
    (6,  "AI-контроль «красной зоны» на буровой",
         "https://jpt.spe.org/ai-eyes-on-the-rig-computer-vision-boosts-oilfield-safety"),
    (7,  "ПНИПУ — керамические капсулы delayed-release breaker",
         "https://scientificrussia.ru/articles/ucenye-sozdali-umnye-kapsuly-kotorye-udesevlaut-dobycu-nefti-vtroe"),
    (8,  "Сверхдлинные ГС на бассейне Permian",
         "https://www.eia.gov/todayinenergy/detail.php?id=67984"),
    (9,  "Проработка льгот для газовых ТРИЗ в РФ",
         "https://www.interfax.ru/russia/1108038"),
    (10, "ADNOC + SLB — AI/RTOC на 120+ буровых",
         "https://www.slb.com/newsroom/updates/2026/2026-0804-slb-adnoc-rtoc"),
    (11, "AGR + THF — AI-анализ опыта offset wells",
         "https://www.oilfieldtechnology.com/digital-oilfield/05082026/agr-and-thf-partner-to-advance-ai-enabled-well-engineering/"),
    (12, "Emerson PACEdge 3.0 — AI на промышленном edge",
         "https://worldoil.com/news/2026/8/11/emerson-expands-industrial-edge-ai-capabilities-with-pacedge-3-0/"),
    (13, "Газпромнефть-Хантос — беспилотные грузоперевозки",
         "http://www.angi.ru/news/2935314-%D0%9D%D0%B0%20%D0%AE%D0%B6%D0%BD%D0%BE-%D0%9F%D1%80%D0%B8%D0%BE%D0%B1%D1%81%D0%BA%D0%BE%D0%BC%20%D0%BC%D0%B5%D1%81%D1%82%D0%BE%D1%80%D0%BE%D0%B6%D0%B4%D0%B5%D0%BD%D0%B8%D0%B8%20%D0%BD%D0%B0%D1%87%D0%B0%D0%BB%D0%B8%20%D0%B8%D1%81%D0%BF%D0%BE%D0%BB%D1%8C%D0%B7%D0%BE%D0%B2%D0%B0%D1%82%D1%8C%20%D0%B1%D0%B5%D1%81%D0%BF%D0%B8%D0%BB%D0%BE%D1%82%D0%BD%D1%8B%D0%B5%20%D0%B3%D1%80%D1%83%D0%B7%D0%BE%D0%B2%D0%B8%D0%BA%D0%B8/"),
    (14, "Индустриализация инженерного интеллекта",
         "https://www.oilfieldtechnology.com/special-reports/17082026/the-industrialisation-of-engineering-intelligence/"),
    (15, "Dynamic AI Permit-to-Work",
         "https://ifactoryapp.com/industries/oil-and-gas/ai-permit-to-work-compliance-monitoring-refineries"),
]

_TRACKING = ("utm_", "yclid", "gclid", "fbclid", "_openstat")


def norm(url: str) -> str:
    """Ключ сравнения. Точный дедуп платформы идёт по articles.url, но одна
    и та же статья приходит в разных обёртках: с /amp/, с www, с utm-хвостом,
    по http вместо https. Без нормализации №11 (адрес с /amp/) завёлся бы
    вторым экземпляром рядом с уже собранным каноническим."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path
    for suffix in ("/amp/", "/amp"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    path = path.rstrip("/")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query)
                       if not any(k.lower().startswith(t) for t in _TRACKING)])
    return urlunsplit(("", host, path, query, ""))


def main() -> None:
    from oiltech_digest.db.connection import get_connection
    from oiltech_digest.ingestion.manual_import import ManualImportError, import_article
    from psycopg.rows import dict_row

    # снимок того, что уже в базе, в нормализованном виде
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id, url, title FROM articles")
        rows = cur.fetchall()
    existing: dict[str, dict] = {}
    for row in rows:
        existing.setdefault(norm(row["url"]), row)
    print(f"статей в базе: {len(rows)}\n")

    to_add, already = [], []
    for num, title, url in SIGNALS:
        hit = existing.get(norm(url))
        (already if hit else to_add).append((num, title, url, hit))

    print(f"--- уже есть: {len(already)} ---")
    for num, title, url, hit in already:
        print(f"  #{num:<3} id={hit['id']:<6} {title[:52]}")
    print(f"\n--- будет добавлено: {len(to_add)} ---")
    for num, title, url, _ in to_add:
        print(f"  #{num:<3} {title[:52]}")
        print(f"        {url[:110]}")

    if not APPLY:
        print("\nСУХОЙ ПРОГОН — ничего не записано. Для записи добавь --apply")
        return

    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id, kind, status FROM background_jobs "
                    "WHERE status IN ('running', 'finalizing')")
        busy = cur.fetchall()
    if busy:
        print("\nВНИМАНИЕ: на проде уже идут задачи — "
              + ", ".join(f"#{b['id']} {b['kind']}/{b['status']}" for b in busy))
        print("Добавление статей безопасно, но новую AI-обработку лучше ставить "
              "после их завершения.")

    print(f"\n=== ДОБАВЛЯЮ {len(to_add)} ===")
    added, failed = [], []
    for num, title, url, _ in to_add:
        try:
            res = import_article(url)
            status = "дубль" if res.duplicate else "добавлена"
            print(f"  #{num:<3} {status:<10} id={res.article_id:<6} "
                  f"источник={res.source_name[:24]:<24} текст={res.full_text_chars} зн. "
                  f"({res.full_text_status})")
            added.append((num, res.article_id))
        except ManualImportError as exc:
            print(f"  #{num:<3} НЕ УДАЛОСЬ: {exc}")
            failed.append((num, str(exc)))
        except Exception as exc:  # noqa: BLE001
            print(f"  #{num:<3} ОШИБКА {type(exc).__name__}: {exc}")
            failed.append((num, f"{type(exc).__name__}: {exc}"))

    print(f"\nдобавлено: {len(added)}, не удалось: {len(failed)}")
    for num, err in failed:
        print(f"  #{num}: {err[:120]}")

    if PROCESS and added:
        from oiltech_digest import background_jobs, network_policy
        ids = [aid for _, aid in added]
        decision = network_policy.route_ai_processing()
        job = background_jobs.enqueue(
            "process_articles",
            {"article_ids": ids, "limit": len(ids), "offline": False},
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        print(f"\nAI-обработка поставлена в очередь: job id={job['id']} "
              f"({decision.queue_name}), статей={len(ids)}")
    elif added:
        print("\nAI-обработка НЕ запускалась (нет --process): у статей пока нет "
              "сути, тегов и балла, в кандидатах дайджеста они не появятся.")


if __name__ == "__main__":
    main()
