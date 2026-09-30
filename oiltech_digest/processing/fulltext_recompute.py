"""Пересчёт сути, тега и балла, когда обрывок статьи заменён полным текстом.

Суть, тег и балл считаются по тексту статьи (pipeline._article_prompt), а лента часто отдаёт
только анонс. Полное тело приходит позже: локальная дозагрузка (fetch-full-text) и зарубежный
воркер (refetch_text — для источников network_region='external'). Выдача ИИ-пакета берёт статью,
которой не хватает стадий, не глядя на дозагрузку (repository._NEEDS_PIPELINE_FROM), а
enqueue-external-refetch стоит в цикле планировщика ПОСЛЕ enqueue-process: пакет дня, выданный с
анонсом, идёт десятки минут, и его суть ложится уже после того, как полное тело записано.

До #79 полный проход пакета иногда случайно пересчитывал такую статью; с #79 пакет зовёт только
недостающие стадии (external_ai._stages_left), и суть по обрывку осталась бы навсегда. Здесь —
явный пересчёт, один раз на замену тела:

- только настоящий прирост: то, что читает модель (первые ARTICLE_PROMPT_TEXT_CHARS знаков),
  выросло минимум вдвое (MIN_GAIN_RATIO дозагрузки) против тела, лежавшего до записи. То же
  тело при ретрае — не прирост. Промежуточное тело (too_short с NL) тоже в счёт: прирост к
  анонсу срабатывает один раз, следующий — только если тело снова вдвое длиннее;
- только то, что посчитано по прежнему тексту: у статьи есть суть, тег или балл — или она в
  пакете, выданном до замены (его итог ляжет по анонсу);
- только видимое в окне ленты: архив — только просмотр (feed_window), отвергнутое гейтом и
  скрытое не пересчитывается;
- только не выбранное в выпуск: отмеченное кем-либо «В дайджест» или стоящее в сохранённом
  черновике выпуска не меняется — с 1-го по 4-е выпуск за прошлый месяц ещё собирается, и суть
  с баллом не должны меняться под редактором (решение координатора, ревью PR #87);
- стадии — суть, тег и балл (набор business, как у любого пакета). Гейт не зовётся: это самая
  дорогая стадия, а передумав, он убрал бы статью из ленты. Перевод заголовка тоже: заголовок
  тот же, тело ему — только 900 знаков контекста, а русский заголовок — копия без модели.

Задачи — process_articles с явным списком и пометкой only (как enqueue-rescore) в полосе
пересчётов, а без неё — в потоке дня: пачки малые. Выдача откладывает задачу, пока статью
держит пакет, выданный до замены (repository.ArticlesBusy), и отбрасывает тех, кого за это время
отверг гейт, скрыли, отметили в выпуск или унесло в архив (still_applicable): за них модель не
платится.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from oiltech_digest import feed_window, network_policy
# Модулем, а не функцией: тестовая фикстура подменяет connection.get_connection.
from oiltech_digest.db import connection, repository
from oiltech_digest.ingestion.article_fetcher import MIN_GAIN_RATIO
from oiltech_digest.processing import pipeline

logger = logging.getLogger(__name__)

RECOMPUTE_STAGES = ["summary", "tagging", "scoring"]
# Пометка задачи: по ней выдача отбрасывает статьи, которым пересчёт больше не нужен.
PAYLOAD_FLAG = "after_full_text"
BATCH_SIZE = 20


def _seen_chars(text: str | None) -> int:
    """Сколько знаков текста увидит модель в сути, теге и балле."""
    return len(pipeline._compact(text or "", pipeline.ARTICLE_PROMPT_TEXT_CHARS))


def substantial_gain(old: str | None, new: str | None) -> bool:
    """Новое тело даёт модели минимум вдвое больше текста, чем прежнее.

    Порог — MIN_GAIN_RATIO дозагрузки («извлечённое явно лучше сохранённого»). Мерим то, что
    читает промпт: пробелы схлопнуты, дальше ARTICLE_PROMPT_TEXT_CHARS не видно, — простыня
    вместо 4000 знаков для модели 4000 → 6000, и платить за такой пересчёт незачем."""
    seen = _seen_chars(new)
    return seen > 0 and seen >= MIN_GAIN_RATIO * _seen_chars(old)


def after_bodies(replaced: Iterable[tuple[int, str | None, str | None]]) -> int:
    """Пути дозагрузки: (id, прежнее тело, новое) записанных тел → сколько статей ушло в пересчёт.

    Тело к этому моменту уже записано. Сбой постановки не роняет запись: повтор задачи дозагрузки
    прироста уже не увидит (тело то же), так что падение ничего бы не вернуло, — сбой уходит в
    лог с id статей."""
    gained = [int(article_id) for article_id, old, new in replaced if substantial_gain(old, new)]
    if not gained:
        return 0
    try:
        return len(enqueue_after_body(gained)["articles"])
    except Exception:  # noqa: BLE001 - записанное тело дороже постановки пересчёта
        logger.exception("пересчёт после полного текста НЕ поставлен: статьи %s", gained[:50])
        return 0


def _allowed_sql() -> str:
    """Пересчитывать можно: статья видна в ленте, её месяц открыт (архив — только просмотр,
    feed_window) и её никто не выбрал в выпуск — ни отметкой «В дайджест», ни в черновике."""
    return (
        f"{feed_window.visible_sql('a', 'c', 's')} AND {feed_window.current().sql('a')}"
        " AND NOT EXISTS (SELECT 1 FROM user_article_states u"
        "                 WHERE u.article_id = a.id AND u.status = 'digest')"
        " AND NOT EXISTS (SELECT 1 FROM monthly_digest_items di WHERE di.article_id = a.id)"
    )


def enqueue_after_body(article_ids: Iterable[int], *, batch_size: int = BATCH_SIZE) -> dict[str, Any]:
    """Поставить пересчёт сути, тега и балла статьям, чьё тело только что заменено с приростом.

    Возвращает {"articles": поставленные, "jobs": id задач, "skipped": причина или None}."""
    ids = list(dict.fromkeys(int(item) for item in article_ids))
    outcome: dict[str, Any] = {"articles": [], "jobs": [], "skipped": None}
    if not ids:
        return outcome
    decision = network_policy.route_ai_bulk()
    if decision.execution_region != "external":
        # Местный конвейер пометки only не знает, а у посчитанной статьи готовые стадии
        # пропускает: задача прошла бы молча впустую (как у enqueue-rescore).
        outcome["skipped"] = "ИИ не во внешнем контуре — пересчёт не ставится"
        logger.warning("пересчёт после полного текста: %s; статьи %s", outcome["skipped"], ids[:50])
        return outcome
    with connection.get_connection() as conn:
        in_work = repository.process_articles_in_work(conn)
        queued = _queued_recompute(conn)
        rows = conn.execute(
            f"""
            SELECT a.id
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE a.id = ANY(%s)
              AND {_allowed_sql()}
              AND (
                    (c.relevant IS TRUE AND (
                        c.summary IS NOT NULL
                        OR EXISTS (SELECT 1 FROM article_tags t WHERE t.article_id = a.id)
                        OR EXISTS (SELECT 1 FROM article_scores sc WHERE sc.article_id = a.id)))
                 -- Пакет выдан до замены тела: его суть и балл лягут по прежнему тексту.
                 OR a.id = ANY(%s)
              )
            ORDER BY a.id
            """,
            (ids, in_work),
        ).fetchall()
    chosen = [int(row[0]) for row in rows if int(row[0]) not in queued]
    batch = max(1, batch_size)
    for start in range(0, len(chosen), batch):
        chunk = chosen[start : start + batch]
        job = repository.create_background_job(
            "process_articles",
            {"article_ids": chunk, "limit": len(chunk), "offline": False, "only": RECOMPUTE_STAGES,
             PAYLOAD_FLAG: True},
            queue_name=decision.queue_name,
            execution_region=decision.execution_region,
            capability=decision.capability,
        )
        outcome["jobs"].append(int(job["id"]))
    outcome["articles"] = chosen
    if chosen:
        logger.info("пересчёт после полного текста: статьи %s → задачи %s (%s)",
                    chosen[:50], outcome["jobs"], decision.queue_name)
    return outcome


def _queued_recompute(conn) -> set[int]:
    """Статьи в ещё не выданных задачах, которые пересчитают суть, тег и балл по тексту на момент
    выдачи: явный список без пометки only (весь конвейер) или с only, где есть все три стадии.
    Отложенная выдачей задача (ArticlesBusy) тоже здесь — она вернулась в queued."""
    rows = conn.execute(
        """
        SELECT DISTINCT (jsonb_array_elements_text(payload_json->'article_ids'))::bigint
        FROM background_jobs
        WHERE kind = 'process_articles'
          AND status = 'queued'
          AND jsonb_typeof(payload_json->'article_ids') = 'array'
          AND (jsonb_typeof(payload_json->'only') IS DISTINCT FROM 'array'
               OR payload_json->'only' @> %s::jsonb)
        """,
        (json.dumps(RECOMPUTE_STAGES),),
    ).fetchall()
    return {int(row[0]) for row in rows}


def still_applicable(article_ids: list[int]) -> set[int]:
    """При выдаче задачи пересчёта: кому он ещё нужен.

    Пока задача ждала пакет, выданный с анонсом, его гейт мог статью отвергнуть, её могли скрыть
    или отметить в выпуск, а месяц — закрыться. Оставляем релевантные по гейту, видимые, в
    открытом месяце и никем не выбранные в выпуск."""
    if not article_ids:
        return set()
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT a.id
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE a.id = ANY(%s) AND c.relevant IS TRUE AND {_allowed_sql()}
            """,
            (list(article_ids),),
        ).fetchall()
    return {int(row[0]) for row in rows}
