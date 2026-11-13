"""CRUD and query helpers for sources, articles, auth and AI processing."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
import logging
from pathlib import Path
import re
from typing import Literal, NamedTuple, get_args
from urllib.parse import urlsplit

from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest import auth, config, contract, feed_window, lanes, word_forms
from oiltech_digest.ingestion import normalize, verdicts
from oiltech_digest.db import article_tail_store, marks
from oiltech_digest.db.connection import get_connection
from oiltech_digest.feed_window import FeedWindow, period_month_sql, signal_month_sql, visible_sql
from oiltech_digest.scoring_profiles import ARTICLE_SCORING_PROFILE, check_profile

logger = logging.getLogger(__name__)

# Единый источник правды для набора статусов отметки статьи (общей с #111).
# ДОЛЖЕН совпадать с union Article["status"] во фронте (frontend/src/api/types.ts).
# api.ArticlePatch.status импортирует ArticleStatus отсюда (валидация 422), dashboard_stats
# проецирует счётчики по ARTICLE_STATUS_VALUES, так Python-половина не рассинхронится:
# добавил статус в Literal → он автоматически появился и в кортеже (get_args).
# 12.09: статус `review` («На проверке») убран по требованию заказчика. Он был
# декоративным: ничего не скрывал и ни на что не влиял, но при этом был ЕДИНСТВЕННЫМ
# статусом, который система ставила сама (снятие статьи из дайджеста). Теперь этот
# переход ведёт в `archive`, а сам `archive` из пустого счётчика стал рабочим: он
# скрывает статью из ленты, как `noise`/`duplicate`, но без штрафа баллу источника
# (см. api.list_articles и repository.compute_source_quality_rows).
ArticleStatus = Literal["new", "digest", "archive", "noise", "duplicate"]
ARTICLE_STATUS_VALUES: tuple[ArticleStatus, ...] = get_args(ArticleStatus)

# Служебный тег-приёмник: сюда падала статья, не совпавшая ни с одной тематикой (у сигнала ленты это тема до
# ответа «Карточки»). Он не тематика, а предохранитель:
# без него классификация уводит весь непонятый поток в ПЕРВЫЙ тег списка. Поэтому его нельзя выключить ни
# сохранением экрана тегов, ни удалением: 13.09 он выключился именно так и молча,
# а страховка «больше половины» такой случай не ловит (один тег из четырнадцати).
SYSTEM_TAG_UNCLASSIFIED = "Не классифицировано / новая тема"

# ---------------------------------------------------------------------------
#  sources
# ---------------------------------------------------------------------------

def upsert_source(rec: dict) -> str:
    """Вставить/обновить источник по name (естественный ключ).

    Ключ, (name, source_type): один бренд может иметь сайт и Telegram-канал с
    одинаковым именем, но разным типом (это разные источники). При конфликте
    обновляются только описательные поля (url, category, priority). rss_url /
    parse_strategy / enabled НЕ трогаем: ими управляет discover-rss.
    Возвращает 'inserted' либо 'updated'.
    """
    with get_connection() as conn:
        rec = {**rec, "update_frequency": rec.get("update_frequency")}
        cur = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, rss_url, enabled,
                                 parse_strategy, category, update_frequency, priority)
            VALUES (%(name)s, %(source_type)s, %(url)s, %(rss_url)s,
                    COALESCE(%(enabled)s, TRUE), %(parse_strategy)s,
                    %(category)s, %(update_frequency)s, COALESCE(%(priority)s, 1.0))
            ON CONFLICT (name, source_type) DO UPDATE SET
                url         = EXCLUDED.url,
                category    = EXCLUDED.category,
                update_frequency = EXCLUDED.update_frequency,
                priority    = EXCLUDED.priority,
                updated_at  = now()
            RETURNING (xmax = 0) AS inserted
            """,
            rec,
        )
        inserted = cur.fetchone()[0]
        conn.commit()
    return "inserted" if inserted else "updated"


def get_enabled_sources(strategy: str | None = None) -> list[dict]:
    """Включённые источники, опционально с фильтром по parse_strategy."""
    query = "SELECT * FROM sources WHERE enabled = TRUE"
    params: list = []
    if strategy is not None:
        query += " AND parse_strategy = %s"
        params.append(strategy)
    query += " ORDER BY id"
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(query, params)
        return cur.fetchall()


def get_sources_for_discovery(only_missing: bool = True,
                              source_id: int | None = None,
                              limit: int | None = None) -> list[dict]:
    """Источники-кандидаты на автообнаружение RSS (не Telegram и не Playwright).

    `playwright`: осознанно выставленная вручную стратегия (JS/WAF-сайты); discover-rss
    НЕ должен её сбрасывать в request, иначе оверрайды откатываются на каждом цикле.

    По той же причине исключаются источники с заданным `listing_url`: он ставится только
    осознанно (реестр оверрайдов или админка), а discover_feed пробует НЕ его, а `url`:
    главную страницу издания. У федеральных СМИ главная всегда рекламирует RSS, поэтому
    update_source_rss перезаписал бы parse_strategy на 'rss' с ОБЩИМ фидом издания:
    ровно тем мусором, ради которого источник и правился (#61). Порядок на деплое делает
    это неизбежным: bootstrap применяет реестр, а первый же цикл планировщика запускает
    discover-rss (RUN_DISCOVER_ON_START=1), то есть откат случился бы до первого парса.
    """
    query = ("SELECT * FROM sources WHERE enabled = TRUE "
             "AND parse_strategy NOT IN ('telegram', 'playwright') "
             "AND (listing_url IS NULL OR listing_url = '')")
    params: list = []
    if only_missing:
        query += " AND (rss_url IS NULL OR rss_url = '')"
    if source_id is not None:
        query += " AND id = %s"
        params.append(source_id)
    query += " ORDER BY id"
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(query, params)
        return cur.fetchall()


def update_source_rss(source_id: int, rss_url: str | None, parse_strategy: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE sources SET rss_url = %s, parse_strategy = %s, updated_at = now() WHERE id = %s",
            (rss_url, parse_strategy, source_id),
        )
        conn.commit()


def set_source_enabled(source_id: int, enabled: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE sources SET enabled = %s, updated_at = now() WHERE id = %s",
            (enabled, source_id),
        )
        conn.commit()


def add_rss_source(name: str, rss_url: str, source_type: str = "RSS",
                   url: str | None = None, priority: float = 1.0,
                   category: str | None = None,
                   update_frequency: str | None = None,
                   parse_strategy: str = "rss") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, rss_url, enabled,
                                 parse_strategy, category, update_frequency, priority)
            VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s, %s)
            ON CONFLICT (name, source_type) DO UPDATE SET
                url = EXCLUDED.url,
                rss_url = EXCLUDED.rss_url,
                -- Архивный источник повторным добавлением НЕ воскрешаем. Иначе получилось
                -- бы худшее из двух: сбор возобновился (enabled=TRUE), а статьи всё равно
                -- скрыты (archived_at не NULL) — источник молча жжёт ИИ в никуда.
                -- Вернуть в работу можно только явно, через /api/sources/{id}/unarchive.
                enabled = (sources.archived_at IS NULL),
                parse_strategy = EXCLUDED.parse_strategy,
                category = EXCLUDED.category,
                update_frequency = EXCLUDED.update_frequency,
                priority = EXCLUDED.priority,
                updated_at = now()
            RETURNING id
            """,
            (name, source_type, url, rss_url, parse_strategy, category, update_frequency, priority),
        )
        source_id = cur.fetchone()[0]
        conn.commit()
        return source_id


def list_sources(search: str | None = None, limit: int = 50) -> list[dict]:
    query = "SELECT * FROM sources"
    params: list = []
    if search:
        query += " WHERE name ILIKE %s OR url ILIKE %s OR rss_url ILIKE %s OR listing_url ILIKE %s"
        like = f"%{search}%"
        params.extend([like, like, like, like])
    query += " ORDER BY enabled DESC, parse_strategy NULLS LAST, id LIMIT %s"
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(query, params)
        return cur.fetchall()


def get_source(source_id: int) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM sources WHERE id = %s", (source_id,))
        return cur.fetchone()


def touch_last_parsed(source_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE sources SET last_parsed_at = now() WHERE id = %s", (source_id,)
        )
        conn.commit()


def set_sources_network_region(ids: list[int], region: str) -> int:
    """Проставить network_region (auto|ru|external) источникам по списку id.

    При уходе в/из external сбрасываем request-состояние (last_seen/hash), чтобы
    смена пути парсинга не коротила на старом хэше. Возвращает число обновлённых строк."""
    if not ids:
        return 0
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE sources SET network_region = %s, last_listing_hash = NULL, "
            "last_seen_article_url = NULL, last_seen_published_at = NULL, updated_at = now() "
            "WHERE id = ANY(%s)",
            (region, list(ids)),
        )
        conn.commit()
        return cur.rowcount


# ---------------------------------------------------------------------------
#  Разведка источников и обратная связь
# ---------------------------------------------------------------------------


SIGNAL_FEEDBACK_EVENTS = (
    "added_to_digest",
    "marked_noise",
    "marked_duplicate",
    "tag_changed",
    "score_changed",
    "status_changed",
    "comment_added",
    # Правка заголовка и сути по согласованному улучшению карточки из журнала ОС (#189, feedback_changes).
    "proposal_applied",
)


def normalize_domain(url: str) -> str:
    parsed = urlsplit((url or "").strip())
    host = (parsed.netloc or parsed.path.split("/")[0]).lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split("@")[-1].split(":")[0]


def record_signal_feedback_event(
    article_id: int | None,
    event_type: str,
    *,
    signal_id: int | None = None,
    signal_evidence_id: int | None = None,
    source_url: str | None = None,
    signal_title: str | None = None,
    user_id: int | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    comment: str | None = None,
    verdict: str | None = None,
    reason: str | None = None,
    corrected_title: str | None = None,
    corrected_thesis: str | None = None,
    duplicate_of_signal_id: int | None = None,
    applied: bool = True,
) -> int:
    """Запись журнала ОС. applied (#188): явные действия отметки применены сразу (отметка администратора, правка
    материала выпуска); False: отметка участника без прав администратора: записана и видна блоком, а её правки
    заголовка и сути на карточку не ложатся (_SIGNAL_CORRECTIONS_LATERAL)."""
    if event_type not in SIGNAL_FEEDBACK_EVENTS:
        raise ValueError(f"Unknown signal feedback event_type: {event_type}")
    if article_id is None and signal_id is None and not source_url:
        raise ValueError("signal feedback requires article_id, signal_id or source_url")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO signal_feedback_events
              (article_id, signal_id, signal_evidence_id, source_url, signal_title,
               user_id, event_type, old_value, new_value, comment,
               verdict, reason, corrected_title, corrected_thesis, duplicate_of_signal_id, applied)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                article_id,
                signal_id,
                signal_evidence_id,
                source_url,
                signal_title,
                user_id,
                event_type,
                old_value,
                new_value,
                comment,
                verdict,
                reason,
                corrected_title,
                corrected_thesis,
                duplicate_of_signal_id,
                applied,
            ),
        )
        event_id = int(cur.fetchone()[0])
        conn.commit()
        return event_id


def seed_signal_radar_topics(topics: list[dict]) -> int:
    changed = 0
    with get_connection() as conn:
        for order, topic in enumerate(topics, start=1):
            cur = conn.execute(
                """
                INSERT INTO signal_radar_topics (
                  name, description, query_seeds_json, industry_scope_json, enabled, sort_order
                )
                VALUES (%s, %s, %s, %s, TRUE, %s)
                ON CONFLICT (name) DO UPDATE SET
                  description = EXCLUDED.description,
                  query_seeds_json = EXCLUDED.query_seeds_json,
                  industry_scope_json = EXCLUDED.industry_scope_json,
                  enabled = TRUE,
                  sort_order = EXCLUDED.sort_order,
                  updated_at = now()
                RETURNING id
                """,
                (
                    topic["name"],
                    topic.get("description"),
                    Json(_jsonable(topic.get("query_seeds") or [])),
                    Json(_jsonable(topic.get("industry_scope") or [])),
                    order,
                ),
            )
            if cur.fetchone():
                changed += 1
        conn.commit()
    return changed


def list_signal_radar_topics(*, enabled_only: bool = True) -> list[dict]:
    query = "SELECT * FROM signal_radar_topics"
    params: list = []
    if enabled_only:
        query += " WHERE enabled"
    query += " ORDER BY sort_order, name"
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(query, params)
        return cur.fetchall()


# Кто поставил значение карточки сигнала (#96, signals.card_set_by): агент «Карточка сигнала», судья
# радара, агент «Отбор» (#130: отрасль, связь и применение, категория) или человек (правки людей внесёт
# сервис ОС).
CARD_SET_BY_AGENT = "agent"
CARD_SET_BY_JUDGE = "radar_judge"
CARD_SET_BY_OTBOR = "otbor"
CARD_SET_BY_HUMAN = "human"
# Статья, которую решает агент «Отбор», а не гейт ленты (articles.selector, #130): с 08.11 так вставляется каждая
# новая статья (флаг и гейт сняты); пустой путь у статьи, вставленной до включения «Отбора».
ARTICLE_SELECTOR_OTBOR = "otbor"
# То же у сигнала (signals.selector, #131): с 08.11 так заводится каждая находка поиска и каждый материал коллеги
# (флаг снят); пустой путь у заведённых до включения «Отбора». Пока решения нет, сигнал не виден нигде (правила показа
# и «Карточка» его не берут).
SIGNAL_SELECTOR_OTBOR = "otbor"
# Находка пробного прогона широкого поиска (#138, signal_search_trial): её не видят витрины, разделы отклонённого,
# шаги конвейера (они берут только NULL или «otbor») и снимок дедупа радара; «Отбор» решает её в тени проверки #134,
# и решение лежит строкой таблицы проверки (otbor_shadow_store.write_signal), а не в полях сигнала.
SIGNAL_SELECTOR_TRIAL = "otbor_trial"
# Поля карточки сигнала, которые ставит судья радара: их значения агент у находки радара не
# перезаписывает, а пустые: заполняет.
RADAR_JUDGE_CARD_FIELDS = ("event_date", "oilfield_relevance", "oilfield_application", "why_interesting")


def upsert_signal(signal: dict) -> int:
    """Карточка радара по ключу: новая: вставка, найденная снова: обновление.

    Текст карточки закрытого месяца, которую кто-то выбрал «в дайджест» (заголовки, суть, тезис,
    тема), повторная находка не переписывает: это текст закрытого выпуска, а он только на просмотр
    (решение владельца 30.09). Месяц карточки: месяц поступления на радар
    (feed_window.signal_month_sql), окно: то же, что у отказа отметке «в дайджест». Карточку,
    которой нет ни в одном выпуске, радар обновляет, как раньше (ревью #88: меньше следа в радаре).
    Остальное: балл, зрелость, число ссылок, время находки: обновляется всегда; ссылки пишет
    upsert_signal_evidence."""
    closed = (
        f"({feed_window.current().closed_sql(signal_month_sql('signals'))}"
        f" AND {marks.signal_marked_sql('signals.id', 'digest')})"
    )

    def by_human(field: str) -> str:
        return f"signals.card_set_by->>'{field}' = '{CARD_SET_BY_HUMAN}'"

    # Кто поставил поля судьи (#96): присланные: судья, но значение человека судья не переписывает.
    judge_set_by = ", ".join(
        f"'{field}', CASE WHEN {by_human(field)} THEN NULL ELSE EXCLUDED.card_set_by->>'{field}' END"
        for field in RADAR_JUDGE_CARD_FIELDS
    )
    with get_connection() as conn:
        cur = conn.execute(
            f"""
            INSERT INTO signals (
              signal_key, title, title_ru, theme, summary, thesis, transferability, maturity, confidence, score,
              why_now, why_not_noise, companies_json, industries_json, evidence_count,
              interest_score, why_interesting,
              signal_category, event_date, mixed_events, mixed_events_reason,
              score_profile, score_items_json, criteria_snapshot,
              oilfield_relevance, oilfield_application, filter_stage, filter_reason,
              verdict_reason, decision_log, card_set_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (signal_key) DO UPDATE SET
              title = CASE WHEN {closed} THEN signals.title ELSE EXCLUDED.title END,
              title_ru = CASE WHEN {closed} THEN signals.title_ru ELSE EXCLUDED.title_ru END,
              theme = CASE WHEN {closed} THEN signals.theme ELSE EXCLUDED.theme END,
              summary = CASE WHEN {closed} THEN signals.summary ELSE EXCLUDED.summary END,
              thesis = CASE WHEN {closed} THEN signals.thesis ELSE EXCLUDED.thesis END,
              transferability = EXCLUDED.transferability,
              maturity = EXCLUDED.maturity,
              confidence = EXCLUDED.confidence,
              score = EXCLUDED.score,
              why_now = EXCLUDED.why_now,
              why_not_noise = EXCLUDED.why_not_noise,
              companies_json = EXCLUDED.companies_json,
              industries_json = EXCLUDED.industries_json,
              evidence_count = EXCLUDED.evidence_count,
              -- Балл интереса — сравнение внутри пачки; тема из одного кандидата его
              -- не получает, и прошлый балл не должен стираться NULL'ом.
              interest_score = COALESCE(EXCLUDED.interest_score, signals.interest_score),
              why_interesting = CASE WHEN {by_human("why_interesting")} THEN signals.why_interesting
                                     ELSE COALESCE(NULLIF(EXCLUDED.why_interesting, ''), signals.why_interesting) END,
              -- Поля судьи (29.09): результат старой сборки NL их не несёт — прежние не стираем.
              signal_category = COALESCE(EXCLUDED.signal_category, signals.signal_category),
              event_date = CASE WHEN {by_human("event_date")} THEN signals.event_date
                                ELSE COALESCE(EXCLUDED.event_date, signals.event_date) END,
              -- Дата судьи — без источника (откуда она у судьи, неизвестно): источник агента «Карточка
              -- сигнала» (#96) к ней не относится.
              event_date_source = CASE WHEN EXCLUDED.event_date IS NOT NULL AND NOT {by_human("event_date")}
                                       THEN NULL ELSE signals.event_date_source END,
              -- Дата судьи — день (#132): точность месяца или года от агента к ней не относится.
              event_date_precision = CASE WHEN EXCLUDED.event_date IS NOT NULL AND NOT {by_human("event_date")}
                                          THEN NULL ELSE signals.event_date_precision END,
              mixed_events = COALESCE(EXCLUDED.mixed_events, signals.mixed_events),
              mixed_events_reason = COALESCE(EXCLUDED.mixed_events_reason, signals.mixed_events_reason),
              oilfield_relevance = CASE WHEN {by_human("oilfield_relevance")} THEN signals.oilfield_relevance
                                        ELSE COALESCE(EXCLUDED.oilfield_relevance, signals.oilfield_relevance) END,
              oilfield_application = CASE WHEN {by_human("oilfield_application")} THEN signals.oilfield_application
                                          ELSE COALESCE(EXCLUDED.oilfield_application, signals.oilfield_application) END,
              -- Кто поставил значения карточки (#96): присланные судьёй поля — его, кроме правок человека;
              -- остальные (агента «Карточка сигнала») не трогаем.
              card_set_by = COALESCE(signals.card_set_by, '{{}}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object({judge_set_by})),
              -- Отсев — решение этого прогона: принятая снова карточка выходит на радар.
              filter_stage = EXCLUDED.filter_stage,
              filter_reason = EXCLUDED.filter_reason,
              -- Ход решения — этого прогона; старая сборка NL его не несёт — прежний не стираем.
              verdict_reason = COALESCE(EXCLUDED.verdict_reason, signals.verdict_reason),
              decision_log = COALESCE(EXCLUDED.decision_log, signals.decision_log),
              -- Балл и его происхождение — всегда вместе: score выше уже новый.
              score_profile = EXCLUDED.score_profile,
              score_items_json = EXCLUDED.score_items_json,
              criteria_snapshot = EXCLUDED.criteria_snapshot,
              last_seen_at = now(),
              updated_at = now()
            RETURNING id
            """,
            (
                signal["signal_key"],
                signal["title"],
                signal.get("title_ru"),
                signal["theme"],
                signal.get("summary"),
                signal.get("thesis"),
                signal.get("transferability"),
                signal.get("maturity") or "watch",
                float(signal.get("confidence") or 0),
                float(signal.get("score") or 0),
                signal.get("why_now"),
                signal.get("why_not_noise"),
                Json(_jsonable(signal.get("companies") or [])),
                Json(_jsonable(signal.get("industries") or [])),
                int(signal.get("evidence_count") or 0),
                float(signal["interest_score"]) if signal.get("interest_score") is not None else None,
                (signal.get("why_interesting") or None),
                signal.get("signal_category"),
                signal.get("event_date") or None,
                signal.get("mixed_events") if isinstance(signal.get("mixed_events"), bool) else None,
                (signal.get("mixed_events_reason") or None),
                signal.get("score_profile"),
                Json(_jsonable(signal["score_items"])) if signal.get("score_items") else None,
                Json(_jsonable(signal["criteria_snapshot"])) if signal.get("criteria_snapshot") else None,
                signal.get("oilfield_relevance"),
                (signal.get("oilfield_application") or None),
                signal.get("filter_stage") or None,
                (signal.get("filter_reason") or None),
                (signal.get("verdict_reason") or None),
                Json(_jsonable(signal["decision_log"])) if signal.get("decision_log") else None,
                Json({field: CARD_SET_BY_JUDGE for field in RADAR_JUDGE_CARD_FIELDS if signal.get(field)}),
            ),
        )
        signal_id = int(cur.fetchone()[0])
        conn.commit()
        return signal_id


def upsert_signal_evidence(signal_id: int, evidence: dict) -> int:
    with get_connection() as conn:
        previous = conn.execute(
            "SELECT signal_id FROM signal_evidence WHERE source_url = %s", (evidence["source_url"],)
        ).fetchone()
        cur = conn.execute(
            f"""
            INSERT INTO signal_evidence (
              signal_id, article_id, source_url, title, title_ru, publisher, published_at,
              evidence_type, extracted_fact, summary_ru, strength, raw_payload_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_url) DO UPDATE SET
              -- Ссылка видимой карточки к другой не переезжает: иначе у прежней не
              -- оставалось источника, а счётчик показывал старое число (сигнал 97,
              -- замечание 22.09). Переезд — из скрытого дубля в главную и из карточки,
              -- скрытой качеством или архивом, если её не разбирал человек (02.10).
              -- Отсеянная (filter_stage) — тоже скрытая качеством: принятое забирает её ссылку.
              signal_id = CASE
                WHEN EXISTS (
                  SELECT 1 FROM signals owner
                  WHERE owner.id = signal_evidence.signal_id
                    AND owner.id <> EXCLUDED.signal_id
                    AND ({_signal_holds_evidence_sql("owner")}
                         OR {_feed_holds_finding_link_sql("owner", "signal_evidence")})
                ) THEN signal_evidence.signal_id
                ELSE EXCLUDED.signal_id
              END,
              article_id = COALESCE(EXCLUDED.article_id, signal_evidence.article_id),
              title = EXCLUDED.title,
              title_ru = EXCLUDED.title_ru,
              publisher = EXCLUDED.publisher,
              published_at = COALESCE(EXCLUDED.published_at, signal_evidence.published_at),
              evidence_type = EXCLUDED.evidence_type,
              extracted_fact = EXCLUDED.extracted_fact,
              summary_ru = EXCLUDED.summary_ru,
              strength = EXCLUDED.strength,
              raw_payload_json = EXCLUDED.raw_payload_json,
              updated_at = now()
            RETURNING id, signal_id
            """,
            (
                signal_id,
                evidence.get("article_id"),
                evidence["source_url"],
                evidence["title"],
                evidence.get("title_ru"),
                evidence.get("publisher"),
                evidence.get("published_at"),
                evidence.get("evidence_type") or "article",
                evidence.get("extracted_fact"),
                evidence.get("summary_ru"),
                float(evidence.get("strength") or 0),
                Json(_jsonable(evidence.get("raw_payload") or evidence)),
            ),
        )
        evidence_id, owner_id = cur.fetchone()
        conn.commit()
    if previous is not None and int(previous[0]) != int(owner_id):
        # Ссылка переехала: у прежней карточки счётчик не должен показывать старое число.
        refresh_signal_evidence_count(int(previous[0]))
        _retire_emptied_filtered_card(int(previous[0]), taken_by=int(owner_id))
    return int(evidence_id)


def _retire_emptied_filtered_card(signal_id: int, *, taken_by: int) -> None:
    """Отсеянная карточка, у которой найденное снова забрало последнюю ссылку, дубль забравшей:
    иначе в «Отсеянных» висела бы пустая копия события со старой причиной. Забравшая: принятая
    (событие на радаре) или, если пустая отсеяна до вердикта (поиск, сбой судьи), забракованная
    судьёй или ревью пачки: событие разобрано дальше, и причина: у неё (17.10). Дубль не
    удаляется (merged_into_signal_id, как у дедупа). Разобранную человеком это не касается: её
    ссылки не переезжают (_signal_holds_evidence_sql).

    Обе карточки в этот момент не склеены, а склеенная ни ссылку не забирает, ни повторным
    отсевом не оживает, кольца дублей не бывает (ревью 05.10: склеенная хозяйка давала
    A→B→C→A, и событие пропадало из обоих списков). Забракованная судьёй дублем отсеянной не
    становится: её ключ носят кандидаты судьи, и принятый потом повтор ушёл бы в скрытую карточку
    (_reconcile_with_owner). У отсеянной до вердикта ключ свой (filtered:…): его кандидаты не носят."""
    with get_connection() as conn:
        retired = conn.execute(
            """
            UPDATE signals
            SET merged_into_signal_id = %(taken_by)s, merge_reason = %(reason)s, updated_at = now()
            WHERE id = %(id)s
              AND filter_stage IS NOT NULL
              AND merged_into_signal_id IS NULL
              AND NOT EXISTS (SELECT 1 FROM signal_evidence WHERE signal_id = %(id)s)
              AND EXISTS (SELECT 1 FROM signals t WHERE t.id = %(taken_by)s AND t.merged_into_signal_id IS NULL
                          AND (t.filter_stage IS NULL
                               OR (t.filter_stage = ANY(%(verdict)s)
                                   AND signals.filter_stage = ANY(%(before_verdict)s))))
            RETURNING id
            """,
            {"id": signal_id, "taken_by": taken_by,
             "reason": f"найдено снова: ссылку забрала карточка {taken_by}",
             "verdict": list(RADAR_VERDICT_STAGES), "before_verdict": list(RADAR_STAGES_BEFORE_VERDICT)},
        ).fetchone()
        if retired is not None:
            # Группа, в один шаг, как у mark_signal_merged: пустая до вердикта, ставшая дублем
            # этой карточки, дубль забравшей.
            conn.execute(
                "UPDATE signals SET merged_into_signal_id = %s, updated_at = now() WHERE merged_into_signal_id = %s",
                (taken_by, signal_id),
            )
        conn.commit()


def signal_publication_count_sql(alias: str = "s") -> str:
    """Число публикаций сигнала (evidence_count): его ссылки, а у сигнала ленты: и его статья,
    когда её адрес держит карточка радара (ссылка на адрес одна, feed_signals): в списке
    публикаций она показана и у него (list_radar_evidence), и число должно с ним сходиться."""
    a = alias
    return (
        f"((SELECT COUNT(*) FROM signal_evidence ce WHERE ce.signal_id = {a}.id)"
        f" + CASE WHEN {a}.origin = '{SIGNAL_ORIGIN_FEED}' AND EXISTS ("
        "SELECT 1 FROM articles ca JOIN signal_evidence cb ON cb.source_url = ca.url"
        f" WHERE ca.id = {a}.origin_article_id AND cb.signal_id IS DISTINCT FROM {a}.id)"
        " THEN 1 ELSE 0 END)::int"
    )


def platform_publications_sql(alias: str = "s") -> str:
    """Публикаций у события для «Решения платформы» (#132): публикации сигнала (signal_publication_count_sql) и ссылки
    склеенных в него дублей: «Дедуп» добавляет публикацию и тем, и другим путём."""
    return (f"({signal_publication_count_sql(alias)}"
            " + (SELECT COUNT(*) FROM signal_evidence de JOIN signals d ON d.id = de.signal_id"
            f" WHERE d.merged_into_signal_id = {alias}.id))")


def platform_decision_sql(alias: str = "s") -> str:
    """«Решение платформы» сигнала после «Отбора» (#132, спека #128), считает код: у отклонённой: «отклонено»; у
    «другого» его нет; сильный по баллам, «кандидат», с SIGNAL_CONFIRMED_PUBLICATIONS публикациями и больше:
    «подтверждено»; полезный и ниже: «наблюдать»; без баллов (ещё не оценён): «наблюдать». Решённое до «Отбора»
    живёт по прежним правилам: его «Решение радара» ставил судья, выражение его не трогает."""
    a = alias
    return (
        f"(CASE WHEN {a}.otbor_decision IS NULL THEN {a}.maturity"
        f" WHEN {a}.otbor_decision = '{feed_window.OTBOR_REJECT}' THEN 'reject'"
        f" WHEN {a}.signal_category = 'other' THEN NULL"
        f" WHEN {a}.score_band = 'strong' THEN CASE WHEN {platform_publications_sql(a)}"
        f" >= {int(config.SIGNAL_CONFIRMED_PUBLICATIONS)} THEN 'proven' ELSE 'shortlist' END"
        f" WHEN {a}.score_band IS NOT NULL OR {a}.maturity IS NULL OR {a}.maturity = 'reject' THEN 'watch'"
        f" ELSE {a}.maturity END)"
    )


def refresh_platform_decisions(signal_ids: Sequence[int], *, conn) -> None:
    """Пересчитать «Решение платформы» сигналов после «Отбора» (#132): после баллов, решения «Отбора» и когда у события
    стало больше публикаций. Внутри транзакции того, кто их поменял (conn): решение видно вместе с причиной."""
    ids = sorted({int(value) for value in signal_ids if value is not None})
    if ids:
        conn.execute(f"UPDATE signals s SET maturity = {platform_decision_sql('s')}, updated_at = now()"
                     f" WHERE s.id = ANY(%s) AND s.otbor_decision IS NOT NULL AND s.maturity IS DISTINCT FROM"
                     f" {platform_decision_sql('s')}", (ids,))


def refresh_signal_evidence_count(signal_id: int) -> int:
    """Число публикаций сигнала; у события, к которому «Дедуп» добавил публикацию, и «Решение платформы» (#132): у
    самого сигнала и у главной карточки, если он дубль."""
    with get_connection() as conn:
        cur = conn.execute(
            f"""
            UPDATE signals s
            SET evidence_count = {signal_publication_count_sql("s")},
                updated_at = now()
            WHERE s.id = %s
            RETURNING evidence_count, merged_into_signal_id
            """,
            (signal_id,),
        )
        row = cur.fetchone()
        if row:
            refresh_platform_decisions([signal_id, row[1]], conn=conn)
        conn.commit()
        return int(row[0]) if row else 0


# Разобранная карточка, у неё есть суждение человека: вердикт, комментарий или статус,
# отличный от «наблюдать» (дайджест, архив, шум, дубль). Дедуп такую не прячет и не
# склеивает с другой разобранной; её ссылки радар не приносит повторно.
_SIGNAL_REVIEWED_SQL = """(
  EXISTS (SELECT 1 FROM signal_feedback_events sfe_r
          WHERE sfe_r.signal_id = {alias}.id
            AND (COALESCE(sfe_r.verdict, '') <> '' OR sfe_r.event_type = 'comment_added'))
  OR """ + marks.signal_marked_other_than_watch_sql("{alias}.id") + """
)"""
# Выбранная кем-то «в дайджест»: её не прячет ничто: ни дедуп, ни качество, ни архив. Иначе
# она исчезла бы с экрана и осталась в выпуске без способа её оттуда убрать (mark_signal_merged).
_SIGNAL_IN_DIGEST_SQL = marks.signal_marked_sql("{alias}.id", "digest")
# Положительные вердикты: те же, что у дедупа (signal_dedup._POSITIVE_VERDICTS). «Оценка
# завышена», тоже одобрение: сигнал годный, завышен только балл.
SIGNAL_POSITIVE_VERDICTS = ("strong_signal", "approved", "watch_later", "needs_better_source", "bad_translation",
                            "overrated")
# Одобрена человеком: последний вердикт: положительный. Отказ, «не тот блок», «шум» или
# комментарий без вердикта карточку одобренной не делают. Только применённые отметки (#188): оценка участника без прав
# администратора карточку не прячет и не возвращает.
_SIGNAL_APPROVED_SQL = (
    "COALESCE((SELECT sfe_a.verdict FROM signal_feedback_events sfe_a"
    " WHERE sfe_a.signal_id = {alias}.id AND sfe_a.applied AND COALESCE(sfe_a.verdict, '') <> ''"
    " ORDER BY sfe_a.created_at DESC, sfe_a.id DESC LIMIT 1), '') IN ("
    + ", ".join(f"'{verdict}'" for verdict in SIGNAL_POSITIVE_VERDICTS)
    + ")"
)
# Происхождение сигнала (#95, ADR 0003): находка поиска радара, статья ленты, принятая отбором, или материал коллеги
# из бота (#131, третье происхождение: решает «Отбор», поля автора не переписываются).
SIGNAL_ORIGIN_RADAR = "radar"
SIGNAL_ORIGIN_FEED = "feed"
SIGNAL_ORIGIN_MATERIAL = "material"


def radar_origin_sql(alias: str = "s") -> str:
    """Сигнал: находка радара. Всё, что радар показывает, считает и отдаёт в снимки, берёт
    только их: сигналы ленты радар не меняют (#95)."""
    return f"{alias}.origin = '{SIGNAL_ORIGIN_RADAR}'"


def list_signals_for_dedup(*, window_days: int = 30, fresh_days: int = 3, limit: int = 300) -> list[dict]:
    """Сохранённые карточки радара для сверки дублей на внешнем воркере (базы у него нет).

    Разобранные экспертом заказчика, всегда, любого возраста: по ним видно, что он уже одобрил
    или отклонил, и повтор того же события не должен вернуться новой карточкой.
    Неразобранные: за окно. fresh, появилась за последние дни: только такие
    неразобранные карточки сверяются между собой (см. signal_dedup._eligible).
    hidden: не видна ни в одной витрине (качество, архив): такая не забирает себе новую
    находку того же события (signal_dedup._rank). Бизнес-находка видна в витрине «бизнес»:
    не скрытая (#95). last_seen_day, когда радар нашёл событие последний раз: поиск темы
    получает карточки, найденные за его период, чтобы не приносить их снова (17.10)."""
    reviewed = _SIGNAL_REVIEWED_SQL.format(alias="s")
    return _dedup_snapshot_rows(
        hidden=f"NOT {_any_showcase_quality_sql('s')}",
        where=f"""
              -- Только находки радара (#95): сигналы ленты (тысячи в месяц) вытеснили бы карточки радара из снимка
              -- в 300 штук. Видимые сигналы ленты окна поиска едут отдельным списком (list_feed_signals_for_dedup).
              {radar_origin_sql("s")}
              -- Находка пробного прогона (#138) не видна нигде: повтор её события — новая находка, а не её дубль.
              AND s.selector IS DISTINCT FROM '{SIGNAL_SELECTOR_TRIAL}'
              -- Отсеянное в дедуп не идёт: новое событие не склеится с браком. Разобранное
              -- человеком — идёт: одобренная отсеянная видна на радаре, и её повтор должен
              -- стать её дублем; отклонённая — главной (_rank), повтор не вернётся.
              AND (s.filter_stage IS NULL OR {reviewed})
              AND ({reviewed} OR s.last_seen_at >= now() - make_interval(days => %(window)s))""",
        order=f"{reviewed} DESC, s.last_seen_at DESC, s.id DESC",
        params={"fresh": fresh_days, "window": window_days, "limit": limit},
    )


def list_feed_signals_for_dedup(*, since: date | datetime, fresh_days: int = 3, limit: int = 1500) -> list[dict]:
    """Сигналы ленты для «Дедупа» прогона радара (блок А5 схемы агентов, 04.11): видимые в витрине (технологий или
    бизнеса) и разобранные человеком, поступившие с since. Находка поиска того же события садится в такой сигнал,
    а не заводит вторую карточку (прогон 51351: у 9 из 24 находок событие уже было в ленте под другой ссылкой).
    Сверяется он только с находками прогона (signal_dedup._eligible): ленту с лентой склеивают перепечатки. Скрытый
    неразобранный не нужен: главным он не стал бы (signal_dedup._rank), а склейку «сохранённая → новая» ядро не
    пишет."""
    reviewed = _SIGNAL_REVIEWED_SQL.format(alias="s")
    visible = f"({_RADAR_VISIBLE_SQL} OR {_BUSINESS_VISIBLE_SQL})"
    return _dedup_snapshot_rows(
        hidden=f"NOT {visible}",
        where=f"""
              s.origin = '{SIGNAL_ORIGIN_FEED}'
              AND s.first_seen_at >= %(since)s
              AND ({visible} OR {reviewed})""",
        order=f"{reviewed} DESC, s.first_seen_at DESC, s.id DESC",
        params={"fresh": fresh_days, "since": since, "limit": limit},
    )


def _dedup_snapshot_rows(*, hidden: str, where: str, order: str, params: dict) -> list[dict]:
    """Строки снимка дедупа (несклеенные карточки) по условию where: что знает о карточке воркер NL: заголовки,
    суть, участники, вердикт, разобрана ли, скрыта ли (hidden), свежая ли (params fresh: дней), первые 10 ссылок."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT s.id, s.signal_key, s.origin, s.title, s.title_ru, s.theme,
                   left(s.summary, 400) AS summary,
                   {_dedup_companies_sql()} AS companies, s.score, s.evidence_count,
                   (s.first_seen_at >= now() - make_interval(days => %(fresh)s)) AS fresh,
                   to_char(s.first_seen_at AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD') AS first_seen_day,
                   to_char(s.last_seen_at AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD') AS last_seen_day,
                   v.verdict,
                   {_SIGNAL_REVIEWED_SQL.format(alias="s")} AS reviewed,
                   {hidden} AS hidden,
                   COALESCE(ev.urls, ARRAY[]::text[]) AS evidence_urls
            FROM signals s
            LEFT JOIN LATERAL (
              SELECT sfe.verdict
              FROM signal_feedback_events sfe
              WHERE sfe.signal_id = s.id AND COALESCE(sfe.verdict, '') <> ''
              ORDER BY sfe.created_at DESC, sfe.id DESC
              LIMIT 1
            ) v ON TRUE
            LEFT JOIN LATERAL (
              SELECT array_agg(top.source_url ORDER BY top.strength DESC, top.id) AS urls
              FROM (
                SELECT source_url, strength, id
                FROM signal_evidence
                WHERE signal_id = s.id
                ORDER BY strength DESC, id
                LIMIT 10
              ) top
            ) ev ON TRUE
            WHERE s.merged_into_signal_id IS NULL AND {where}
            ORDER BY {order}
            LIMIT %(limit)s
            """,
            params,
        )
        rows = cur.fetchall()
    return [
        {**row, "score": float(row["score"] or 0), "companies": list(row["companies"] or []),
         "evidence_urls": list(row["evidence_urls"] or [])}
        for row in rows
    ]


def _dedup_companies_sql() -> str:
    """Компании карточки s для правила пар «Дедупа»: у прежней цепочки: компании судьи (companies_json), у сигнала
    после «Отбора» и у сигнала ленты, участники «Карточки» (participants_json): companies_json у них пуст, и общей
    компании дедуп не видел (прогон 51351, 04.11)."""
    return ("(CASE WHEN (CASE WHEN jsonb_typeof(s.companies_json) = 'array'"
            " THEN jsonb_array_length(s.companies_json) ELSE 0 END) > 0 THEN s.companies_json"
            f" ELSE COALESCE((SELECT jsonb_agg(p->>'name') FROM {_PARTICIPANTS_SQL}"
            " WHERE COALESCE(p->>'name', '') <> ''), '[]'::jsonb) END)")


def resolve_signal_merge_root(signal_id: int) -> int | None:
    """Главная карточка группы: идти по merged_into_signal_id до видимой. None: нет такой."""
    with get_connection() as conn:
        current = int(signal_id)
        for _ in range(10):
            row = conn.execute("SELECT merged_into_signal_id FROM signals WHERE id = %s", (current,)).fetchone()
            if row is None:
                return None
            if row[0] is None:
                return current
            current = int(row[0])
    return None


def merge_refusal(signal_id: int, into_signal_id: int) -> str | None:
    """Почему «Дубль» администратора не склеит карточку с главной (#188), словами для ответа; None: склеит. Склейка
    идёт к корню группы главной (mark_signal_merged); выбранную «в дайджест» не склеивает ничто: она исчезла бы из
    витрины и осталась бы в выпуске без способа её оттуда убрать."""
    root = resolve_signal_merge_root(into_signal_id)
    if root is None:
        return f"главной карточки №{int(into_signal_id)} нет"
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT s.merged_into_signal_id, {signal_digest_sql('s')} FROM signals s WHERE s.id = %s",
            (int(signal_id),),
        ).fetchone()
    if row is None:
        return f"карточки №{int(signal_id)} нет"
    merged_into, chosen = row
    if root == int(signal_id):
        return f"№{int(into_signal_id)} уже склеена с этой карточкой: главная в группе она сама"
    if merged_into is not None:
        return f"карточка уже склеена с №{int(merged_into)}"
    if chosen:
        return ("карточка выбрана «в дайджест»: склейка убрала бы её из витрины, а в выпуске она осталась бы; "
                "сначала уберите её из дайджеста")
    return None


def mark_signal_merged(signal_id: int, into_signal_id: int, reason: str = "", *, respect_review: bool = True) -> bool:
    """Скрыть карточку-дубль со ссылкой на главную. Не удаляет: пометку снимает UPDATE.

    Разобранную карточку не прячем, даже если судья счёл её дублем: между выдачей
    задачи и её завершением её могли успеть разобрать или выбрать в дайджест.
    `respect_review=False`: решение человека («Дубль #ID» в обратной связи): сам отзыв
    делает карточку разобранной, и без этого флага она не спряталась бы никогда."""
    root = resolve_signal_merge_root(into_signal_id)
    if root is None or root == int(signal_id):
        return False
    if respect_review:
        review_clause = "AND NOT " + _SIGNAL_REVIEWED_SQL.format(alias="signals")
    else:
        # Решение человека прячет и разобранную карточку, но не выбранную в дайджест: иначе она
        # исчезла бы с экрана и осталась в выпуске без способа её оттуда убрать. У сигнала ленты «в дайджест»
        # отметка его статьи (#130, signal_digest_sql), а не карточки.
        review_clause = "AND NOT " + signal_digest_sql("signals")
    with get_connection() as conn:
        row = conn.execute(
            f"""
            UPDATE signals
            SET merged_into_signal_id = %s, merge_reason = %s, updated_at = now()
            WHERE id = %s
              AND merged_into_signal_id IS NULL
              {review_clause}
            RETURNING id
            """,
            (root, (reason or "")[:500] or None, int(signal_id)),
        ).fetchone()
        if row is not None:
            # Группа всегда в один шаг: всё, что было склеено с этой карточкой, к корню.
            conn.execute(
                "UPDATE signals SET merged_into_signal_id = %s, updated_at = now() WHERE merged_into_signal_id = %s",
                (root, int(signal_id)),
            )
            # У главной стало больше публикаций (#132).
            refresh_platform_decisions([root], conn=conn)
        conn.commit()
        return row is not None


def insert_otbor_finding(signal: dict, *, selector: str = SIGNAL_SELECTOR_OTBOR) -> tuple[int, bool]:
    """Находка поиска, которую решит «Отбор» (#131): сигнал радара с меткой пути и без решения: заголовок, тема,
    «Ход решения» поиска. Уже есть с этим ключом: прежний сигнал как есть (найдено снова). (id, заведён ли сейчас).
    selector: у находки пробного прогона (#138) своя метка пути."""
    with get_connection() as conn:
        row = conn.execute(
            f"""
            INSERT INTO signals (signal_key, origin, selector, title, theme, maturity, decision_log)
            VALUES (%s, '{SIGNAL_ORIGIN_RADAR}', %s, %s, %s, 'watch', %s)
            ON CONFLICT (signal_key) DO NOTHING
            RETURNING id
            """,
            (signal["signal_key"], selector, signal["title"], signal["theme"],
             Json(_jsonable(signal.get("decision_log") or []))),
        ).fetchone()
        created = row is not None
        if row is None:
            row = conn.execute("SELECT id FROM signals WHERE signal_key = %s", (signal["signal_key"],)).fetchone()
        conn.commit()
    return int(row[0]), created


def touch_signal(signal_id: int) -> None:
    """Радар нашёл карточку снова. Только находку радара: сигнал ленты, в который «Дедуп» склеил находку поиска
    (04.11), радар не находил, его время: время сбора статьи, а last_seen_at берёт сортировка витрин."""
    with get_connection() as conn:
        conn.execute(f"UPDATE signals s SET last_seen_at = now(), updated_at = now() WHERE s.id = %s"
                     f" AND {radar_origin_sql('s')}", (int(signal_id),))
        conn.commit()


def signal_evidence_urls(signal_id: int) -> set[str]:
    """Свои ссылки карточки (без ссылок склеенных дублей)."""
    with get_connection() as conn:
        rows = conn.execute("SELECT source_url FROM signal_evidence WHERE signal_id = %s", (int(signal_id),)).fetchall()
    return {str(row[0]) for row in rows}


def append_signal_decision_log(signal_id: int, stage: str, text: str) -> None:
    """Шаг в «Ход решения» карточки: «Дедуп» склеил с ней находку поиска того же события (04.11)."""
    with get_connection() as conn:
        conn.execute("UPDATE signals SET decision_log = COALESCE(decision_log, '[]'::jsonb) || %s::jsonb,"
                     " updated_at = now() WHERE id = %s",
                     (Json([{"stage": stage, "text": text}]), int(signal_id)))
        conn.commit()


def _signal_holds_evidence_sql(alias: str) -> str:
    """Карточка держит свои ссылки: не дубль и видна на радаре: или разобрана человеком.

    Скрытую качеством или архивом неразобранную карточку повторная находка того же материала
    не узнаёт как «её же» и забирает ссылку себе: иначе событие пропало бы с экрана вместе с
    ней (02.10). Ссылки разобранной радар заново не приносит (list_reviewed_signal_urls)."""
    # Сигнал ленты ссылку у радара не держит (#95): иначе находка поиска с адресом статьи
    # ленты стала бы его повтором и пропала с радара. Бизнес-находка радара держит: её
    # видно в витрине «бизнес». Находка пробного прогона (#138) не держит: она не видна нигде, и
    # обычный прогон, нашедший то же событие, заводит свою находку.
    return (f"({radar_origin_sql(alias)} AND {alias}.merged_into_signal_id IS NULL"
            f" AND {alias}.selector IS DISTINCT FROM '{SIGNAL_SELECTOR_TRIAL}'"
            f" AND ({_any_showcase_quality_sql(alias)} OR {_SIGNAL_REVIEWED_SQL.format(alias=alias)}))")


def _feed_holds_finding_link_sql(owner: str, evidence: str) -> str:
    """Ссылку поиска, которую «Дедуп» прибавил к сигналу ленты (04.11: у неё нет статьи), сигнал держит, пока он не
    дубль. Иначе следующий прогон, найдя её снова, завёл бы на ней вторую карточку (судья ответил «разные», сбой
    вызова, пара не влезла в потолок) и забрал бы ссылку у карточки ленты. Свою статью сигнал ленты у радара по-прежнему
    не держит (_signal_holds_evidence_sql): ссылку статьи сбора находка не берёт с #138."""
    return (f"({owner}.origin = '{SIGNAL_ORIGIN_FEED}' AND {owner}.merged_into_signal_id IS NULL"
            f" AND {evidence}.article_id IS NULL)")


def signal_takes_findings(signal_id: int) -> bool:
    """В карточку «Дедуп» склеивает находку поиска (04.11), только если её видно в витрине или её разобрал человек.
    Главная могла скрыться между снимком прогона и записью (возврат в «Отбор» с отказом, архив источника): находка
    пропала бы с экранов вместе с ней."""
    with get_connection() as conn:
        return bool(conn.execute(
            f"SELECT EXISTS (SELECT 1 FROM signals s WHERE s.id = %s AND ({_RADAR_VISIBLE_SQL} OR {_BUSINESS_VISIBLE_SQL}"
            f" OR {_SIGNAL_REVIEWED_SQL.format(alias='s')}))",
            (int(signal_id),),
        ).fetchone()[0])


def visible_evidence_owners(urls: list[str]) -> dict[str, dict]:
    """Каким видимым карточкам уже принадлежат ссылки: {url: {id, signal_key}}. И сигналу ленты: ссылка поиска,
    которую к нему прибавил «Дедуп» (_feed_holds_finding_link_sql): находка её снова: найдена снова."""
    urls = [url for url in urls if url]
    if not urls:
        return {}
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT e.source_url, s.id, s.signal_key
            FROM signal_evidence e
            JOIN signals s ON s.id = e.signal_id
            WHERE e.source_url = ANY(%s)
              AND ({_signal_holds_evidence_sql("s")} OR {_feed_holds_finding_link_sql("s", "e")})
            """,
            (list(urls),),
        )
        return {row["source_url"]: {"id": int(row["id"]), "signal_key": row["signal_key"]} for row in cur.fetchall()}


def rejected_evidence_owners(urls: list[str]) -> dict[str, dict]:
    """Какие ссылки, у несклеенной находки радара, забракованной судьёй или ревью пачки: {url: {id,
    signal_key}}. Такую ссылку себе не берут ни повторный отсев, ни новый брак под другим ключом: событие
    уже разобрано (17.10). Забрать её может только принятая: тогда забракованная станет её дублем."""
    urls = [url for url in urls if url]
    if not urls:
        return {}
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT e.source_url, s.id, s.signal_key
            FROM signal_evidence e
            JOIN signals s ON s.id = e.signal_id
            WHERE e.source_url = ANY(%s)
              AND {radar_origin_sql("s")} AND s.merged_into_signal_id IS NULL
              AND s.filter_stage = ANY(%s)
            """,
            (list(urls), list(RADAR_VERDICT_STAGES)),
        )
        return {row["source_url"]: {"id": int(row["id"]), "signal_key": row["signal_key"]} for row in cur.fetchall()}


def archive_signal_candidates(*, created_before: date | str | None = None, free_theme_only: bool = True,
                              ids: Sequence[int] | None = None) -> list[dict]:
    """Какие видимые на радаре карточки уйдут в архив: для сухого прогона и для записи.

    Ранние карточки 13.09 писали тему свободным текстом (не одна из 13 тематик) и считались
    прежним судьёй: баллы 20-40, почти половина экрана (замечание эксперта заказчика 29.09). Отбор:
    по дате поступления, свободной теме или списку номеров. Разобранные не исключаются, но
    помечены: решение за человеком, архив обратим. Выбранную «в дайджест» архив не берёт:
    её не прячет ничто (_SIGNAL_IN_DIGEST_SQL)."""
    clauses = ["s.archived_at IS NULL", "s.merged_into_signal_id IS NULL"]
    params: list = []
    base_clauses = len(clauses)
    if created_before:
        clauses.append("s.first_seen_at < %s::date")
        params.append(str(created_before))
    if free_theme_only:
        clauses.append(f"NOT {_RADAR_TOPIC_SQL}")
        params.append(SYSTEM_TAG_UNCLASSIFIED)
    if ids:
        clauses.append("s.id = ANY(%s)")
        params.append([int(value) for value in ids])
    if len(clauses) == base_clauses:
        raise ValueError("Нужно условие отбора: дата, свободная тема или номера карточек")
    clauses.append(f"NOT {_SIGNAL_IN_DIGEST_SQL.format(alias='s')}")
    # Архив, инструмент радара (#95): сигналы ленты по дате поступления сюда не попадают.
    clauses.append(radar_origin_sql("s"))
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT s.id, s.theme, COALESCE(s.title_ru, s.title) AS title, s.score, s.first_seen_at,
                   {_SIGNAL_REVIEWED_SQL.format(alias="s")} AS reviewed
            FROM signals s
            WHERE {' AND '.join(clauses)}
            ORDER BY s.id
            """,
            params,
        )
        return cur.fetchall()


def archive_signals(signal_ids: Sequence[int], *, reason: str) -> int:
    """Убрать карточки с радара в архив. Не удаляет: вернуть: unarchive_signals.
    Выбранную «в дайджест» не трогает, даже если её номер назван явно."""
    ids = [int(value) for value in signal_ids]
    if not ids:
        return 0
    with get_connection() as conn:
        cur = conn.execute(
            f"""
            UPDATE signals
            SET archived_at = now(), archive_reason = %s, updated_at = now()
            WHERE id = ANY(%s) AND archived_at IS NULL
              AND NOT {_SIGNAL_IN_DIGEST_SQL.format(alias="signals")}
            """,
            (reason, ids),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def unarchive_signals(*, signal_ids: Sequence[int] | None = None, reason: str | None = None) -> int:
    """Вернуть карточки из архива: по номерам или по причине архивации (одной командой)."""
    if not signal_ids and not reason:
        raise ValueError("Нужны номера карточек или причина архивации")
    clauses = ["archived_at IS NOT NULL"]
    params: list = []
    if signal_ids:
        clauses.append("id = ANY(%s)")
        params.append([int(value) for value in signal_ids])
    if reason:
        clauses.append("archive_reason = %s")
        params.append(reason)
    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE signals SET archived_at = NULL, archive_reason = NULL, updated_at = now() WHERE {' AND '.join(clauses)}",
            params,
        )
        conn.commit()
        return int(cur.rowcount or 0)


def _radar_hidden_group_sql() -> str:
    """Одна причина на скрытую карточку, для сводки: первая по порядку правил качества.
    Одобренную решения машины не прячут (_radar_quality_sql): её причина: архив или срок."""
    age_days = int(config.SIGNAL_RADAR_MAX_EVENT_AGE_DAYS)
    stages = " ".join(f"WHEN '{stage}' THEN 'отсеяно ({label})'" for stage, label in RADAR_FILTER_STAGES.items())
    age = (f"WHEN {_judged_sql('s', 'event_date')} < CURRENT_DATE - {age_days} THEN 'событие старше {age_days} дней'"
           if age_days > 0 else "")
    approved_reason = f"CASE {age} ELSE 'другое' END" if age else "'другое'"
    return (
        "CASE WHEN s.archived_at IS NOT NULL THEN 'в архиве'"
        f" WHEN {_SIGNAL_APPROVED_SQL.format(alias='s')} THEN {approved_reason}"
        f" WHEN s.filter_stage IS NOT NULL THEN (CASE s.filter_stage {stages} ELSE 'отсеяно' END)"
        " WHEN s.signal_category = 'business' THEN 'бизнес-сигнал'"
        " WHEN s.signal_category = 'other' THEN 'не технологическое событие'"
        f" WHEN {_judged_sql('s', 'oilfield_relevance')} = 'none' THEN 'не про нефтесервис'"
        " WHEN s.mixed_events THEN 'ссылки о разных событиях'"
        f" {age} ELSE 'другое' END"
    )


def radar_weekly_summary(*, days: int = 7) -> dict:
    """«Итоги недели» радара для заказчика (решение 03.10): что вышло на радар, что отсеяно и
    почему, что ждёт разбора, сколько отзывов. Те же правила видимости, что у экрана."""
    since_sql = "now() - make_interval(days => %(days)s)"
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT COUNT(*) FILTER (WHERE {_RADAR_VISIBLE_SQL}) AS on_radar,
                   COUNT(*) FILTER (WHERE {_RADAR_HIDDEN_SQL}) AS filtered
            FROM signals s
            WHERE s.first_seen_at >= {since_sql}
            """,
            {"days": days},
        )
        totals = dict(cur.fetchone())
        cur.execute(
            f"""
            SELECT {_radar_hidden_group_sql()} AS reason, COUNT(*) AS count
            FROM signals s
            WHERE s.first_seen_at >= {since_sql} AND {_RADAR_HIDDEN_SQL}
            GROUP BY 1 ORDER BY 2 DESC, 1
            """,
            {"days": days},
        )
        reasons = [dict(row) for row in cur.fetchall()]
        cur.execute(
            f"""
            SELECT s.id, COALESCE(s.title_ru, s.title) AS title, s.theme, s.score, s.event_date
            FROM signals s
            WHERE s.first_seen_at >= {since_sql} AND {_RADAR_VISIBLE_SQL}
            ORDER BY s.score DESC, s.id DESC
            LIMIT 5
            """,
            {"days": days},
        )
        top = [dict(row) for row in cur.fetchall()]
        cur.execute(
            f"""
            SELECT COUNT(*) AS awaiting
            FROM signals s
            WHERE {_RADAR_VISIBLE_SQL}
              AND NOT {_SIGNAL_REVIEWED_SQL.format(alias="s")}
            """,
            {"days": days},
        )
        awaiting = int(cur.fetchone()["awaiting"])
        cur.execute(
            f"""
            SELECT verdict, COUNT(*) AS count
            FROM signal_feedback_events sfe
            WHERE created_at >= {since_sql} AND COALESCE(verdict, '') <> ''
              -- Отзывы радара (#95): отзыв о сигнале ленты в итоги радара не входит.
              AND NOT EXISTS (SELECT 1 FROM signals fs WHERE fs.id = sfe.signal_id AND NOT {radar_origin_sql('fs')})
            GROUP BY verdict ORDER BY 2 DESC, 1
            """,
            {"days": days},
        )
        verdicts = [dict(row) for row in cur.fetchall()]
    return {
        "days": days,
        "on_radar": int(totals["on_radar"] or 0),
        "filtered": int(totals["filtered"] or 0),
        "filtered_reasons": [{"reason": row["reason"], "count": int(row["count"])} for row in reasons],
        "top": top,
        "awaiting_review": awaiting,
        "feedback": sum(int(row["count"]) for row in verdicts),
        "feedback_verdicts": [{"verdict": row["verdict"], "count": int(row["count"])} for row in verdicts],
    }


def radar_cards_for_recall() -> list[dict]:
    """Все карточки радара (кроме скрытых дублей и неодобренных отсеянных) со ссылками и
    признаком «видна на экране»: для замера полноты по эталону заказчика (signal_reference.recall).

    Отсеянное не «найдено»: до 02.10 брак судьи и поиска не сохранялся вовсе, и без этого
    условия полнота выросла бы без единой новой карточки на экране: замер по октябрьскому
    списку не сравнился бы с прежними. Одобренная отсеянная на радаре: найдена."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT s.id, s.title, s.title_ru, s.summary, s.companies_json AS companies,
                   ({_RADAR_VISIBLE_SQL}) AS visible,
                   ARRAY(SELECT e.source_url FROM signal_evidence e
                         WHERE e.signal_id = s.id
                            OR e.signal_id IN (SELECT m.id FROM signals m WHERE m.merged_into_signal_id = s.id)
                        ) AS urls
            FROM signals s
            WHERE s.merged_into_signal_id IS NULL
              AND {radar_origin_sql("s")}
              -- Пробный прогон широкого поиска (#138) мерит свою полноту сам (signal_search_trial.report).
              AND s.selector IS DISTINCT FROM '{SIGNAL_SELECTOR_TRIAL}'
              AND (s.filter_stage IS NULL OR {_SIGNAL_APPROVED_SQL.format(alias="s")}
                   OR {_SIGNAL_IN_DIGEST_SQL.format(alias="s")})
            """
        )
        return cur.fetchall()


def refresh_all_signal_evidence_counts() -> int:
    """Пересчитать evidence_count у всех карточек; вернуть, у скольких он был неверен.

    Ссылки раньше молча переезжали между карточками, а счётчик пересчитывался только у
    получателя: у прежней оставалось старое число при пустом списке ссылок."""
    with get_connection() as conn:
        cur = conn.execute(
            f"""
            WITH actual AS (
              SELECT s2.id, {signal_publication_count_sql("s2")} AS n
              FROM signals s2
            )
            UPDATE signals s
            SET evidence_count = actual.n, updated_at = now()
            FROM actual
            WHERE actual.id = s.id AND s.evidence_count <> actual.n
            """,
        )
        changed = cur.rowcount
        conn.commit()
        return int(changed or 0)


def signal_key_owners(keys: list[str]) -> dict[str, dict]:
    """Каким сохранённым карточкам уже принадлежат ключи кандидатов: id, склеена ли, разобрана ли."""
    if not keys:
        return {}
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT s.signal_key, s.id, s.merged_into_signal_id, s.filter_stage, {reviewed} AS reviewed
            FROM signals s
            WHERE s.signal_key = ANY(%s)
            """.format(reviewed=_SIGNAL_REVIEWED_SQL.format(alias="s")),
            (list(keys),),
        )
        return {row["signal_key"]: row for row in cur.fetchall()}


def create_signal_generation_run(
    *,
    config_payload: dict,
    trigger: str | None = None,
    background_job_id: int | None = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO signal_generation_runs (background_job_id, trigger, status, config_json)
            VALUES (%s, %s, 'running', %s)
            RETURNING id
            """,
            (background_job_id, trigger, Json(_jsonable(config_payload))),
        )
        run_id = int(cur.fetchone()[0])
        conn.commit()
        return run_id


def finish_signal_generation_run(
    run_id: int,
    *,
    status: str,
    result: dict | None = None,
    error_message: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE signal_generation_runs
            SET status = %s,
                result_json = %s,
                error_message = %s,
                finished_at = now()
            WHERE id = %s
            """,
            (status, Json(_jsonable(result or {})), error_message, run_id),
        )
        conn.commit()


def latest_signal_generation_run(*, payload_subset: dict) -> dict | None:
    """Последний завершённый прогон радара, чья задача содержит payload_subset (ежедневный:
    {"schedule": "daily_signal_discovery"}). Идущий не берём: итога у него ещё нет.

    Время прогона, старт задачи: у пути через воркер строка прогона появляется только при
    записи итога, её started_at: это конец прогона, а не начало."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT sgr.id, sgr.status, sgr.result_json, sgr.error_message, sgr.finished_at,
                   bj.id AS background_job_id,
                   COALESCE(bj.started_at, bj.created_at) AS run_at
            FROM signal_generation_runs sgr
            JOIN background_jobs bj ON bj.id = sgr.background_job_id
            WHERE bj.kind = 'signal_discovery'
              AND bj.payload_json @> %s::jsonb
              AND sgr.status <> 'running'
            ORDER BY bj.created_at DESC, sgr.id DESC
            LIMIT 1
            """,
            (Json(_jsonable(payload_subset)),),
        )
        return cur.fetchone()


def attach_feedback_to_signal_training_examples(
    feedback_event_id: int,
    *,
    signal_id: int | None = None,
    limit: int = 20,
) -> int:
    if signal_id is None:
        return 0
    with get_connection() as conn:
        cur = conn.execute(
            """
            WITH target AS (
              SELECT id
              FROM signal_training_examples
              WHERE signal_id = %s
                AND feedback_event_id IS NULL
              ORDER BY created_at DESC, id DESC
              LIMIT %s
            )
            UPDATE signal_training_examples ste
            SET feedback_event_id = %s,
                updated_at = now()
            FROM target
            WHERE ste.id = target.id
            """,
            (signal_id, limit, feedback_event_id),
        )
        conn.commit()
        return cur.rowcount or 0


def list_signal_training_examples(
    *,
    limit: int = 1000,
    with_feedback_only: bool = True,
    verdict: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if with_feedback_only:
        clauses.append("ste.feedback_event_id IS NOT NULL")
    if verdict:
        clauses.append("sfe.verdict = %s")
        params.append(verdict)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT
              ste.id,
              ste.generation_run_id,
              ste.signal_id,
              ste.feedback_event_id,
              ste.topic,
              ste.signal_key,
              ste.pipeline_verdict,
              ste.input_json,
              ste.raw_output_json,
              ste.normalized_output_json,
              ste.created_at,
              ste.updated_at,
              sfe.event_type AS feedback_event_type,
              sfe.verdict AS feedback_verdict,
              sfe.reason AS feedback_reason,
              sfe.corrected_title AS feedback_corrected_title,
              sfe.corrected_thesis AS feedback_corrected_thesis,
              sfe.duplicate_of_signal_id AS feedback_duplicate_of_signal_id,
              sfe.comment AS feedback_comment,
              sfe.created_at AS feedback_created_at,
              s.title AS signal_title,
              s.title_ru AS signal_title_ru,
              s.theme AS signal_theme,
              s.score AS signal_score,
              s.maturity AS signal_maturity
            FROM signal_training_examples ste
            LEFT JOIN signal_feedback_events sfe ON sfe.id = ste.feedback_event_id
            LEFT JOIN signals s ON s.id = ste.signal_id
            {where}
            ORDER BY ste.created_at DESC, ste.id DESC
            LIMIT %s
            """,
            params,
        )
        return list(cur.fetchall())


# Исправления из обратной связи («Рекомендуемый заголовок», «…формулировка сути»):
# последнее непустое по карточке. Накладываются при чтении: текст модели в signals не
# затирается, и повторная находка той же карточки прогоном радара правку не перепишет. Только применённые (#188):
# правка администратора и правка материала выпуска; правка участника без прав администратора лежит в его блоке ОС.
# Правка, отменённая из журнала ОС (#189), не ложится: снова текст модели или прежняя правка ({undone}).
_SIGNAL_CORRECTIONS_LATERAL = """
LEFT JOIN LATERAL (
  SELECT
    (SELECT e.corrected_title FROM signal_feedback_events e
     WHERE e.signal_id = {alias}.id AND e.applied AND btrim(COALESCE(e.corrected_title, '')) <> ''{until}{undone}
     ORDER BY e.id DESC LIMIT 1) AS corrected_title,
    (SELECT e.corrected_thesis FROM signal_feedback_events e
     WHERE e.signal_id = {alias}.id AND e.applied AND btrim(COALESCE(e.corrected_thesis, '')) <> ''{until}{undone}
     ORDER BY e.id DESC LIMIT 1) AS corrected_thesis
) corr ON TRUE
"""


def _signal_corrections_lateral(alias: str, issue_window: FeedWindow | None = None, *,
                                month_sql: str | None = None) -> str:
    """LEFT JOIN LATERAL corr: последние правки заголовка и сути у карточки `alias`.

    Экран радара: все правки. Выпуск (`issue_window`: окно ленты на сейчас) у карточки
    закрытого месяца берёт только правки, поданные до закрытия (решение владельца 30.09:
    «архив только на просмотр»: и текст закрытого выпуска). Отзыв после закрытия принимается
    как раньше и виден на радаре: он нужен агенту для обучения, но выпуск не меняет.
    `month_sql`: месяц выпуска материала, если он не месяц поступления карточки (у статьи ленты её месяц).
    Отмена правки из журнала ОС (#189) так же: закрытый выпуск не видит отмену после закрытия."""
    until, undone = "", " AND e.correction_undone_at IS NULL"
    if issue_window is not None:
        month = month_sql or signal_month_sql(alias)
        until = " AND " + issue_window.before_close_sql(month, "e.created_at")
        undone = (" AND (e.correction_undone_at IS NULL OR NOT "
                  + issue_window.before_close_sql(month, "e.correction_undone_at") + ")")
    return _SIGNAL_CORRECTIONS_LATERAL.format(alias=alias, until=until, undone=undone)


# Балла у сигнала нет (экран пишет «без баллов», а не 0): то же правило, что signal_scoring.unscored у витрин
# (/api/signals), менять вместе.
_FEED_SIGNAL_UNSCORED_SQL = ("(fs.score_band IS NULL AND (fs.otbor_decision IS NOT NULL"
                             " OR fs.otbor_failure IS NOT NULL OR fs.filter_stage = 'search'))")


def feed_signal_order_sql(alias: str) -> str:
    """Порядок, по которому у статьи берётся её сигнал ленты (строка `alias` таблицы signals с origin = 'feed'): не
    дубль раньше дубля, затем ранний. Сигнал у статьи один (ключ feed:<статья>), порядок держит выбор однозначным.
    Ставится в ORDER BY перед LIMIT 1 или вслед за ключом DISTINCT ON. Одно правило у темы и балла читателей
    (publication_joins), разбивки балла /api/articles и полей материала PDF (issue_material_fields): у письма, PDF и
    экрана «Дайджест» один балл."""
    return f"{alias}.merged_into_signal_id NULLS FIRST, {alias}.id"


def publication_joins(article: str = "a") -> str:
    """Тема и балл публикации ленты для её читателей: выпуска, письма и DOCX, /api/articles, «Статистики», аналитики.

    Тег и балл статьи снимаются вместе со старой цепочкой (решение владельца 08.11), поэтому тему и балл читатели берут
    у сигнала ленты статьи: тему ставит «Карточка», балл её второй вызов, у решённой гейтом до «Отбора» мост скопировал
    туда тег и балл статьи. Статья без сигнала ленты (собрана до 01.08.2026 или сигнал не заведён; на проде 08.11 в
    окне с 11.10 таких 23 из 2 709) живёт по прежним тегу и баллу статьи. Сигнал ищется так же, как поля материала PDF
    (issue_material_fields): у письма и PDF один балл.

    Приносит в запрос статьи `article` алиасы: sc, at, t, parent (прежние балл и тег статьи, история), fs (её сигнал
    ленты) и pub (что читать): tag_name и parent_tag_name (тема для показа: у сигнала его тема, у статьи без сигнала
    «родитель / тег»), theme (корневая тема), total_score (NULL: балла нет), score_band (ступень второго вызова),
    scored_at (когда балл готов). LIMIT держит поиск сигнала по индексу на каждую статью: без него планировщик вправе
    соединить выборку со всей таблицей сигналов. Лишние соединения тегов планировщик выбрасывает сам (ключи
    уникальны)."""
    return f"""
    LEFT JOIN article_scores sc ON sc.article_id = {article}.id
    LEFT JOIN article_tags at ON at.article_id = {article}.id
    LEFT JOIN tags t ON t.id = at.tag_id
    LEFT JOIN tags parent ON parent.id = t.parent_id
    LEFT JOIN LATERAL (
      SELECT x.id, x.theme, x.score, x.score_band, x.otbor_decision, x.otbor_failure, x.filter_stage,
             x.scores_recipe->>'filled_at' AS filled_at
      FROM signals x
      WHERE x.origin_article_id = {article}.id AND x.origin = '{SIGNAL_ORIGIN_FEED}'
      ORDER BY {feed_signal_order_sql('x')}
      LIMIT 1
    ) fs ON TRUE
    LEFT JOIN LATERAL (
      SELECT CASE WHEN fs.id IS NULL THEN t.name ELSE fs.theme END AS tag_name,
             CASE WHEN fs.id IS NULL THEN parent.name END AS parent_tag_name,
             COALESCE(fs.theme, parent.name, t.name) AS theme,
             CASE WHEN fs.id IS NULL THEN sc.total_score WHEN {_FEED_SIGNAL_UNSCORED_SQL} THEN NULL
                  ELSE fs.score END AS total_score,
             fs.score_band,
             -- Балл второго вызова готов, когда записан его рецепт; балл, который мост скопировал со статьи, и балл
             -- статьи без сигнала готовы, когда оценили статью.
             CASE WHEN fs.id IS NULL THEN sc.created_at WHEN {_FEED_SIGNAL_UNSCORED_SQL} THEN NULL
                  ELSE COALESCE(fs.filled_at::timestamptz, sc.created_at) END AS scored_at
    ) pub ON TRUE
    """


def _article_corrections_lateral(issue_window: FeedWindow) -> str:
    """LEFT JOIN LATERAL corr: правки заголовка и сути статьи `a` в выпуске, у её сигнала ленты fs (#162, ADR 0006; fs
    приносит publication_joins): правка с экрана «Дайджест» ложится на сигнал, и выпуск показывает тот же текст, что
    бизнес-лента. Закрытый выпуск берёт правки до закрытия месяца статьи."""
    return _signal_corrections_lateral("fs", issue_window, month_sql=period_month_sql("a"))


def apply_signal_corrections(row: dict) -> dict:
    """Заголовок и суть карточки: с правками людей поверх текста модели."""
    title = (row.get("corrected_title") or "").strip()
    thesis = (row.get("corrected_thesis") or "").strip()
    if title:
        row["original_title_ru"] = row.get("title_ru")
        row["title_ru"] = title
    if thesis:
        row["original_summary"] = row.get("summary")
        row["summary"] = thesis
    return row


def _radar_card_evidence_exists(condition: str = "TRUE") -> str:
    """Есть ли у карточки s ссылка с условием: своя или её склеенного дубля. Два EXISTS, а не
    один с «e.signal_id = s.id OR e.signal_id IN (подзапрос)»: такой OR база исполняет
    подзапросом на каждую ссылку (ревью PR #83; замер на синтетике 600 карточек / 1 651
    ссылка: список 236 → 21 мс, счётчики 517 → 19 мс, выборки те же)."""
    return (
        f"(EXISTS (SELECT 1 FROM signal_evidence e WHERE e.signal_id = s.id AND {condition})"
        " OR EXISTS (SELECT 1 FROM signals m JOIN signal_evidence e ON e.signal_id = m.id"
        f" WHERE m.merged_into_signal_id = s.id AND {condition}))"
    )


def _judged_sql(alias: str, column: str) -> str:
    """Значение поля карточки, по которому витрина решает, показывать ли карточку: судьи радара или
    человека. Значение агента «Карточка сигнала» (#96) ничего не прячет: он заполняет пустые поля
    старых находок, и иначе прогон по истории убрал бы с радара карточки, которые там были."""
    return f"(CASE WHEN {alias}.card_set_by->>'{column}' = '{CARD_SET_BY_AGENT}' THEN NULL ELSE {alias}.{column} END)"


def _quality_rules_sql(alias: str, *machine_rules: str) -> str:
    """Правила качества витрины поверх правил машины `machine_rules` (замечания эксперта заказчика 29.09):

    - архив (ранние карточки со свободной темой): обратимо, `unarchive-signals`;
    - ссылки о разных событиях: одна карточка смешала бы две истории (сигнал 71);
    - нет связи с нефтесервисом: «релевантности мало» (эксперт заказчика, 29.09);
    - отсеяно поиском, судьёй, ревью пачки или сбоем судьи: хранится для разбора заказчиком;
    - событие старше срока: «нашёл очень старый сигнал».

    Решения машины (категорию, смешанность, связь с нефтесервисом ставит судья, отсев: поиск,
    судья и ревью) не прячут карточку, которую человек одобрил (последний вердикт
    положительный). Отказ, «не тот блок», «шум» её не возвращают: разметка учит агента,
    карточка остаётся в «Отсеянных и скрытых». Выбранную «в дайджест» не прячет ни одно правило,
    включая архив: иначе её не снять с выпуска (mark_signal_merged)."""
    a = alias
    machine = " AND ".join([
        *machine_rules,
        f"{a}.mixed_events IS NOT TRUE",
        # «Релевантности мало» (эксперт заказчика, 29.09): общепромышленное без связи с нефтесервисом.
        f"COALESCE({_judged_sql(a, 'oilfield_relevance')}, 'direct') <> 'none'",
        # Отсеяно поиском, судьёй, ревью пачки или сбоем судьи: хранится для разбора заказчиком.
        f"{a}.filter_stage IS NULL",
    ])
    parts = [
        f"{a}.archived_at IS NULL",
        f"({machine} OR {_SIGNAL_APPROVED_SQL.format(alias=a)})",
    ]
    age_days = int(config.SIGNAL_RADAR_MAX_EVENT_AGE_DAYS)
    if age_days > 0:
        event_date = _judged_sql(a, "event_date")
        parts.append(f"({event_date} IS NULL OR {event_date} >= CURRENT_DATE - {age_days})")
    return f"({_SIGNAL_IN_DIGEST_SQL.format(alias=a)} OR ({' AND '.join(parts)}))"


# Видимая карточка радара: одна для списка и для чисел над ним (как visible_sql у ленты).
# Дубли скрыты: их ссылки уже в главной карточке (signal_dedup). Карточка без единой ссылки
# (своей или склеенного дубля) не показывается: оценить её нельзя (сигнал 97, 22.09).
def _radar_quality_sql(alias: str = "s") -> str:
    """Что технологический радар не показывает по качеству (_quality_rules_sql) плюс бизнес и
    «другое»: не техрадар (с 14.10 бизнес: своя витрина, _business_quality_sql). Карточка
    хранится: правило: показа, срок и категории меняются без пересчёта. NULL: карточка до
    правки. Категорию ставит судья: одобренная человеком бизнес-карточка на радаре видна."""
    return _quality_rules_sql(alias, f"COALESCE({alias}.signal_category, 'technology') = 'technology'")


def _business_quality_sql(alias: str = "s") -> str:
    """Что витрина «бизнес» показывает из находок радара (#95, ADR 0003): категория «бизнес» и
    те же правила качества, что у радара. Категорию одобрение не перекрывает: одобренная
    технологическая карточка в бизнес не попадает."""
    return f"({alias}.signal_category = 'business' AND {_quality_rules_sql(alias)})"


def _any_showcase_quality_sql(alias: str = "s") -> str:
    """Находка радара проходит качество своей витрины: технологий или бизнеса. Для дедупа и для
    того, кто держит ссылку: бизнес-находку видно в витрине «бизнес», она не скрытая, и новая
    находка того же события садится в неё, а не плодит пару (№186/№343, 14.10)."""
    return f"({_radar_quality_sql(alias)} OR {_business_quality_sql(alias)})"


# Какую карточку вообще показывать: со ссылкой (своей или склеенного дубля), или отсеянную:
# находка поиска без ссылки (битый адрес) тоже видна в разборе. Одно условие для радара и для
# «Отсеянных и скрытых»: одобренная или выбранная «в дайджест» отсеянная без ссылки переходит
# на радар, а не пропадает из обоих списков.
_RADAR_CARD_SHOWN_SQL = f"({_radar_card_evidence_exists()} OR s.filter_stage IS NOT NULL)"
# Сигнал ленты виден, пока видна его статья в ленте (feed_window.visible_sql): отбор передумал,
# статью пометили на удаление или её источник в архиве: сигнал уходит вместе с ней. Правила
# качества радара (смешанные ссылки, отсев, «не про нефтесервис», срок события): о находках
# поиска: статью, которую принял отбор, они не прячут. Ссылку сигнал ленты мог отдать карточке
# радара (feed_signals), поэтому показ: по статье, а не по ссылке.
_FEED_ARTICLE_SHOWN_SQL = (
    "EXISTS (SELECT 1 FROM articles fa JOIN sources fs ON fs.id = fa.source_id"
    " LEFT JOIN article_cards fc ON fc.article_id = fa.id"
    f" WHERE fa.id = s.origin_article_id AND {visible_sql('fa', 'fc', 'fs')})"
)


def signal_digest_sql(alias: str = "s") -> str:
    """Сигнал выбран «в дайджест» (#111). Одна механика на две витрины (решение владельца 23.10): у сигнала
    ленты: отметка его статьи, та же, что ставит лента статей (одна отметка на публикацию, выпуск берёт статью
    один раз); у находки радара: отметка карточки."""
    return (
        f"(CASE WHEN {alias}.origin = '{SIGNAL_ORIGIN_FEED}'"
        f" THEN {marks.article_marked_sql(f'{alias}.origin_article_id', 'digest')}"
        f" ELSE {_SIGNAL_IN_DIGEST_SQL.format(alias=alias)} END)"
    )


def _otbor_shown_sql(category: str) -> str:
    """Сигнал с решением «Отбора» (#130, ADR 0005), в витрине своей категории, для любого происхождения: по
    смыслу его прячет только «Отбор». Правила качества радара (связь, смешанные ссылки, срок события) его не
    прячут; технические остаются: архив, дубли и перепечатки, архивный источник (у сигнала ленты: по его
    статье), карточка без ссылок. Выбранный «в дайджест» не прячет ничто, кроме технических правил статьи.
    Материал коллеги (#131) виден и без ссылки: у файла и события своими словами её нет, а адрес, который уже
    держит другая карточка, к материалу не переходит (он свой сигнал, а не прикрепление)."""
    return (
        f"s.merged_into_signal_id IS NULL AND s.otbor_decision IS NOT NULL AND s.signal_category = '{category}'"
        f" AND ({signal_digest_sql('s')} OR (s.archived_at IS NULL AND s.otbor_decision = '{feed_window.OTBOR_ACCEPT}'))"
        f" AND CASE WHEN s.origin = '{SIGNAL_ORIGIN_FEED}' THEN {_FEED_ARTICLE_SHOWN_SQL}"
        f" WHEN s.origin = '{SIGNAL_ORIGIN_MATERIAL}' THEN TRUE"
        f" ELSE {_RADAR_CARD_SHOWN_SQL} END"
    )


# Сигнал без решения «Отбора» живёт по прежним правилам, если «Отбор» его не решает: находку и материал на пути
# «Отбора» (#131, с 08.11 все новые), пока решения нет, не показывает ни витрина, ни «До „Отбора“», только раздел сбоя.
_BEFORE_OTBOR_SQL = "s.otbor_decision IS NULL AND s.selector IS NULL"
# Видимая карточка радара: одна для списка и для чисел над ним (как visible_sql у ленты).
# Дубли скрыты: их ссылки уже в главной карточке (signal_dedup). Карточка без единой ссылки
# (своей или склеенного дубля) не показывается: оценить её нельзя (сигнал 97, 22.09). Без
# решения «Отбора» (#130) радар, только находки поиска (#95) по прежним правилам; с решением:
# технологии любого происхождения (_otbor_shown_sql).
_RADAR_VISIBLE_SQL = (
    f"(({_BEFORE_OTBOR_SQL} AND {radar_origin_sql()} AND s.merged_into_signal_id IS NULL"
    f" AND {_radar_quality_sql()} AND {_RADAR_CARD_SHOWN_SQL}) OR ({_otbor_shown_sql('technology')}))"
)
# «Отсеянные и скрытые» до «Отбора» (раздел «До „Отбора“», rejected_sections; только админу: решение
# 05.10): проверить, не отсеяно ли лишнее. Та же карточка, что видна бы была, кроме правил качества;
# дубли сюда не идут: их ссылки и так в главной карточке.
_RADAR_HIDDEN_SQL = (
    f"{_BEFORE_OTBOR_SQL} AND {radar_origin_sql()} AND s.merged_into_signal_id IS NULL"
    f" AND NOT ({_radar_quality_sql()}) AND {_RADAR_CARD_SHOWN_SQL}"
)
# Витрина «бизнес», сигналы категории «бизнес» обоих происхождений: без решения «Отбора»: по прежним
# правилам, с решением: по его решению (_otbor_shown_sql).
_BUSINESS_VISIBLE_SQL = (
    f"(({_BEFORE_OTBOR_SQL} AND s.merged_into_signal_id IS NULL AND s.signal_category = 'business'"
    f" AND CASE WHEN s.origin = '{SIGNAL_ORIGIN_FEED}'"
    f" THEN s.archived_at IS NULL AND {_FEED_ARTICLE_SHOWN_SQL}"
    f" ELSE {_business_quality_sql()} AND {_RADAR_CARD_SHOWN_SQL} END) OR ({_otbor_shown_sql('business')}))"
)
SIGNAL_SHOWCASES = ("technology", "business")
RADAR_FILTER_STAGES = {
    "search": "поиск",
    "judge": "судья",
    "review": "ревью пачки",
    "judge_error": "судья не ответил",
}
# Этапы отсева идут только вперёд (17.10): до вердикта: поиск и сбой судьи, вердикт: брак судьи или
# ревью пачки. Пустая до вердикта становится дублем забраковавшей (_retire_emptied_filtered_card), а
# ссылку забракованной не берёт себе ни повторный отсев, ни новый брак под другим ключом
# (rejected_evidence_owners): забрать её может только принятая.
RADAR_STAGES_BEFORE_VERDICT = ("search", "judge_error")
RADAR_VERDICT_STAGES = ("judge", "review")


def _radar_hidden_reason_sql() -> str:
    """Почему карточка не на радаре: по-русски, теми же правилами, что _radar_quality_sql.
    У видимой причины нет: экран подписывает «Не на радаре: …» любую карточку с причиной. У сигнала
    с решением «Отбора» (#130) прежних правил нет: почему его нет в витрине, говорит его раздел."""
    age_days = int(config.SIGNAL_RADAR_MAX_EVENT_AGE_DAYS)
    age = (
        f"CASE WHEN {_judged_sql('s', 'event_date')} < CURRENT_DATE - {age_days}"
        f" THEN 'событие старше {age_days} дней' END"
        if age_days > 0 else "NULL"
    )
    # Решения машины (судья, отсев) одобренную карточку не прячут (_radar_quality_sql): и в
    # причине их нет.
    machine = (
        f"CASE WHEN NOT {_SIGNAL_APPROVED_SQL.format(alias='s')} THEN NULLIF(concat_ws('; ',"
        " CASE s.filter_stage"
        + "".join(f" WHEN '{stage}' THEN 'отсеяно ({label})'" for stage, label in RADAR_FILTER_STAGES.items())
        + " END || COALESCE(': ' || NULLIF(s.filter_reason, ''), ''),"
        " CASE s.signal_category WHEN 'business' THEN 'бизнес-сигнал, не технология'"
        "   WHEN 'other' THEN 'не технологическое событие' END,"
        f" CASE WHEN {_judged_sql('s', 'oilfield_relevance')} = 'none' THEN 'не про нефтесервис' END,"
        " CASE WHEN s.mixed_events THEN 'ссылки о разных событиях'"
        "   || COALESCE(': ' || NULLIF(s.mixed_events_reason, ''), '') END"
        "), '') END"
    )
    return (
        f"CASE WHEN s.otbor_decision IS NOT NULL OR {_radar_quality_sql('s')} THEN NULL ELSE NULLIF(concat_ws('; ',"
        " CASE WHEN s.archived_at IS NOT NULL THEN 'в архиве' END,"
        f" {machine},"
        f" {age}), '') END"
    )
# Тема: одна из тематик заказчика (корневые теги). Первая партия радара (13.09) писала
# тему свободным текстом: экран собирает такие в отдельный блок и в фильтр тем не берёт.
_RADAR_TOPIC_SQL = (
    "(s.theme IN (SELECT t.name FROM tags t WHERE t.parent_id IS NULL AND t.enabled AND t.name <> %s))"
)
# Публикация бывает общей у сигнала ленты и карточки радара (#95): отзыв о сигнале другого происхождения, найденный по
# адресу ссылки, этой карточке не засчитывается.
_FEEDBACK_SAME_ORIGIN_SQL = "NOT EXISTS (SELECT 1 FROM signals fs WHERE fs.id = sfe.signal_id AND fs.origin <> s.origin)"
# Отзыв о карточке: по её номеру или по адресу её ссылки. То же правило двумя EXISTS в SIGNAL_HAS_FEEDBACK_SQL:
# менять вместе.
_FEEDBACK_OF_SIGNAL_SQL = f"""(
  sfe.signal_id = s.id
     OR (sfe.source_url IS NOT NULL AND EXISTS (
          SELECT 1 FROM signal_evidence se
          WHERE se.signal_id = s.id AND se.source_url = sfe.source_url
        )
        AND {_FEEDBACK_SAME_ORIGIN_SQL}))"""
# Запись блока ОС (#130): оценка, причина или комментарий человека из блока «Обратная связь» карточки. «В дайджест»
# и смена статуса пишутся тем же журналом, но не в этом блоке (решение владельца 03.11): по записям блока считают
# фильтр «Наличие ОС», плашка «С обратной связью» и счётчик «Обратная связь: N». Отметка участника без прав
# администратора (#188) запись блока и с одной правкой заголовка или сути либо «Дублем»: на карточку они не легли,
# видно их только в блоке. Применённая правка без оценки, причины и комментария не блок: она уже на карточке.
_NOT_APPLIED_PROPOSAL_SQL = (
    "(NOT sfe.applied AND (btrim(COALESCE(sfe.corrected_title, '')) <> ''"
    " OR btrim(COALESCE(sfe.corrected_thesis, '')) <> '' OR sfe.duplicate_of_signal_id IS NOT NULL))"
)
_FEEDBACK_IS_BLOCK_SQL = (
    "(sfe.event_type = 'comment_added'"
    " AND (COALESCE(sfe.verdict, '') <> '' OR COALESCE(sfe.reason, '') <> '' OR COALESCE(sfe.comment, '') <> ''"
    f" OR {_NOT_APPLIED_PROPOSAL_SQL}))"
)
# Блоки ОС сигнала (#130, решение владельца 22.10): каждый человек добавляет свой блок (оценка, причина,
# комментарий), чужие не перезаписываются; показываются списком, на отбор и показ не влияют. Автор: почта
# учётки (другого имени у неё нет); у оценки автора материала коллеги (#131) учётки нет: его имя в Telegram. У
# отметки участника без прав администратора (#188) её неприменённые правки и «Дубль»: proposed_title,
# proposed_thesis, duplicate_of_signal_id; applied: применены ли явные действия отметки.
_FEEDBACK_BLOCKS_SQL = f"""(
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
           'id', sfe.id, 'author', COALESCE(u.email, sfe.author), 'verdict', NULLIF(sfe.verdict, ''),
           'reason', NULLIF(sfe.reason, ''), 'comment', NULLIF(sfe.comment, ''),
           'proposed_title', CASE WHEN NOT sfe.applied THEN NULLIF(btrim(sfe.corrected_title), '') END,
           'proposed_thesis', CASE WHEN NOT sfe.applied THEN NULLIF(btrim(sfe.corrected_thesis), '') END,
           'duplicate_of_signal_id', CASE WHEN NOT sfe.applied THEN sfe.duplicate_of_signal_id END,
           'applied', sfe.applied, 'created_at', sfe.created_at) ORDER BY sfe.created_at, sfe.id),
         '[]'::jsonb)
  FROM signal_feedback_events sfe
  LEFT JOIN users u ON u.id = sfe.user_id
  WHERE {_FEEDBACK_OF_SIGNAL_SQL} AND {_FEEDBACK_IS_BLOCK_SQL})"""


# У карточки есть запись блока ОС, та, что в списке «Оценки людей» (решение владельца 03.11). Правило отзыва то же, что
# _FEEDBACK_OF_SIGNAL_SQL, но двумя EXISTS по индексам номера и адреса: один EXISTS с OR перебирал все отзывы на каждую
# карточку витрины (прод 03.11, бизнес-лента: 801 мс против 190 мс, карточки те же).
SIGNAL_HAS_FEEDBACK_SQL = (
    f"(EXISTS (SELECT 1 FROM signal_feedback_events sfe WHERE sfe.signal_id = s.id AND {_FEEDBACK_IS_BLOCK_SQL})"
    " OR EXISTS (SELECT 1 FROM signal_evidence se JOIN signal_feedback_events sfe ON sfe.source_url = se.source_url"
    f" WHERE se.signal_id = s.id AND {_FEEDBACK_IS_BLOCK_SQL} AND {_FEEDBACK_SAME_ORIGIN_SQL}))"
)
# Фильтр «Наличие ОС»: значение параметра и условие выборки, как у плашек (RADAR_TILE_SQL).
SIGNAL_FEEDBACK_SQL = {"with": SIGNAL_HAS_FEEDBACK_SQL, "without": f"NOT {SIGNAL_HAS_FEEDBACK_SQL}"}
# «Поступил» на экране: дата первой находки по Москве (сутки радара: московские).
_RADAR_ARRIVAL_DATE_SQL = "(s.first_seen_at AT TIME ZONE 'Europe/Moscow')::date"
RADAR_SORTS = {
    "score_desc": "s.score DESC, s.last_seen_at DESC, s.id DESC",
    "date_desc": "s.first_seen_at DESC, s.score DESC, s.id DESC",
    "score_asc": "s.score ASC, s.first_seen_at DESC, s.id DESC",
}


def _like_contains(text: str) -> str:
    """Шаблон «содержит» для LIKE: «%» и «_» из запроса человека: обычные символы."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# Участники карточки (#97): элементы списка participants_json; не список (запись мимо агента): участников нет.
_PARTICIPANTS_SQL = (
    "jsonb_array_elements(CASE WHEN jsonb_typeof(s.participants_json) = 'array'"
    " THEN s.participants_json ELSE '[]'::jsonb END) p"
)
# Участник карточки, одно из имён компании (#97): имена участников из базы, которые по правилу «одна компания»
# та же компания, сверены при запросе (signal_card_filters.group_names), здесь точное сравнение.
_PARTICIPANT_NAMED_SQL = f"EXISTS (SELECT 1 FROM {_PARTICIPANTS_SQL} WHERE p->>'name' = ANY(%s))"
# Страна сигнала (#137), в любой из двух ролей: страна события или страна участника (signal_card_countries).
_COUNTRY_SQL = f"(%s = ANY(s.event_countries) OR EXISTS (SELECT 1 FROM {_PARTICIPANTS_SQL} WHERE p->>'country' = %s))"


def participant_names() -> list[str]:
    """Все разные имена участников карточек (#97), по всем сигналам: из них фильтр «всё про компанию» берёт те,
    что по правилу «одна компания» та же компания (signal_card_filters.group_names); они же подсказки формы на
    экране «Дочерние общества». Элемент без имени не в счёт."""
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT p->>'name' FROM signals s, {_PARTICIPANTS_SQL} WHERE p->>'name' IS NOT NULL"
        ).fetchall()
    return [str(row[0]) for row in rows]


def _without_jit(conn) -> None:
    """Без JIT-компиляции в этой транзакции: витрину «бизнес» (7 тыс. сигналов) планировщик Postgres
    переоценивает, и JIT съедал 3,1 из 3,35 с страницы; без него: 171 мс (замер на проде 15.10, #95)."""
    conn.execute("SET LOCAL jit = off")


def _showcase_visible_sql(showcase: str, *, hidden: bool = False, section=None) -> str:
    """Видимость карточки в витрине: «технологии»: прежний радар (и его «Отсеянные и
    скрытые»), «бизнес»: категория «бизнес» обоих происхождений (#95). Отсеянных у витрины
    «бизнес» нет: «Отсеянные и скрытые»: разбор работы радара. section: раздел отклонённого
    (rejected_sections.Section, #130): вместо витрины: его сигналы любой категории."""
    if section is not None:
        return f"({section.condition})"
    if showcase not in SIGNAL_SHOWCASES:
        raise ValueError(f"Неизвестная витрина сигналов: {showcase!r}")
    if showcase == "business":
        if hidden:
            raise ValueError("«Отсеянные и скрытые» есть только у витрины технологий")
        return _BUSINESS_VISIBLE_SQL
    return _RADAR_HIDDEN_SQL if hidden else _RADAR_VISIBLE_SQL


def _radar_filters(*, maturity: str | None = None, theme: str | None = None, q: str | None = None,
                   since: date | str | None = None, until: date | str | None = None,
                   min_score: float | None = None, max_score: float | None = None,
                   hidden: bool = False, showcase: str = "technology", event_type: str | None = None,
                   region: str | None = None, technology_class: str | None = None,
                   technology_maturity: str | None = None, country: str | None = None,
                   company_names: Sequence[str] | None = None, section=None,
                   tile: str | None = None, feedback: str | None = None) -> tuple[list[str], list]:
    """Условия выборки экрана радара. Поиск, по тому, что человек видит в карточке: заголовок
    и суть (с правками людей), тезис, тема, переносимость, заголовки и издатели ссылок, в том
    числе ссылок склеенного дубля; «#123»: номер карточки из поля «ID дубля». Атрибуты карточки
    (#97), значение справочника как есть, «не определено» тоже: так находятся пробелы карточек;
    компания: участник карточки под любым из имён company_names (signal_card_filters.group_names); пустой
    список, имён нет, и карточек тоже: фильтр есть и ничего не нашёл, а не «без фильтра». Страна: имя
    справочника стран в любой роли: страна события или страна участника. tile: плашка над
    списком (решение владельца 23.10): то же условие, что у её числа в signal_radar_summary. feedback: наличие
    ОС (решение владельца 03.11), with значит у карточки есть запись блока ОС, without значит нет."""
    clauses = [_showcase_visible_sql(showcase, hidden=hidden, section=section)]
    params: list = []
    if feedback:
        feedback_sql = SIGNAL_FEEDBACK_SQL.get(feedback)
        if feedback_sql is None:
            raise ValueError(f"Неизвестный фильтр ОС: {feedback!r}")
        clauses.append(feedback_sql)
    if tile:
        tile_sql = RADAR_TILE_SQL.get(tile)
        if tile_sql is None:
            raise ValueError(f"Неизвестная плашка радара: {tile!r}")
        clauses.append(tile_sql)
    if maturity:
        clauses.append("s.maturity = %s")
        params.append(maturity)
    for column, value in (("event_type", event_type), ("region", region), ("technology_class", technology_class),
                          ("technology_maturity", technology_maturity)):
        if value:
            clauses.append(f"s.{column} = %s")
            params.append(value)
    if country:
        clauses.append(_COUNTRY_SQL)
        params.extend([country, country])
    if company_names is not None:
        clauses.append(_PARTICIPANT_NAMED_SQL)
        params.append(list(company_names))
    if theme:
        clauses.append("s.theme ILIKE %s")
        params.append(f"%{theme}%")
    query = (q or "").strip()
    if query:
        pattern = _like_contains(query.lower())
        text_sql = (
            "LOWER(concat_ws(' ', s.title, s.title_ru, s.theme, s.summary, s.thesis, s.transferability,"
            " corr.corrected_title, corr.corrected_thesis)) LIKE %s"
        )
        # Ссылки, свои и склеенного дубля: условие входит в оба EXISTS, шаблон: дважды.
        evidence_sql = _radar_card_evidence_exists("LOWER(concat_ws(' ', e.title, e.title_ru, e.publisher)) LIKE %s")
        alternatives = [text_sql, evidence_sql]
        params.extend([pattern, pattern, pattern])
        # Только ASCII-цифры: str.isdigit() пропускает «²», и int() на нём падает.
        number = query.lstrip("#")
        if re.fullmatch(r"[0-9]{1,18}", number):
            alternatives.append("s.id = %s")
            params.append(int(number))
        clauses.append(f"({' OR '.join(alternatives)})")
    if since:
        clauses.append(f"{_RADAR_ARRIVAL_DATE_SQL} >= %s")
        params.append(since)
    if until:
        clauses.append(f"{_RADAR_ARRIVAL_DATE_SQL} <= %s")
        params.append(until)
    if min_score is not None:
        clauses.append("s.score >= %s")
        params.append(min_score)
    if max_score is not None:
        clauses.append("s.score <= %s")
        params.append(max_score)
    return clauses, params


def list_signals(*, maturity: str | None = None, theme: str | None = None, limit: int = 50,
                 q: str | None = None, since: date | str | None = None,
                 until: date | str | None = None, min_score: float | None = None,
                 max_score: float | None = None, sort: str = "score_desc", offset: int = 0,
                 hidden: bool = False, showcase: str = "technology", event_type: str | None = None,
                 region: str | None = None, technology_class: str | None = None,
                 technology_maturity: str | None = None, country: str | None = None,
                 company_names: Sequence[str] | None = None, section=None, tile: str | None = None,
                 feedback: str | None = None) -> list[dict]:
    """Карточки радара для экрана: выборка и страница: в базе, а не по загруженным на экран
    (замечание заказчика 19.09: поиск находил только среди первых 150 по баллу).
    hidden=True, скрытые правилами качества (для админа), с причиной в hidden_reason; section:
    раздел отклонённого (#130, только админу), с короткой причиной в section_reason."""
    clauses, filter_params = _radar_filters(
        maturity=maturity, theme=theme, q=q, since=since, until=until, min_score=min_score, max_score=max_score,
        hidden=hidden, showcase=showcase, event_type=event_type, region=region, technology_class=technology_class,
        technology_maturity=technology_maturity, country=country, company_names=company_names, section=section,
        tile=tile, feedback=feedback,
    )
    order_by = RADAR_SORTS.get(sort)
    if order_by is None:
        raise ValueError(f"Unknown radar sort: {sort}")
    where = f"WHERE {' AND '.join(clauses)}"
    params = [SYSTEM_TAG_UNCLASSIFIED, *filter_params, limit, max(0, int(offset or 0))]
    with get_connection() as conn:
        _without_jit(conn)
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT s.*,
                   {_RADAR_TOPIC_SQL} AS theme_is_topic,
                   {_radar_hidden_reason_sql() if showcase == "technology" else "NULL"} AS hidden_reason,
                   {section.short_reason if section is not None else "NULL"} AS section_reason,
                   -- Месяц выпуска карточки: по нему экран гасит «В дайджест» и правку материала в
                   -- закрытом месяце тем же выражением, что у выпуска (signal_issue_month_sql).
                   {signal_issue_month_sql('s')} AS digest_month,
                   {marks.signal_status_sql()} AS user_status,
                   {signal_digest_sql('s')} AS selected_for_digest,
                   sm.analyst_comment AS user_comment,
                   sm.updated_at AS user_status_updated_at,
                   (SELECT COUNT(*) FROM signals m WHERE m.merged_into_signal_id = s.id) AS merged_count,
                   {_FEEDBACK_BLOCKS_SQL} AS feedback_blocks,
                   -- Предложение разбора ОС, которое ждёт решения в журнале ОС (#189): значок на карточке.
                   (SELECT fa.id FROM feedback_actions fa WHERE fa.signal_id = s.id AND fa.kind = 'signal_proposal'
                      AND fa.status = 'pending' ORDER BY fa.id DESC LIMIT 1) AS feedback_pending,
                   corr.corrected_title,
                   corr.corrected_thesis
            FROM signals s
            {marks.signal_mark_join()}
            {_signal_corrections_lateral("s")}
            {where}
            ORDER BY {order_by}
            LIMIT %s OFFSET %s
            """,
            params,
        )
        rows = [apply_signal_corrections(row) for row in cur.fetchall()]
    # «Обратная связь: N»: число записей в списке «Оценки людей» (решение владельца 03.11): «В дайджест» не ОС.
    for row in rows:
        row["feedback_count"] = len(row["feedback_blocks"])
    return rows


# Плашки над списком радара и бизнес-ленты: число плашки и выборка по её нажатию: одно условие.
RADAR_TILE_SQL = {
    "new_7d": "s.first_seen_at >= now() - interval '7 days'",
    "in_digest": signal_digest_sql("s"),
    # «С обратной связью» считает то же, что фильтр «Есть ОС» (решение владельца 03.11).
    "with_feedback": SIGNAL_HAS_FEEDBACK_SQL,
}


def signal_radar_summary(**filters) -> dict:
    """Числа над списком радара: той же видимостью карточки, что и список. Плитки: по всему
    радару (поиск их не меняет); «в дайджесте»: по общей отметке, как кнопка на карточке. matching: сколько в текущей выборке (фильтры: как у list_signals)."""
    clauses, filter_params = _radar_filters(**filters)
    showcase = filters.get("showcase") or "technology"
    visible = _showcase_visible_sql(showcase)
    with get_connection() as conn:
        _without_jit(conn)
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE {RADAR_TILE_SQL['new_7d']}) AS new_7d,
                   COUNT(*) FILTER (WHERE {RADAR_TILE_SQL['in_digest']}) AS in_digest,
                   COUNT(*) FILTER (WHERE {RADAR_TILE_SQL['with_feedback']}) AS with_feedback
            FROM signals s
            {marks.signal_mark_join()}
            WHERE {visible}
            """
        )
        tiles = {key: int(value) for key, value in cur.fetchone().items()}
        # Дубли, скрытые в видимых карточках: их ссылки: в главной карточке.
        cur.execute(
            f"""
            SELECT COUNT(*) AS merged
            FROM signals d
            JOIN signals s ON s.id = d.merged_into_signal_id
            WHERE {visible}
            """
        )
        tiles["merged"] = int(cur.fetchone()["merged"])
        # Плашка «Отклонённые» (#130): все разделы отклонённого одним числом, одно в обеих лентах.
        from oiltech_digest.db import rejected_sections

        tiles["rejected"] = rejected_sections.total()
        total = tiles["total"]
        matching = total
        # Скрытые и раздел, своя выборка: «N из M» считается по ним даже без других фильтров.
        if len(clauses) > 1 or filters.get("hidden") or filters.get("section") is not None:
            cur.execute(
                f"""
                SELECT COUNT(*) AS matching
                FROM signals s
                {_signal_corrections_lateral("s")}
                WHERE {' AND '.join(clauses)}
                """,
                filter_params,
            )
            matching = int(cur.fetchone()["matching"])
        cur.execute(
            f"""
            SELECT s.theme, COUNT(*) AS count
            FROM signals s
            WHERE {visible} AND {_RADAR_TOPIC_SQL}
            GROUP BY s.theme
            ORDER BY s.theme
            """,
            [SYSTEM_TAG_UNCLASSIFIED],
        )
        themes = [{"theme": row["theme"], "count": int(row["count"])} for row in cur.fetchall()]
    return {**tiles, "matching": matching, "themes": themes}


def list_radar_evidence(signal_ids: Sequence[int], *, limit: int) -> dict[int, list[dict]]:
    """Ссылки карточек страницы радара, одним запросом, а не list_signal_evidence на каждую:
    подключения к базе без пула, и на страницу в 100 карточек выходило ~100 подключений на
    каждую смену фильтра. Выборка та же: свои ссылки и ссылки склеенных дублей, тот же
    порядок и предел на карточку.

    Участники карточки (она сама и её дубли), отдельным списком и простым JOIN: с «OR
    e.signal_id IN (подзапрос)» в условии JOIN база гоняла подзапрос по каждой паре
    карточка × ссылка (ревью PR #83: 150 карточек: 1 118 → 4 мс; на синтетике 600 карточек /
    1 651 ссылка: 342 → 15 мс, строки и порядок те же)."""
    ids = [int(signal_id) for signal_id in signal_ids]
    if not ids or limit <= 0:
        return {}
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            WITH members AS (
              SELECT s.id AS card_id, s.id AS member_id FROM signals s WHERE s.id = ANY(%s)
              UNION ALL
              SELECT m.merged_into_signal_id AS card_id, m.id AS member_id
              FROM signals m WHERE m.merged_into_signal_id = ANY(%s)
            ),
            linked AS (
              SELECT mb.card_id, e.id AS evidence_id
              FROM members mb
              JOIN signal_evidence e ON e.signal_id = mb.member_id
              UNION
              -- Статья сигнала ленты — его публикация, даже когда её адрес держит карточка радара
              -- (адрес уникален, ссылка остаётся у радара — feed_signals): показываем ту же ссылку.
              SELECT s.id, e.id
              FROM signals s
              JOIN articles a ON a.id = s.origin_article_id
              JOIN signal_evidence e ON e.source_url = a.url
              WHERE s.id = ANY(%s) AND s.origin = '{SIGNAL_ORIGIN_FEED}'
            )
            SELECT *
            FROM (
              SELECT e.*, l.card_id AS radar_card_id,
                     ROW_NUMBER() OVER (
                       PARTITION BY l.card_id
                       ORDER BY e.strength DESC, e.published_at DESC NULLS LAST, e.created_at DESC, e.id DESC
                     ) AS radar_card_rank
              FROM linked l
              JOIN signal_evidence e ON e.id = l.evidence_id
            ) ranked
            WHERE ranked.radar_card_rank <= %s
            ORDER BY ranked.radar_card_id, ranked.radar_card_rank
            """,
            (ids, ids, ids, limit),
        )
        evidence: dict[int, list[dict]] = {}
        for row in cur.fetchall():
            card_id = int(row.pop("radar_card_id"))
            row.pop("radar_card_rank")
            evidence.setdefault(card_id, []).append(row)
    return evidence


def list_signal_evidence(signal_id: int, *, limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT *
            FROM signal_evidence
            WHERE signal_id = %s
               OR signal_id IN (SELECT id FROM signals WHERE merged_into_signal_id = %s)
            ORDER BY strength DESC, published_at DESC NULLS LAST, created_at DESC
            LIMIT %s
            """,
            (signal_id, signal_id, limit),
        )
        return cur.fetchall()


def upsert_signal_agent_memory(
    *,
    memory_key: str,
    memory_type: str,
    subject: str,
    status: str = "active",
    score: float = 0,
    facts: dict | None = None,
) -> int:
    key = memory_key.strip()
    if not key:
        raise ValueError("signal agent memory_key is required")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO signal_agent_memory
              (memory_key, memory_type, subject, status, score, facts_json, last_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (memory_key) DO UPDATE SET
              memory_type = EXCLUDED.memory_type,
              subject = EXCLUDED.subject,
              status = EXCLUDED.status,
              score = EXCLUDED.score,
              facts_json = EXCLUDED.facts_json,
              last_seen_at = now(),
              updated_at = now()
            RETURNING id
            """,
            (key, memory_type, subject, status, score, Json(_jsonable(facts or {}))),
        )
        memory_id = int(cur.fetchone()[0])
        conn.commit()
        return memory_id


def list_signal_agent_memory(
    *,
    memory_type: str | None = None,
    status: str | None = "active",
    limit: int = 50,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if memory_type:
        clauses.append("memory_type = %s")
        params.append(memory_type)
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT *
            FROM signal_agent_memory
            {where}
            ORDER BY score DESC, updated_at DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def unmerge_signal(signal_id: int) -> bool:
    """Снять пометку дубля (ошибочный «Дубль #ID»): карточка снова видна."""
    with get_connection() as conn:
        row = conn.execute(
            "UPDATE signals SET merged_into_signal_id = NULL, merge_reason = NULL, updated_at = now()"
            " WHERE id = %s AND merged_into_signal_id IS NOT NULL RETURNING id",
            (int(signal_id),),
        ).fetchone()
        conn.commit()
        return row is not None


def signal_issue_month_sql(alias: str = "s") -> str:
    """Месяц выпуска сигнала строкой «ГГГГ-ММ»: у сигнала ленты месяц его статьи («в дайджест» у него отметка статьи,
    #130), у прочих месяц поступления по Москве (signal_month_sql), как у сборщика выпуска."""
    return (f"CASE WHEN {alias}.origin = '{SIGNAL_ORIGIN_FEED}'"
            f" THEN (SELECT {period_month_sql('da')} FROM articles da WHERE da.id = {alias}.origin_article_id)"
            f" ELSE {signal_month_sql(alias)} END")


def signal_issue_month(signal_id: int) -> str | None:
    """Месяц выпуска сигнала (signal_issue_month_sql): по нему правка материала закрытого месяца получает отказ (#162).
    Сигнала нет: KeyError; месяц не определить (у сигнала ленты нет статьи): None."""
    with get_connection() as conn:
        row = conn.execute(f"SELECT {signal_issue_month_sql('s')} FROM signals s WHERE s.id = %s",
                           (int(signal_id),)).fetchone()
    if row is None:
        raise KeyError(signal_id)
    return row[0]


def signal_selected_for_digest(signal_id: int) -> bool:
    """Отмечен ли сигнал «в дайджест» (signal_digest_sql: у сигнала ленты отметка его статьи): материал выпуска (#162)."""
    with get_connection() as conn:
        row = conn.execute(f"SELECT {signal_digest_sql('s')} FROM signals s WHERE s.id = %s",
                           (int(signal_id),)).fetchone()
    return bool(row and row[0])


def feed_signal_article(signal_id: int) -> int | None:
    """Статья сигнала ленты (#95): её отметку ставит кнопка «В дайджест» витрины (#130); None: сигнал не ленты
    или его нет."""
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT origin_article_id FROM signals WHERE id = %s AND origin = '{SIGNAL_ORIGIN_FEED}'",
            (int(signal_id),),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def signal_digest_refusal(signal_id: int) -> str | None:
    """Почему сигнал не ставится «в дайджест» (#130): его отклонил «Отбор» или «Отбор» не дал решения (сбой). У
    такого и на экране кнопки нет; вернуть отклонённое: #132. None: ставится (или сигнала нет)."""
    with get_connection() as conn:
        row = conn.execute("SELECT otbor_decision, otbor_failure FROM signals WHERE id = %s",
                           (int(signal_id),)).fetchone()
    if row is None:
        return None
    if row[0] == feed_window.OTBOR_REJECT:
        return "публикацию отклонил «Отбор»"
    return "«Отбор» не дал решения" if row[0] is None and row[1] else None


def collected_url_keys_since(*, since: date | datetime) -> list[str]:
    """Ключи адресов статей сбора, собранных с этой даты (normalize.url_key): широкий поиск радара (#138) не делает
    находкой ссылку, которая уже есть в сборе. Статья на удалении в счёт не идёт."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT url_key FROM articles WHERE collected_at >= %s AND url_key IS NOT NULL"
            " AND NOT pending_deletion",
            (since,),
        ).fetchall()
    return sorted(row[0] for row in rows)


def collected_url_keys_among(keys: list[str]) -> set[str]:
    """Какие из этих ключей адресов (normalize.url_key) есть среди статей сбора за всё время (#138)."""
    keys = [key for key in keys if key]
    if not keys:
        return set()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT url_key FROM articles WHERE url_key = ANY(%s) AND NOT pending_deletion", (keys,),
        ).fetchall()
    return {row[0] for row in rows}


def covered_collection_sites(*, since: date | datetime, until: date | datetime, min_articles: int) -> list[str]:
    """Сайты, которые сбор за период охватывает целиком (#138): у сайта источников сбора не меньше min_articles
    статей, собранных за период; Telegram-каналы: не сайты для поиска. Сайт: хост адреса источника без www;
    у нескольких источников одного сайта (разделы JPT) статьи складываются. Больше статей: раньше в списке."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.url, count(*) AS n
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.collected_at >= %s AND a.collected_at < %s AND NOT a.pending_deletion
              AND s.source_type IS DISTINCT FROM 'Telegram'
            GROUP BY s.url
            """,
            (since, until),
        ).fetchall()
    counts: dict[str, int] = {}
    for url, count in rows:
        host = (urlsplit(str(url or "").strip().lower()).hostname or "").removeprefix("www.")
        if host and host != "t.me":
            counts[host] = counts.get(host, 0) + int(count)
    return [host for host, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            if count >= min_articles]


def list_reviewed_signal_urls() -> list[str]:
    """Адреса материалов, по которым человек уже вынес суждение о сигнале.

    Радар не должен приносить их снова: отклонённое вернулось бы тем же сигналом,
    а одобренное перезаписалось бы новой оценкой модели поверх разобранного
    (signal_evidence.source_url уникален: повторная находка забирает материал себе).
    Ссылка материала коллеги (#131), тоже: его прислал и оценил человек, а второй сигнал той же
    публикации «Отбор» решал бы ещё раз."""
    with get_connection() as conn:
        cur = conn.execute(
            f"""
            WITH reviewed AS (
                -- Только находки радара (#95): отзыв о сигнале ленты — не повод радару не
                -- приносить тот же адрес.
                SELECT f.signal_id AS id FROM signal_feedback_events f
                JOIN signals rs ON rs.id = f.signal_id AND {radar_origin_sql("rs")}
                WHERE (f.verdict IS NOT NULL OR f.event_type = 'comment_added')
                UNION
                SELECT ms.id FROM signals ms WHERE ms.origin = '{SIGNAL_ORIGIN_MATERIAL}'
            )
            SELECT DISTINCT e.source_url
            FROM signal_evidence e
            WHERE e.signal_id IN (SELECT id FROM reviewed)
               -- ссылки скрытых дублей разобранной карточки — тоже разобраны
               OR e.signal_id IN (SELECT s.id FROM signals s WHERE s.merged_into_signal_id IN (SELECT id FROM reviewed))
            UNION
            SELECT DISTINCT f.source_url
            FROM signal_feedback_events f
            JOIN signals rs ON rs.id = f.signal_id AND {radar_origin_sql("rs")}
            WHERE COALESCE(f.source_url, '') <> ''
            """
        )
        return [str(row[0]) for row in cur.fetchall() if row[0]]


def compute_source_quality_rows(period_from: datetime, period_to: datetime) -> list[dict]:
    """Посчитать качество источников за период без записи в историю."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            WITH article_metrics AS (
              SELECT
                a.id,
                a.source_id,
                c.summary,
                {feed_window.relevance_sql('a', 'c')} AS relevant,
                c.status AS card_status,
                sc.total_score,
                {marks.article_marked_sql('a.id', 'digest')} AS user_digest,
                {marks.article_marked_sql('a.id', 'duplicate')} AS user_duplicate,
                {marks.article_marked_sql('a.id', 'noise')} AS user_noise,
                EXISTS (SELECT 1 FROM article_tags at WHERE at.article_id = a.id) AS has_tag,
                COALESCE((
                  SELECT SUM(r.cost_usd)
                  FROM ai_processing_runs r
                  WHERE r.article_id = a.id AND r.status = 'ok'
                ), 0) AS processing_cost_usd
              FROM articles a
              LEFT JOIN article_cards c ON c.article_id = a.id
              LEFT JOIN article_scores sc ON sc.article_id = a.id
              WHERE a.collected_at >= %s
                AND a.collected_at < %s
            )
            SELECT
              s.id AS source_id,
              s.name AS source_name,
              s.enabled,
              s.parse_strategy,
              s.source_type,
              s.update_frequency,
              s.network_region,
              s.network_profile,
              s.last_ru_probe_status,
              s.last_external_probe_status,
              s.external_required_reason,
              s.last_seen_published_at,
              COUNT(am.id) AS articles_found,
              COUNT(am.id) FILTER (
                WHERE am.summary IS NOT NULL
                   OR am.relevant IS NOT NULL
                   OR am.has_tag
                   OR am.total_score IS NOT NULL
              ) AS articles_processed,
              COUNT(am.id) FILTER (WHERE am.relevant IS TRUE) AS relevant_count,
              COUNT(am.id) FILTER (WHERE am.relevant IS FALSE OR am.card_status = 'rejected') AS rejected_count,
              ROUND(AVG(am.total_score), 2) AS avg_score,
              COUNT(am.id) FILTER (WHERE am.user_digest OR am.card_status = 'digest') AS digest_count,
              COUNT(am.id) FILTER (WHERE am.user_duplicate OR am.card_status = 'duplicate') AS duplicate_count,
              COUNT(am.id) FILTER (WHERE am.user_noise OR am.card_status = 'noise') AS noise_count,
              COALESCE(ROUND(SUM(am.processing_cost_usd), 6), 0) AS processing_cost_usd
            FROM sources s
            LEFT JOIN article_metrics am ON am.source_id = s.id
            GROUP BY s.id, s.name, s.enabled, s.parse_strategy, s.source_type,
              s.update_frequency, s.network_region, s.network_profile,
              s.last_ru_probe_status, s.last_external_probe_status,
              s.external_required_reason, s.last_seen_published_at
            ORDER BY articles_found DESC, s.name
            """,
            (period_from, period_to),
        )
        rows = []
        for row in cur.fetchall():
            item = dict(row)
            item["quality_score"] = _source_quality_score(item)
            rows.append(item)
        return rows


def snapshot_source_quality(period_from: datetime, period_to: datetime) -> int:
    rows = compute_source_quality_rows(period_from, period_to)
    if not rows:
        return 0
    with get_connection() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO source_quality_snapshots (
                  source_id, period_from, period_to, articles_found, articles_processed,
                  relevant_count, rejected_count, avg_score, digest_count, duplicate_count,
                  noise_count, processing_cost_usd, quality_score
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    row["source_id"], period_from, period_to, row["articles_found"],
                    row["articles_processed"], row["relevant_count"], row["rejected_count"],
                    row["avg_score"], row["digest_count"], row["duplicate_count"],
                    row["noise_count"], row["processing_cost_usd"], row["quality_score"],
                ),
            )
        conn.commit()
    return len(rows)


def _source_quality_score(row: dict) -> float:
    found = max(int(row.get("articles_found") or 0), 1)
    relevant_rate = int(row.get("relevant_count") or 0) / found
    digest_rate = int(row.get("digest_count") or 0) / found
    duplicate_rate = int(row.get("duplicate_count") or 0) / found
    noise_rate = int(row.get("noise_count") or 0) / found
    avg_score = float(row.get("avg_score") or 0) / 100
    score = (
        relevant_rate * 35
        + avg_score * 25
        + digest_rate * 25
        - duplicate_rate * 10
        - noise_rate * 5
    )
    return round(max(0.0, min(100.0, score)), 2)


def article_exists(url: str) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT 1 FROM articles WHERE url = %s", (url,)).fetchone()
        return row is not None


def set_source_collection(source_id: int, *, listing_url: str | None, network_region: str,
                          enabled: bool) -> None:
    """Записать, чем и откуда собирать источник, итог перебора стратегий.

    Архивный источник этим не воскрешается: `enabled` ставится только вместе с
    `archived_at IS NULL`, как и в add_rss_source.
    """
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE sources
            SET listing_url = %s, network_region = %s,
                enabled = (%s AND archived_at IS NULL), updated_at = now()
            WHERE id = %s
            """,
            (listing_url, network_region, bool(enabled), int(source_id)),
        )
        conn.commit()


def recent_article_urls_for_site(site_url: str | None, limit: int = 500) -> list[str]:
    """Последние адреса статей этого сайта и его поддоменов: от любого источника.

    Для внешнего воркера: базы у него нет, и без этого списка он качает каждую статью
    листинга на каждом цикле. Сайт, а не источник: у JPT восемь разделов-источников
    делят одни статьи, и знакомую для соседа статью качать тоже незачем.
    """
    host = (urlsplit(site_url or "").netloc or "").lower().removeprefix("www.")
    if not host:
        return []
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT url FROM articles
            WHERE lower(split_part(url, '/', 3)) IN (%s, %s)
               OR lower(split_part(url, '/', 3)) LIKE %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (host, "www." + host, "%." + host, int(limit)),
        ).fetchall()
    return [row[0] for row in rows]


def update_source_request_state(
    source_id: int,
    *,
    last_seen_article_url: str | None = None,
    last_seen_published_at=None,
    last_listing_hash: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE sources
            SET last_seen_article_url = COALESCE(%s, last_seen_article_url),
                last_seen_published_at = COALESCE(%s, last_seen_published_at),
                last_listing_hash = COALESCE(%s, last_listing_hash),
                updated_at = now()
            WHERE id = %s
            """,
            (last_seen_article_url, last_seen_published_at, last_listing_hash, source_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
#  auth
# ---------------------------------------------------------------------------

def create_user(email: str, password: str, role: str = "user") -> dict:
    email = auth.normalize_email(email)
    role = role if role in ("admin", "user") else "user"
    salt_hex, password_hash = auth.hash_password(password)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        if cur.fetchone() is not None:
            raise ValueError("Пользователь с таким email уже существует")
        cur.execute(
            """
            INSERT INTO users (email, password_salt, password_hash, role)
            VALUES (%s, %s, %s, %s)
            RETURNING id, email, role, created_at
            """,
            (email, salt_hex, password_hash, role),
        )
        user = cur.fetchone()
        conn.commit()
        return user


def authenticate_user(email: str, password: str) -> dict | None:
    email = auth.normalize_email(email)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, email, role, password_salt, password_hash, created_at FROM users WHERE email = %s",
            (email,),
        )
        user = cur.fetchone()
        if user is None:
            return None
        if not auth.verify_password(password, user["password_salt"], user["password_hash"]):
            return None
        return {"id": user["id"], "email": user["email"], "role": user["role"], "created_at": user["created_at"]}


def create_user_session(user_id: int) -> str:
    token = auth.create_session_token()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_sessions (user_id, session_token, expires_at)
            VALUES (%s, %s, now() + %s::interval)
            """,
            (user_id, token, f"{config.AUTH_SESSION_DAYS} days"),
        )
        conn.commit()
    return token


def get_user_by_session(session_token: str) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT u.id, u.email, u.role, u.created_at
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.session_token = %s
              AND s.expires_at > now()
            """,
            (session_token,),
        )
        user = cur.fetchone()
        if user is not None:
            conn.execute(
                "UPDATE user_sessions SET last_seen_at = now() WHERE session_token = %s",
                (session_token,),
            )
            conn.commit()
        return user


def delete_user_session(session_token: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM user_sessions WHERE session_token = %s", (session_token,))
        conn.commit()


def delete_expired_user_sessions() -> int:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM user_sessions WHERE expires_at <= now()")
        conn.commit()
        return cur.rowcount or 0


def count_expired_user_sessions() -> int:
    with get_connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM user_sessions WHERE expires_at <= now()").fetchone()[0])


def count_users() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def list_users() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id, email, role, created_at FROM users ORDER BY id")
        return cur.fetchall()


def get_user_by_id(user_id: int) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id, email, role, created_at FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()


def set_user_role(user_id: int, role: str) -> None:
    role = role if role in ("admin", "user") else "user"
    with get_connection() as conn:
        conn.execute("UPDATE users SET role = %s, updated_at = now() WHERE id = %s", (role, user_id))
        conn.commit()


def set_user_password(user_id: int, password: str) -> None:
    salt_hex, password_hash = auth.hash_password(password)
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET password_salt = %s, password_hash = %s, updated_at = now() WHERE id = %s",
            (salt_hex, password_hash, user_id),
        )
        conn.commit()


def delete_user(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM user_sessions WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()


def count_admins() -> int:
    with get_connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0])


def ensure_admin_bootstrap() -> int | None:
    """Если админов нет, а пользователи есть: назначить админом самого первого
    (по id). Возвращает id назначенного админа или None. Идемпотентно."""
    with get_connection() as conn:
        has_admin = conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        if has_admin:
            return None
        row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("UPDATE users SET role = 'admin', updated_at = now() WHERE id = %s", (row[0],))
        conn.commit()
        return int(row[0])


def create_export_job(export_type: str, export_format: str) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO export_jobs (export_type, format, status, started_at)
            VALUES (%s, %s, 'running', now())
            RETURNING id
            """,
            (export_type, export_format),
        )
        job_id = cur.fetchone()[0]
        conn.commit()
        return job_id


def finish_export_job(job_id: int, status: str, file_path: str | None = None,
                      error_message: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE export_jobs
            SET status = %s,
                file_path = COALESCE(%s, file_path),
                error_message = %s,
                finished_at = now()
            WHERE id = %s
            """,
            (status, file_path, error_message, job_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
#  background jobs
# ---------------------------------------------------------------------------

def create_background_job(
    kind: str,
    payload: dict | None = None,
    *,
    user_id: int | None = None,
    queue_name: str = "default",
    execution_region: str = "ru",
    capability: str | None = None,
    max_attempts: int = 3,
    agent_run_id: int | None = None,
) -> dict:
    # Агентные задачи: в свою полосу, как бы их ни назвал вызывающий код (lanes.route).
    queue_name = lanes.route(queue_name, kind)
    # Внешняя очередь принимает только то, что её воркер умеет исполнять (lanes.py).
    lanes.check_enqueue(queue_name, kind)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            INSERT INTO background_jobs (
                user_id, kind, queue_name, execution_region, capability,
                agent_run_id, status, progress, max_attempts, payload_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'queued', 0, %s, %s)
            RETURNING *
            """,
            (
                user_id,
                kind,
                queue_name,
                execution_region,
                capability,
                agent_run_id,
                max_attempts,
                Json(_jsonable(payload or {})),
            ),
        )
        job = cur.fetchone()
        conn.commit()
        return job


def has_recent_background_job(
    *,
    kind: str,
    payload_subset: dict,
    lookback_hours: int,
    statuses: tuple[str, ...] = ("queued", "running", "finalizing", "ok"),
) -> bool:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT 1
            FROM background_jobs
            WHERE kind = %s
              AND status = ANY(%s)
              AND created_at >= now() - (%s::text || ' hours')::interval
              AND payload_json @> %s::jsonb
            LIMIT 1
            """,
            (kind, list(statuses), lookback_hours, Json(_jsonable(payload_subset))),
        )
        return cur.fetchone() is not None


def has_recent_signal_discovery_search(*, payload_subset: dict, lookback_hours: float) -> bool:
    """Задача радара за окно, которая обыскала или ещё обыскивает свой период: в очереди, идёт, завершается или ok, и
    её прогон не упал. Прогон, где поиск не ответил ни в одной теме, упал при задаче ok
    (`signal_discovery._finish_generation_run`): его окно никто не обыскал. Правило «раз в N суток» (решение владельца
    04.11) держит следующий прогон только такой задачей."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT 1
            FROM background_jobs j
            WHERE j.kind = 'signal_discovery'
              AND j.status IN ('queued', 'running', 'finalizing', 'ok')
              AND j.created_at >= now() - (%s::text || ' hours')::interval
              AND j.payload_json @> %s::jsonb
              AND NOT EXISTS (
                SELECT 1 FROM signal_generation_runs r WHERE r.background_job_id = j.id AND r.status = 'failed'
              )
            LIMIT 1
            """,
            (lookback_hours, Json(_jsonable(payload_subset))),
        )
        return cur.fetchone() is not None


def get_background_job(job_id: int, *, user_id: int | None = None) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        if user_id is None:
            cur.execute("SELECT * FROM background_jobs WHERE id = %s", (job_id,))
        else:
            cur.execute(
                "SELECT * FROM background_jobs WHERE id = %s AND user_id = %s",
                (job_id, user_id),
            )
        return cur.fetchone()


def claim_next_background_job(queue_names: list[str] | None = None) -> dict | None:
    """Atomically claim the oldest queued job for an external worker."""
    queue_names = queue_names or ["default"]
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            WITH next_job AS (
                SELECT id
                FROM background_jobs
                WHERE status = 'queued'
                  AND queue_name = ANY(%s)
                  AND run_after <= now()
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE background_jobs j
            SET status = 'running',
                progress = CASE WHEN j.progress < 10 THEN 10 ELSE j.progress END,
                attempts = j.attempts + 1,
                started_at = COALESCE(j.started_at, now()),
                error_message = NULL
            FROM next_job
            WHERE j.id = next_job.id
            RETURNING j.*
            """,
            (queue_names,),
        )
        job = cur.fetchone()
        conn.commit()
        return job


class RequeueOutcome(NamedTuple):
    """Итог разбора потерянных задач: сколько вернули в очередь и сколько похоронили."""

    requeued: int
    exhausted: int


def _requeue_or_exhaust(conn, *, lost_where: str, lost_params: tuple, reason: str) -> RequeueOutcome:
    """Разобрать задачи, признанные потерянными: вернуть в очередь либо закрыть как failed.

    ПЕРЕОЧЕРЕДЬ: ЭТО ТОЖЕ ПОПЫТКА. Пока `attempts` не сверялся с `max_attempts`, задача,
    которая физически не может завершиться, возвращалась в очередь вечно. Инцидент 24.07:
    задача 1181 (батч 800 статей) намотала 6 кругов при `max_attempts=3`, каждый круг
    начиная с первой статьи и заново оплачивая OpenAI, а потолок попыток не срабатывал
    никогда, потому что его проверял только `fail_background_job`, до которого дело не доходило.
    Исчерпавшие лимит уходят в `failed`: дальше решает человек, а не бесконечный ретрай.

    `lost_where`: SQL-предикат «задача потеряна». Он приходит из кода модуля (константа),
    не из пользовательского ввода; переменная часть предиката передаётся через `lost_params`.
    Вызывающий отвечает ровно за одно: чем он доказывает потерю. Что делать дальше:
    политика, и она живёт здесь, в одном месте, для всех контуров.
    """
    exhausted = conn.execute(
        f"""
        UPDATE background_jobs
        SET status = 'failed',
            finished_at = now(),
            claimed_by = NULL,
            lease_token_hash = NULL,
            lease_expires_at = NULL,
            error_message = %s
        WHERE ({lost_where})
          AND attempts >= max_attempts
        """,
        (f"{reason}; исчерпаны попытки — автоматически не перезапускаем",) + lost_params,
    )
    requeued = conn.execute(
        f"""
        UPDATE background_jobs
        SET status = 'queued',
            progress = 0,
            started_at = NULL,
            claimed_by = NULL,
            lease_token_hash = NULL,
            lease_expires_at = NULL,
            error_message = %s
        WHERE ({lost_where})
          AND attempts < max_attempts
        """,
        (reason,) + lost_params,
    )
    return RequeueOutcome(requeued=requeued.rowcount or 0, exhausted=exhausted.rowcount or 0)


# Внешняя задача считается потерянной ТОЛЬКО по отсутствию валидного lease. Это её настоящий
# признак жизни: воркер шлёт heartbeat перед каждой статьёй, и пока он жив, lease продлевается.
#
# NULL здесь тоже потеря, и это НЕ придирка. Раньше у внешней задачи было две страховки:
# lease и уборщик по настенным часам. Вторую забрали (она и устраивала вечную петлю), значит
# первая обязана покрыть всё поле. Иначе `running` без lease не подберёт уже никто:
# `release_external_background_job_finalize` возвращает задачу в 'running' при откате
# finalize и прямо рассчитывает, что её подберёт «requeue_expired по истечении лиза
# / requeue_stale». Задача в 'running' без lease не имеет ВООБЩЕ никаких доказательств жизни:
# это ровно определение потерянной.
_EXTERNAL_LOST_WHERE = """
    status = 'running'
    AND execution_region = 'external'
    AND (lease_expires_at IS NULL OR lease_expires_at < now())
"""


def requeue_expired_external_leases() -> RequeueOutcome:
    """Разобрать внешние задачи с протухшим lease: воркер потерян."""
    with get_connection() as conn:
        outcome = _requeue_or_exhaust(
            conn,
            lost_where=_EXTERNAL_LOST_WHERE,
            lost_params=(),
            reason="Requeued after external worker lease expired",
        )
        conn.commit()
        return outcome


def claim_external_background_job(
    *,
    queue_names: list[str],
    capabilities: list[str],
    worker_id: str,
    lease_token_hash: str,
    lease_seconds: int,
) -> dict | None:
    """Atomically lease the oldest queued external job for a remote worker."""
    queue_names = queue_names or ["external-ai", "external-fetch", "external-playwright"]
    capabilities = capabilities or []
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            WITH next_job AS (
                SELECT id
                FROM background_jobs
                WHERE status = 'queued'
                  AND execution_region = 'external'
                  AND queue_name = ANY(%s)
                  AND (%s::text[] = '{}'::text[] OR capability IS NULL OR capability = ANY(%s))
                  AND run_after <= now()
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE background_jobs j
            SET status = 'running',
                progress = CASE WHEN j.progress < 10 THEN 10 ELSE j.progress END,
                attempts = j.attempts + 1,
                started_at = COALESCE(j.started_at, now()),
                claimed_by = %s,
                lease_token_hash = %s,
                lease_expires_at = now() + (%s::text || ' seconds')::interval,
                last_heartbeat_at = now(),
                error_message = NULL
            FROM next_job
            WHERE j.id = next_job.id
            RETURNING j.*
            """,
            (queue_names, capabilities, capabilities, worker_id, lease_token_hash, lease_seconds),
        )
        job = cur.fetchone()
        conn.commit()
        return job


def list_background_jobs(
    *,
    status: str | None = None,
    kind: str | None = None,
    queue_name: str | None = None,
    user_id: int | None = None,
    agent_run_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    clauses = []
    params: list = []
    if status:
        clauses.append("status = %s")
        params.append(status)
    if kind:
        clauses.append("kind = %s")
        params.append(kind)
    if queue_name:
        clauses.append("queue_name = %s")
        params.append(queue_name)
    if user_id is not None:
        clauses.append("user_id = %s")
        params.append(user_id)
    if agent_run_id is not None:
        clauses.append("agent_run_id = %s")
        params.append(agent_run_id)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT *
            FROM background_jobs
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


_EXTERNAL_QUEUE_STATUS_COLUMNS = """
              COUNT(*) FILTER (WHERE status = 'queued') AS queued,
              COUNT(*) FILTER (WHERE status = 'running') AS running,
              COUNT(*) FILTER (WHERE status = 'finalizing') AS finalizing,
              COUNT(*) FILTER (WHERE status = 'failed') AS failed,
              COUNT(*) FILTER (WHERE status = 'ok') AS ok,
              MIN(created_at) FILTER (WHERE status = 'queued') AS oldest_queued_at,
              -- «Ждёт с» — от момента, когда задачу можно было взять: отложенная
              -- на повтор (run_after в будущем) — не застой.
              MIN(GREATEST(created_at, run_after)) FILTER (
                WHERE status = 'queued' AND run_after <= now()
              ) AS oldest_ready_at,
              MAX(last_heartbeat_at) FILTER (WHERE status = 'running') AS last_heartbeat_at,
              MAX(GREATEST(started_at, last_heartbeat_at, finished_at)) AS last_activity_at,
              COUNT(*) FILTER (
                WHERE status = 'running'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < now()
              ) AS expired_leases
"""
# Внешней считается и задача с чужим регионом в очереди external-*: такая ошибка
# маршрута как раз и должна быть видна, а не выпадать из сводки.
_EXTERNAL_JOBS_WHERE = "(execution_region = 'external' OR queue_name LIKE 'external%%')"


def external_queue_status() -> dict:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"SELECT {_EXTERNAL_QUEUE_STATUS_COLUMNS} FROM background_jobs WHERE {_EXTERNAL_JOBS_WHERE}"
        )
        totals = cur.fetchone() or {}
        cur.execute(
            f"""
            SELECT queue_name, {_EXTERNAL_QUEUE_STATUS_COLUMNS}
            FROM background_jobs
            WHERE {_EXTERNAL_JOBS_WHERE}
            GROUP BY queue_name
            ORDER BY queue_name
            """
        )
        queues = cur.fetchall()
    status = {
        "totals": dict(totals),
        "queues": [dict(row) for row in queues],
        **external_consumers_status(),
    }
    status["alerts"] = lanes.lane_alerts(status)
    return status


def external_consumers_status() -> dict:
    """Контракт ядра и воркеры NL с флагом расхождения: для сторожа, экрана и выката NL."""
    try:
        consumers = list_external_consumers()
    except pg_errors.UndefinedTable:
        # Схема ещё без таблицы версий: версий нет, но сторож очередей работать обязан.
        consumers = []
    now = datetime.now(timezone.utc)
    for consumer in consumers:
        consumer["mismatch"] = lanes.consumer_mismatch(consumer, contract.CONTRACT, now=now)
    return {"contract": contract.CONTRACT, "consumers": consumers}


def live_ai_leases() -> list[dict]:
    """ИИ-задачи, которые выкат ядра сейчас оборвал бы: в работе с живой арендой или в записи итога."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT id, kind, queue_name, claimed_by, status, lease_expires_at
            FROM background_jobs
            WHERE queue_name = ANY(%s)
              AND (status = 'finalizing' OR (status = 'running' AND lease_expires_at > now()))
            ORDER BY id
            """,
            (sorted(lanes.AI_LANES),),
        )
        return [dict(row) for row in cur.fetchall()]


def record_external_consumer(worker_id: str, *, queues: list[str], build: str | None, contract_number: int | None) -> None:
    """Запомнить, какую сборку и контракт сообщил контейнер NL при выдаче задачи.

    Пустые очереди claim идут раз в 3-30 с на поток: строку переписываем не чаще раза в
    30 с, если ничего не поменялось, иначе таблица из четырёх строк пухла бы от версий."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO external_worker_consumers (consumer, queues, build, contract)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (consumer) DO UPDATE
            SET queues = EXCLUDED.queues,
                build = EXCLUDED.build,
                contract = EXCLUDED.contract,
                last_seen_at = now()
            WHERE external_worker_consumers.last_seen_at < now() - interval '30 seconds'
               OR (external_worker_consumers.queues, external_worker_consumers.build,
                   external_worker_consumers.contract)
                  IS DISTINCT FROM (EXCLUDED.queues, EXCLUDED.build, EXCLUDED.contract)
            """,
            (contract.consumer_of(worker_id), sorted(queues or []), build, contract_number),
        )
        conn.commit()


def list_external_consumers() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT consumer, queues, build, contract, first_seen_at, last_seen_at
            FROM external_worker_consumers
            ORDER BY consumer
            """
        )
        return [dict(row) for row in cur.fetchall()]


def mark_background_job_running(job_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE background_jobs
            SET status = 'running',
                progress = CASE WHEN progress < 10 THEN 10 ELSE progress END,
                attempts = attempts + 1,
                started_at = COALESCE(started_at, now()),
                error_message = NULL
            WHERE id = %s
            """,
            (job_id,),
        )
        conn.commit()


def update_background_job_progress(job_id: int, progress: float) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET progress = %s WHERE id = %s",
            (progress, job_id),
        )
        conn.commit()


def mark_background_job_ai_started(job_id: int) -> None:
    """Отметить, что локальная AI-обработка сделала первый вызов модели.

    Служит дискриминатором для requeue_stale_background_jobs: задачу с проставленным
    ai_started_at нельзя авто-перезапускать (повторный прогон = повторный расход OpenAI),
    а упавшую ДО первого вызова (ai_started_at IS NULL): можно. progress для этого не
    годится: claim/mark-running форсят его в 10 ещё до тела обработчика, поэтому у любой
    running-задачи progress уже >0 независимо от того, был ли вызов модели."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE background_jobs SET ai_started_at = now() WHERE id = %s",
            (job_id,),
        )
        conn.commit()


def update_external_background_job_progress(
    job_id: int,
    *,
    lease_token_hash: str,
    progress: float,
    lease_seconds: int | None = None,
) -> bool:
    with get_connection() as conn:
        if lease_seconds is None:
            cur = conn.execute(
                """
                UPDATE background_jobs
                SET progress = %s,
                    last_heartbeat_at = now()
                WHERE id = %s
                  AND status = 'running'
                  AND execution_region = 'external'
                  AND lease_token_hash = %s
                  AND lease_expires_at > now()
                """,
                (progress, job_id, lease_token_hash),
            )
        else:
            cur = conn.execute(
                """
                UPDATE background_jobs
                SET progress = %s,
                    last_heartbeat_at = now(),
                    lease_expires_at = now() + (%s::text || ' seconds')::interval
                WHERE id = %s
                  AND status = 'running'
                  AND execution_region = 'external'
                  AND lease_token_hash = %s
                  AND lease_expires_at > now()
                """,
                (progress, lease_seconds, job_id, lease_token_hash),
            )
        conn.commit()
        return bool(cur.rowcount)


def heartbeat_external_background_job(job_id: int, *, lease_token_hash: str, lease_seconds: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET last_heartbeat_at = now(),
                lease_expires_at = now() + (%s::text || ' seconds')::interval
            WHERE id = %s
              AND status = 'running'
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            """,
            (lease_seconds, job_id, lease_token_hash),
        )
        conn.commit()
        return bool(cur.rowcount)


def external_background_job_lease_is_active(job_id: int, *, lease_token_hash: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM background_jobs
            WHERE id = %s
              AND status = 'running'
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            """,
            (job_id, lease_token_hash),
        ).fetchone()
        return row is not None


def finish_background_job(job_id: int, result: dict | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE background_jobs
            SET status = 'ok',
                progress = 100,
                result_json = %s,
                error_message = NULL,
                finished_at = now()
            WHERE id = %s
            """,
            (Json(_jsonable(result or {})), job_id),
        )
        conn.commit()


def begin_external_background_job_finalize(job_id: int, *, lease_token_hash: str) -> bool:
    """Атомарно перевести внешнюю задачу running→finalizing под защитой лиза (баг T2).

    Закрывает окно двойного AI-расхода: применять результат и биллить ai_processing_runs
    можно ТОЛЬКО после успешного перехода в 'finalizing'. Пока задача 'finalizing',
    requeue_expired_external_leases (он трогает лишь status='running') её НЕ переотдаст,
    поэтому другой воркер не прогонит AI повторно. Возвращает True, если лиз ещё валиден
    и задача застолблена именно за этим воркером (его lease_token_hash)."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET status = 'finalizing',
                last_heartbeat_at = now()
            WHERE id = %s
              AND status = 'running'
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            """,
            (job_id, lease_token_hash),
        )
        conn.commit()
        return bool(cur.rowcount)


def release_external_background_job_finalize(job_id: int, *, lease_token_hash: str) -> bool:
    """Откатить finalizing→running, если применение результата упало (баг T2, восстановление).

    Без отката задача залипла бы в 'finalizing' навсегда. После отката её подберёт обычный
    путь восстановления (requeue_expired по истечении лиза / requeue_stale)."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET status = 'running'
            WHERE id = %s
              AND status = 'finalizing'
              AND lease_token_hash = %s
            """,
            (job_id, lease_token_hash),
        )
        conn.commit()
        return bool(cur.rowcount)


def requeue_released_external_job(job_id: int, *, lease_token_hash: str, payload: dict, note: str) -> bool:
    """Воркер вернул задачу сам (мягкая остановка на выкате NL): сразу в очередь.

    Попытка не списывается: остановку устроили мы, а не задача; иначе три выката подряд
    похоронили бы здоровую задачу. payload: то, что осталось сделать (contract.
    remaining_after_partial): сделанная часть уже записана и вычтена, резерв статей снят.
    Ждёт задачу в 'finalizing': ядро застолбило её на время записи частичного итога."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET status = 'queued',
                progress = 0,
                attempts = GREATEST(attempts - 1, 0),
                run_after = now(),
                started_at = NULL,
                claimed_by = NULL,
                lease_token_hash = NULL,
                lease_expires_at = NULL,
                payload_json = %s,
                error_message = %s
            WHERE id = %s
              AND execution_region = 'external'
              AND status = 'finalizing'
              AND lease_token_hash = %s
            """,
            (Json(_jsonable(payload)), note, job_id, lease_token_hash),
        )
        conn.commit()
        return bool(cur.rowcount)


def fail_finalizing_external_job(
    job_id: int,
    *,
    lease_token_hash: str,
    payload: dict,
    error_message: str,
    retryable: bool,
    retry_delay_seconds: int | None = None,
) -> bool:
    """Задача сбоила, но сделанную часть воркер вернул (шаг завис, external_worker.LeaseKeeper).

    Ядро уже записало её и вычло из задачи (contract.remaining_after_partial); дальше: как
    fail_external_background_job: попытка списана, задача в очереди с паузой или failed, если
    попытки кончились. Не как release: зависание может сидеть в самой задаче, и без списания
    она крутилась бы вечно. payload, остаток: и ручной перезапуск failed не позовёт модель за
    записанное. Ждёт задачу в 'finalizing': ядро застолбило её на время записи."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT attempts, max_attempts
            FROM background_jobs
            WHERE id = %s
              AND execution_region = 'external'
              AND status = 'finalizing'
              AND lease_token_hash = %s
            FOR UPDATE
            """,
            (job_id, lease_token_hash),
        )
        row = cur.fetchone()
        if row is None:
            return False
        if retryable and int(row["attempts"] or 0) < int(row["max_attempts"] or 0):
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'queued',
                    progress = 0,
                    run_after = now() + (%s::text || ' seconds')::interval,
                    started_at = NULL,
                    claimed_by = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL,
                    payload_json = %s,
                    error_message = %s
                WHERE id = %s
                """,
                (retry_delay_seconds if retry_delay_seconds is not None else 60, Json(_jsonable(payload)),
                 error_message, job_id),
            )
        else:
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'failed',
                    claimed_by = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL,
                    payload_json = %s,
                    error_message = %s,
                    finished_at = now()
                WHERE id = %s
                """,
                (Json(_jsonable(payload)), error_message, job_id),
            )
        conn.commit()
        return True


def finish_external_background_job(job_id: int, *, lease_token_hash: str, result: dict | None = None) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET status = 'ok',
                progress = 100,
                result_json = %s,
                error_message = NULL,
                claimed_by = NULL,
                lease_token_hash = NULL,
                lease_expires_at = NULL,
                finished_at = now()
            WHERE id = %s
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND (status = 'finalizing'
                   OR (status = 'running' AND lease_expires_at > now()))
            """,
            (Json(_jsonable(result or {})), job_id, lease_token_hash),
        )
        conn.commit()
        return bool(cur.rowcount)


def fail_background_job(job_id: int, error_message: str, *, retry_delay_seconds: int | None = None) -> None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT attempts, max_attempts FROM background_jobs WHERE id = %s",
            (job_id,),
        )
        row = cur.fetchone()
        should_retry = (
            retry_delay_seconds is not None
            and row is not None
            and int(row["attempts"] or 0) < int(row["max_attempts"] or 0)
        )
        if should_retry:
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'queued',
                    progress = 0,
                    run_after = now() + (%s::text || ' seconds')::interval,
                    error_message = %s,
                    started_at = NULL
                WHERE id = %s
                """,
                (retry_delay_seconds, error_message, job_id),
            )
        else:
            conn.execute(
            """
            UPDATE background_jobs
            SET status = 'failed',
                error_message = %s,
                finished_at = now()
            WHERE id = %s
            """,
                (error_message, job_id),
            )
        conn.commit()


def fail_external_background_job(
    job_id: int,
    *,
    lease_token_hash: str,
    error_message: str,
    retryable: bool,
    retry_delay_seconds: int | None = None,
) -> bool:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT attempts, max_attempts
            FROM background_jobs
            WHERE id = %s
              AND status = 'running'
              AND execution_region = 'external'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            """,
            (job_id, lease_token_hash),
        )
        row = cur.fetchone()
        if row is None:
            return False
        should_retry = retryable and int(row["attempts"] or 0) < int(row["max_attempts"] or 0)
        if should_retry:
            delay = retry_delay_seconds if retry_delay_seconds is not None else 60
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'queued',
                    progress = 0,
                    run_after = now() + (%s::text || ' seconds')::interval,
                    error_message = %s,
                    started_at = NULL,
                    claimed_by = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL
                WHERE id = %s
                """,
                (delay, error_message, job_id),
            )
        else:
            conn.execute(
                """
                UPDATE background_jobs
                SET status = 'failed',
                    error_message = %s,
                    claimed_by = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL,
                    finished_at = now()
                WHERE id = %s
                """,
                (error_message, job_id),
            )
        conn.commit()
        return True


def requeue_stale_background_jobs(
    stale_minutes: int, finalizing_stale_minutes: int | None = None
) -> RequeueOutcome:
    """Разобрать локальные running и любые finalizing, зависшие после краша воркера/core.

    Настенные часы по `started_at`: ЗАПАСНАЯ эвристика живости, для задач, у которых
    настоящего протокола живости нет: локальный воркер исполняет задачу в своём процессе
    и, падая, не оставляет о себе никакого сигнала, кроме «висит слишком долго».

    У ВНЕШНЕГО контура протокол есть: lease + heartbeat перед каждой статьёй, и он
    достовернее любого таймаута. Поэтому внешние `running` здесь не трогаются вовсе; ими
    занимается `requeue_expired_external_leases`. Раньше этот фильтр стоял только на ветке
    «пометить failed», а ветка переочереди хватала всё подряд, и комментарий про «внешний
    контур не трогаем» описывал намерение, которого в коде не было. Итог (инцидент 24.07):
    внешний батч длиннее stale_minutes переочередивался вопреки живому, только что
    продлённому lease: воркер получал 409, бросал батч, тут же забирал ту же задачу
    и начинал с нуля. Ровно раз в час, вечно.

    'finalizing' (баг T2): переходный статус на время применения результата внешней задачи;
    он остаётся здесь СОЗНАТЕЛЬНО. В нём работает уже не воркер, а core, heartbeat не идёт,
    и признак жизни тут действительно временной: `last_heartbeat_at` ставится при входе
    в finalize, а сам apply обязан занимать секунды. Отдельный короткий таймаут
    (`finalizing_stale_minutes`): не «как обычный running».
    """
    if finalizing_stale_minutes is None:
        finalizing_stale_minutes = stale_minutes
    with get_connection() as conn:
        # ЛОКАЛЬНУЮ AI-обработку, которая уже НАЧАЛА жечь OpenAI, в очередь НЕ возвращаем:
        # повторный прогон это повторный РЕАЛЬНЫЙ расход. requeue переиспользует тот же
        # job_id, а get_articles_by_ids не пропускает уже обработанные статьи, поэтому модель
        # вызвалась бы заново. Дискриминатор: ai_started_at (ставится строго перед первым
        # вызовом модели). progress тут НЕ годится: claim/mark-running форсят его в 10 ещё до
        # тела обработчика, так что по progress «до/после AI» не отличить: и задача, упавшая
        # ДО первого вызова ($0), ложно попадала бы под failed вместо авто-восстановления.
        # NB: дедуп биллинга по (job_id, article_id, stage) здесь НЕ решение: он бы просто
        # СПРЯТАЛ второй, реально оплаченный вызов из отчёта о стоимости (к тому же
        # fail_background_job умеет ретраить ту же задачу, пока attempts < max_attempts).
        # Помечаем failed без авто-ретрая: пусть решает человек.
        conn.execute(
            """
            UPDATE background_jobs
            SET status = 'failed',
                error_message = 'Зависла после начала AI-обработки. Не перезапускаем '
                                'автоматически, чтобы не оплатить OpenAI дважды — '
                                'проверьте результат и при необходимости запустите заново.'
            WHERE status = 'running'
              AND started_at < now() - (%s::text || ' minutes')::interval
              AND kind = 'process_articles'
              AND execution_region IS DISTINCT FROM 'external'
              AND ai_started_at IS NOT NULL
            """,
            (stale_minutes,),
        )
        outcome = _requeue_or_exhaust(
            conn,
            lost_where="""
                (status = 'running'
                 AND execution_region IS DISTINCT FROM 'external'
                 AND started_at < now() - (%s::text || ' minutes')::interval)
                OR (status = 'finalizing'
                    AND last_heartbeat_at < now() - (%s::text || ' minutes')::interval)
            """,
            lost_params=(stale_minutes, finalizing_stale_minutes),
            reason="Requeued after stale running/finalizing timeout",
        )
        conn.commit()
        return outcome


def count_stale_running_background_jobs(stale_minutes: int) -> int:
    with get_connection() as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM background_jobs
                WHERE status = 'running'
                  AND started_at < now() - (%s::text || ' minutes')::interval
                """,
                (stale_minutes,),
            ).fetchone()[0]
        )


def cleanup_finished_background_jobs(retention_days: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            DELETE FROM background_jobs
            WHERE status IN ('ok', 'failed')
              AND COALESCE(finished_at, started_at, created_at)
                  < now() - (%s::text || ' days')::interval
            """,
            (retention_days,),
        )
        conn.commit()
        return cur.rowcount or 0


def count_finished_background_jobs_eligible_for_cleanup(retention_days: int) -> int:
    with get_connection() as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM background_jobs
                WHERE status IN ('ok', 'failed')
                  AND COALESCE(finished_at, started_at, created_at)
                      < now() - (%s::text || ' days')::interval
                """,
                (retention_days,),
            ).fetchone()[0]
        )


def cleanup_finished_export_jobs(retention_days: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            DELETE FROM export_jobs
            WHERE status IN ('ok', 'failed')
              AND COALESCE(finished_at, started_at)
                  < now() - (%s::text || ' days')::interval
            """,
            (retention_days,),
        )
        conn.commit()
        return cur.rowcount or 0


def count_finished_export_jobs_eligible_for_cleanup(retention_days: int) -> int:
    with get_connection() as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM export_jobs
                WHERE status IN ('ok', 'failed')
                  AND COALESCE(finished_at, started_at)
                      < now() - (%s::text || ' days')::interval
                """,
                (retention_days,),
            ).fetchone()[0]
        )


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


# ---------------------------------------------------------------------------
#  articles
# ---------------------------------------------------------------------------

class InsertVerdict(NamedTuple):
    """Чем кончится insert_article для записи. `holder`: статья, в которую запись
    упёрлась: {id, url, title, source_id, hidden, position}; у записи этого же прогона
    пробы id нет (None)."""

    verdict: str
    url_key: str
    body_hash: str | None
    holder: dict | None = None


_HOLDER_COLUMNS = "id, url, title, source_id, pending_deletion"


def insert_verdict(conn, rec: dict, pending: Sequence[dict] = ()) -> InsertVerdict:
    """Рубежи insert_article по записи: единственная их реализация. Только SELECT.

    insert_article зовёт её перед INSERT; проба источника (`source-probe`): вместо
    вставки, в соединении только для чтения. Второй копии рубежей нет, поэтому вердикт
    пробы это ровно то, что сделал бы сбор.

    `pending`: записи, которые проба «вставила бы» раньше в том же прогоне. Сбор их
    вставляет по-настоящему, и следующая запись прогона упирается в них так же, как в
    строки базы: у Eni 25.09 с каждой страницы извлекался один текст виджета: первая
    статья легла бы, остальные отбились бы по телу. Сбор `pending` не передаёт.

    Порядок рубежей, порядок вставки: ключ адреса среди видимых статей → то же тело у
    видимой статьи того же источника → тот же адрес у любой строки (его держит
    уникальный индекс по url, ON CONFLICT (url) в insert_article).

    Та же ли статья заняла ключ (SAME/OTHER), решает не здесь, а проба: вставке это не
    нужно, а разбор адреса на кривом URL падал бы и ронял сбор источника.
    """
    url = rec.get("url") or ""
    key = normalize.url_key(url)
    body_hash = normalize.compute_body_hash(rec.get("raw_text"))
    source_id = rec.get("source_id")
    earlier = [(p, normalize.url_key(p.get("url") or ""), normalize.compute_body_hash(p.get("raw_text")))
               for p in pending]
    if key:
        holder = _holder(conn.execute(
            f"SELECT {_HOLDER_COLUMNS} FROM articles WHERE url_key = %s AND NOT pending_deletion LIMIT 1",
            (key,),
        ).fetchone()) or next((_pending_holder(p) for p, p_key, _ in earlier if p_key == key), None)
        if holder is not None:
            return InsertVerdict(verdicts.DUP_URL_KEY, key, body_hash, holder)
    # Третий рубеж: одинаковое ТЕЛО у того же источника. Такая же проверка уже
    # стояла в дозагрузке (article_fetcher), но только на замену текста: на
    # первичной вставке её не было, и брак заезжал свободно.
    #
    # Замер прода 17.09: 830 статей с повторяющимся телом, 311 у активных
    # источников, за 134 уже заплачен ИИ. У Новатэка так набралось 82 «статьи»
    # из 86 это оказались страницы навигации сайта (/ru/esg, /ru/press/
    # calculator, даже PDF политики конфиденциальности): парсер берёт из
    # листинга все ссылки подряд, включая меню, а сайт отдаёт на них одну и ту
    # же оболочку. Разные статьи одного источника не совпадают телом побайтово,
    # поэтому проверка безопасна. Перепечатки между источниками НЕ трогаем:
    # это задача №21, и там нужен семантический дедуп, а не хэш.
    if body_hash and source_id is not None:
        # NOT pending_deletion: как на соседнем рубеже по url_key. Скрытая копия
        # иначе блокировала бы пересбор навсегда: у RSS телом на вставке служит
        # summary ленты, и один постоянный тизер-заглушка отрезал бы источник
        # целиком после первой же статьи.
        holder = _holder(conn.execute(
            f"SELECT {_HOLDER_COLUMNS} FROM articles WHERE source_id = %s AND body_hash = %s "
            "AND NOT pending_deletion LIMIT 1",
            (int(source_id), body_hash),
        ).fetchone()) or next((_pending_holder(p) for p, _, p_body in earlier
                               if p_body == body_hash and p.get("source_id") == source_id), None)
        if holder is not None:
            return InsertVerdict(verdicts.DUP_BODY_HASH, key, body_hash, holder)
    # Тот же адрес, у любой строки, в том числе скрытой: его держит уникальный индекс по url.
    holder = _holder(conn.execute(
        f"SELECT {_HOLDER_COLUMNS} FROM articles WHERE url = %s LIMIT 1", (url,),
    ).fetchone()) or next((_pending_holder(p) for p, _, _ in earlier if (p.get("url") or "") == url), None)
    if holder is not None:
        return InsertVerdict(verdicts.KNOWN, key, body_hash, holder)
    return InsertVerdict(verdicts.WOULD_INSERT, key, body_hash)


def _holder(row) -> dict | None:
    if row is None:
        return None
    return {"id": int(row[0]), "url": row[1], "title": row[2], "source_id": row[3],
            "hidden": bool(row[4]), "position": None}


def _pending_holder(rec: dict) -> dict:
    return {"id": None, "url": rec.get("url") or "", "title": rec.get("title") or "",
            "source_id": rec.get("source_id"), "hidden": False, "position": None}


def articles_by_urls(conn, urls: Sequence[str]) -> dict[str, dict]:
    """Статьи по точным адресам (и скрытые): кто держит адрес, для отчёта пробы."""
    if not urls:
        return {}
    rows = conn.execute(f"SELECT {_HOLDER_COLUMNS} FROM articles WHERE url = ANY(%s)", (list(urls),)).fetchall()
    return {row[1]: _holder(row) for row in rows}


def insert_article(rec: dict) -> bool:
    """Вставить статью. Дубликаты игнорируются. Возвращает True, если строка вставлена.

    Тождество статьи: по НОРМАЛИЗОВАННОМУ адресу (`normalize.url_key`), а не по сырому
    url. Замер прода 13.09: на сыром url за 90 дней накопилось 940 лишних статей, и
    причины ровно три: query-хвосты (`?from=main_lines_11` против `?from=newsfeed` у
    РБК), схема (`http://` против `https://` у Ростеха) и хвостовой слэш (Wood Mackenzie).
    Каждая копия проходила полный ИИ-конвейер заново и занимала отдельную карточку в
    ленте: заказчик 08.09 прислал скрин «все 4 новости об одном».

    ДВА ON CONFLICT нужны оба: по `url`: исторический уникальный индекс, он никуда не
    делся; по `url_key`: новый частичный (скрытые копии его не занимают). Пишем первым
    попавшийся конфликт, поэтому проверку по ключу делаем явным запросом до вставки:
    ON CONFLICT умеет целиться только в один индекс за раз.

    Решение «вставлять или нет»: insert_verdict (рубежи в одном месте, их же читает
    проба источника); ON CONFLICT (url) остаётся страховкой от гонки двух вставок.

    Новая статья идёт через «Отбор» (#130, articles.selector = 'otbor'; флаг снят 08.11, гейта ленты больше нет):
    пока «Отбор» не решил, лента её не показывает.
    """
    with get_connection() as conn:
        verdict = insert_verdict(conn, rec)
        if verdict.verdict == verdicts.DUP_BODY_HASH and verdict.holder["url"] != rec.get("url"):
            # Отказ этого рубежа в сводке сбора сливался с «дублями» (~3000 за цикл) и
            # был немым: 25.09 у Eni так молча отбивалась каждая новая статья: с каждой
            # страницы извлекался один и тот же текст виджета чат-бота.
            logger.warning("insert_article: у %s то же тело, что у статьи %s (%s) того же "
                           "источника — не вставлено: либо копия той же статьи по другому "
                           "адресу, либо извлекается общий блок страницы, а не статья",
                           rec.get("url"), verdict.holder["id"], verdict.holder["url"])
        if verdict.verdict != verdicts.WOULD_INSERT:
            return False
        # После рубежей: их проходит только новая статья, а кандидатов за цикл тысячи.
        kept, tail = article_tail_store.split_for_write(
            conn, rec.get("source_id"), rec.get("title") or "", rec.get("raw_text"))
        rec = {**rec, "image_url": rec.get("image_url"),
               "body_hash": verdict.body_hash, "url_key": verdict.url_key,
               "selector": ARTICLE_SELECTOR_OTBOR,
               "raw_text": kept, "text_tail": tail}
        cur = conn.execute(
            """
            INSERT INTO articles (source_id, title, url, url_key, published_at,
                                  raw_text, text_tail, text_truncated, language, content_hash, image_url,
                                  body_hash, selector)
            VALUES (%(source_id)s, %(title)s, %(url)s, %(url_key)s, %(published_at)s,
                    %(raw_text)s, %(text_tail)s, COALESCE(%(text_truncated)s, FALSE), %(language)s,
                    %(content_hash)s, %(image_url)s, %(body_hash)s, %(selector)s)
            ON CONFLICT (url) DO NOTHING
            RETURNING id
            """,
            rec,
        )
        row = cur.fetchone()
        conn.commit()
    return row is not None


def body_hash_belongs_to_other_article(source_id: int, body_hash: str | None,
                                       exclude_article_id: int | None = None) -> bool:
    """Есть ли у ЭТОГО источника другая статья с таким же телом (задача №24).

    Один и тот же текст у разных статей источника, почти всегда подмена: сайт отдал
    листинг/пейвол/заглушку вместо статьи. Проверка идёт в паре (source_id, body_hash),
    под неё есть составной индекс. Кросс-источниковые совпадения НЕ считаем подменой:
    это перепечатки, ими занимается дедуп (№21).
    """
    if not body_hash:
        return False
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT 1 FROM articles
            WHERE source_id = %s AND body_hash = %s
              AND (%s::bigint IS NULL OR id <> %s::bigint)
            LIMIT 1
            """,
            (source_id, body_hash, exclude_article_id, exclude_article_id),
        )
        return cur.fetchone() is not None


def get_articles_missing_image(limit: int = 200) -> list[dict]:
    """Статьи без картинки (image_url пуст), для бэкфилла og:image в дайджест.
    fetch-full-text трогает только статьи без полного текста, поэтому уже
    обработанные статьи остаются без image_url, и их добирает эта выборка.

    СКРЫТЫЕ И ОТКЛОНЁННЫЕ ИСКЛЮЧЕНЫ. Бэкфилл это РЕАЛЬНЫЙ поход в интернет за
    каждой статьёй (медленный, с ретраями), а картинка нужна только тому, что
    показывается. Замер 24.07: из 4675 статей без картинки 2085 (45%) были скрыты
    (pending_deletion) или отклонены гейтом: почти половина запросов уходила
    впустую. Условие то же, что у ленты (feed_window.selected_sql): ждущую «Отбора» статью выборка не берёт."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id, a.url
            FROM articles a
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE a.url IS NOT NULL AND COALESCE(a.image_url, '') = ''
              AND NOT a.pending_deletion
              AND {feed_window.selected_sql('a', 'c')}
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def set_article_image(article_id: int, image_url: str) -> bool:
    """Проставить image_url, только если его ещё нет. True, если обновлено."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE articles SET image_url = %s, updated_at = now() "
            "WHERE id = %s AND COALESCE(image_url, '') = ''",
            (image_url, article_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_articles_needing_full_text(limit: int = 50, retry_too_short: bool = False) -> list[dict]:
    """Articles whose RSS body is likely only a teaser and needs URL extraction.

    retry_too_short=True also includes articles previously marked too_short so they
    can be re-attempted (e.g. after trafilatura is added to the extraction chain).

    Сюда же берём no_gain: этот статус выделен из too_short позже, и в базе с
    прежних прогонов лежат строки, где «нет прироста» записано как too_short.
    Если брать только одно из двух значений, часть статей молча выпадет из
    повтора: поэтому список, а не равенство.
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        status_filter = (
            "AND (a.full_text_status IS NULL OR a.full_text_status IN ('too_short', 'no_gain'))"
            if retry_too_short
            else "AND a.full_text_status IS NULL"
        )
        cur.execute(
            f"""
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.url IS NOT NULL
              -- Источники зарубежного контура пропускаем: они там именно потому, что
              -- с РФ-адреса закрыты, и локальная дозагрузка ловит на них 403
              -- гарантированно. Попытка ОДНА и навсегда — статья получила бы
              -- full_text_status='failed' и больше никогда не переспрашивалась, даже
              -- когда тело уже добрал зарубежный воркер (external_fetch).
              AND COALESCE(s.network_region, 'auto') <> 'external'
              {status_filter}
              AND (
                COALESCE(a.text_truncated, FALSE) = TRUE
                OR length(COALESCE(a.raw_text, '')) < %s
              )
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (config.MIN_FULL_TEXT_CHARS, limit),
        )
        return cur.fetchall()


def update_article_full_text(article_id: int, raw_text: str | None, text_truncated: bool,
                             status: str, method: str, error: str | None = None,
                             image_url: str | None = None) -> str | None:
    """Store full-text extraction result without losing the RSS teaser on failure.

    Возвращает текст, который лёг в базу: от пришедшего отрезан хвост, повторяющийся у других статей
    источника (#140). None: текст не менялся."""
    kept = None
    with get_connection() as conn:
        if image_url:
            # Заполняем картинку, только если её ещё нет: RSS-media в приоритете.
            conn.execute(
                "UPDATE articles SET image_url = %s "
                "WHERE id = %s AND COALESCE(image_url, '') = ''",
                (image_url, article_id),
            )
        if raw_text is not None:
            kept, tail = article_tail_store.split_article_text(conn, article_id, raw_text)
            conn.execute(
                """
                UPDATE articles
                SET raw_text = %s,
                    text_tail = %s,
                    body_hash = %s,
                    text_truncated = %s,
                    full_text_fetched_at = now(),
                    full_text_status = %s,
                    full_text_error = %s,
                    extraction_method = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (kept, tail, normalize.compute_body_hash(raw_text), text_truncated,
                 status, error, method, article_id),
            )
        else:
            conn.execute(
                """
                UPDATE articles
                SET text_truncated = %s,
                    full_text_fetched_at = now(),
                    full_text_status = %s,
                    full_text_error = %s,
                    extraction_method = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (text_truncated, status, error, method, article_id),
            )
        conn.commit()
    return kept


# ---------------------------------------------------------------------------
#  Диагностика (для stats)
# ---------------------------------------------------------------------------

def count_sources() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]


def count_articles() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]


def dashboard_stats(window: FeedWindow | None = None) -> dict:
    """Счётчики над лентой: по той же выборке, что и сама лента.

    Считаются по базе, а не по загруженной странице, поэтому не зависят от того, сколько
    строк забрал фронт. Выборка: базовая видимость ленты (feed_window.visible_sql) в окне
    месяца, так что ``total_articles`` («сигналы», его читает «N из M» над лентой) равно
    выборке ленты до скрытий отметками: помеченное «шум/дубликат/архив» лента по
    умолчанию прячет, а здесь оно входит в M и в плитки статусов (вкладка «Со статусом»
    его показывает). До 23.09 здесь было своё условие: оно считало
    перепечатки и статьи архивных источников, но не видело свежих статей без карточки:
    и над лентой из 1 934 статей висело 2 205 «сигналов».
    ``avg_score``: среднее только по оценённым. ``selected_for_digest`` и статусы: по
    общим отметкам (#111). ``window``: окно месяца; без окна: вся база (замеры, benchmarks).
    Тема и балл берутся у публикации ленты (publication_joins): у сигнала ленты, у статьи без него у самой статьи.
    """
    win = window.sql("a") if window is not None else "TRUE"
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        # Один проход по выборке ленты с разбивкой по отметке: из него же
        # складываются «сигналы», «обработано», «в дайджест» и плитки статусов. Раньше это
        # были шесть подзапросов, и у каждого своя видимость.
        cur.execute(
            f"""
            SELECT {marks.article_status_sql()} AS status,
                   COUNT(*) AS cnt,
                   COUNT(*) FILTER (WHERE COALESCE(c.summary, '') <> '') AS with_summary,
                   -- Обработано: есть суть, проверена релевантность и у публикации есть тема или балл (тема у
                   -- сигнала ленты обязательна, у статьи без сигнала это тег или балл статьи, как раньше).
                   COUNT(*) FILTER (
                     WHERE COALESCE(c.summary, '') <> ''
                       AND {feed_window.relevance_sql('a', 'c')} IS NOT NULL
                       AND (pub.theme IS NOT NULL OR pub.total_score IS NOT NULL)
                   ) AS processed,
                   SUM(pub.total_score) AS score_sum,
                   COUNT(pub.total_score) AS scored
              FROM articles a
              JOIN sources s ON s.id = a.source_id
              LEFT JOIN article_cards c ON c.article_id = a.id
              {publication_joins('a')}
              {marks.article_mark_join()}
             WHERE {visible_sql()}
               AND {win}
             GROUP BY 1
            """
        )
        by_status = cur.fetchall()
        cur.execute(
            f"""
            SELECT
              -- «Всего собрано» (первая плитка) — ВЕСЬ объём за окно (решение владельца 25.07):
              -- и отсев гейтом, и вычищенное перепроверкой. Намеренно шире выборки ленты.
              (SELECT COUNT(*) FROM articles a WHERE {win}) AS all_articles,
              (SELECT COUNT(*) FROM articles a WHERE a.pending_deletion AND {win}) AS cleaned_articles,
              (SELECT COUNT(*) FROM sources) AS sources
            """
        )
        totals = cur.fetchone()

    status_counts = {str(r["status"]): int(r["cnt"] or 0) for r in by_status}
    scored = sum(int(r["scored"] or 0) for r in by_status)
    score_sum = sum(Decimal(str(r["score_sum"] or 0)) for r in by_status)
    # Как SQL ROUND у прежнего AVG: половина: вверх, а не банковское округление Python.
    avg_score = int((score_sum / scored).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if scored else 0
    return {
        "total_articles": sum(status_counts.values()),
        # Весь объём за окно: только под первую плитку, отдельно от «сигналов».
        "all_articles": int(totals["all_articles"] or 0),
        "with_summary": sum(int(r["with_summary"] or 0) for r in by_status),
        "processed_articles": sum(int(r["processed"] or 0) for r in by_status),
        # Терялось: SQL считал cleaned_articles, а возврат собирается вручную и поле
        # в него не попадало → на фронте плитка «Почищено» всегда показывала 0.
        "cleaned_articles": int(totals["cleaned_articles"] or 0),
        "selected_for_digest": status_counts.get("digest", 0),
        "avg_score": avg_score,
        "sources": int(totals["sources"] or 0),
        "status_counts": {
            status: status_counts.get(status, 0)
            for status in ARTICLE_STATUS_VALUES
        },
    }


def article_period_months(article_ids: list[int]) -> set[str]:
    """Месяцы периода («ГГГГ-ММ») у этих статей: тем же выражением, что у окна ленты."""
    if not article_ids:
        return set()
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {period_month_sql('a')} FROM articles a WHERE a.id = ANY(%s)",
            (list(article_ids),),
        ).fetchall()
    return {row[0] for row in rows}


def closed_month_article_ids(article_ids: list[int], window: FeedWindow) -> list[int]:
    """Статьи из списка, чей месяц периода окно ленты уже закрыло (архив: только просмотр)."""
    if not article_ids:
        return []
    with get_connection() as conn:
        return _closed_month_article_ids(conn, article_ids, window)


def _closed_month_article_ids(conn, article_ids: list[int], window: FeedWindow) -> list[int]:
    rows = conn.execute(
        f"SELECT a.id FROM articles a WHERE a.id = ANY(%s)"
        f" AND {window.closed_sql(period_month_sql('a'))} ORDER BY a.id",
        ([int(article_id) for article_id in article_ids],),
    ).fetchall()
    return [int(row[0]) for row in rows]


def feed_archive_months(window: FeedWindow) -> list[dict]:
    """Прошлые месяцы для переключателя «Архив»: сколько в месяце статей и сколько из них
    выбрано «в дайджест» (общая отметка) (по второму числу конструктор выпуска строит
    список прошлых выпусков: только просмотр и выгрузка).

    Месяц: тот же, что у ленты и сборщика выпуска (feed_window.period_month_sql), выборка:
    та же, что у ленты и её счётчиков (feed_window.visible_sql): число у месяца равно
    «сигналам» над лентой этого месяца. Скрытия отметками и порог балла не учитываются:
    число показывает объём месяца, а не текущий фильтр.

    В выпуске идут и карточки радара, выбранные «в дайджест» (ревью 24.10): «digest» считает их
    теми же условиями, что сборщик (_issue_signal_sql, signal_month_sql), а месяц только из
    карточек радара тоже есть в списке: иначе такой прошлый выпуск было бы не открыть.

    «digest»: материалы выпуска, тот же состав, что у выгрузки (issue_composition_by_rule, LOW-4 ревью PR #155):
    статьи черновика месяца, которые до сих пор отмечены, а нет таких, все отмеченные статьи месяца; статья, убранная
    из черновика кнопкой «Из выпуска», в число не идёт.
    """
    month_expr = period_month_sql("a")
    in_issue = _issue_article_sql(marks.article_marked_sql("a.id", "digest"))
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT {month_expr} AS month,
                   COUNT(*) AS articles,
                   COUNT(*) FILTER (WHERE {in_issue}) AS marked,
                   COUNT(*) FILTER (WHERE {in_issue} AND EXISTS (
                     SELECT 1 FROM monthly_digests d JOIN monthly_digest_items i ON i.digest_id = d.id
                     WHERE d.user_id IS NULL AND d.month = {month_expr} AND i.article_id = a.id)) AS in_draft
              FROM articles a
              JOIN sources s ON s.id = a.source_id
              LEFT JOIN article_cards c ON c.article_id = a.id
             WHERE {visible_sql()}
               AND {month_expr} < %s
             GROUP BY 1
            """,
            (window.open_months[0],),
        )
        months = {row["month"]: {"month": row["month"], "articles": int(row["articles"]),
                                 "digest": int(row["in_draft"] or row["marked"])}
                  for row in cur.fetchall()}
        cur.execute(
            f"""
            SELECT {signal_month_sql("sig")} AS month, COUNT(*) AS digest
              FROM signals sig
             WHERE {_issue_signal_member_sql(marks.signal_marked_sql("sig.id", "digest"))}
               AND {signal_month_sql("sig")} < %s
             GROUP BY 1
            """,
            (window.open_months[0],),
        )
        for row in cur.fetchall():
            entry = months.setdefault(row["month"], {"month": row["month"], "articles": 0, "digest": 0})
            entry["digest"] += int(row["digest"])
        return sorted(months.values(), key=lambda item: item["month"], reverse=True)


def monthly_platform_stats(months: int = 6) -> list[dict]:
    """Месячная воронка платформы: собрано → принято («Отбором», до него гейтом) → обработано → скрыто.

    Глобальная (не пер-юзерная) метрика: это результат работы САМОЙ платформы,
    одинаковый для всех. Пер-юзерная активность считается отдельно:
    monthly_user_activity(), у неё свой скоуп по владельцу.
    Месяц берём по collected_at (когда МЫ собрали), а не по published_at:
    иначе старые статьи из архивных лент искажают динамику текущей работы.
    Балл берётся у публикации ленты (publication_joins): «оценено», средний балл и «годных» считают и статьи «Отбора»,
    у которых балла статьи нет. Порог «годных» прежний, 60: так его подписывает экран «Статистика».
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT to_char(a.collected_at, 'YYYY-MM') AS month,
                   count(*) AS collected,
                   count(*) FILTER (WHERE rel.relevant IS TRUE) AS relevant,
                   count(*) FILTER (WHERE rel.relevant IS FALSE) AS rejected,
                   count(*) FILTER (WHERE a.pending_deletion) AS hidden,
                   count(*) FILTER (WHERE COALESCE(c.summary, '') <> '') AS summarized,
                   count(*) FILTER (WHERE pub.total_score IS NOT NULL) AS scored,
                   round(avg(pub.total_score)) AS avg_score,
                   count(*) FILTER (WHERE pub.total_score >= 60) AS digest_ready
            FROM articles a
            LEFT JOIN article_cards c ON c.article_id = a.id
            {publication_joins('a')}
            LEFT JOIN LATERAL (SELECT {feed_window.relevance_sql('a', 'c')} AS relevant) rel ON TRUE
            WHERE a.collected_at >= date_trunc('month', now()) - make_interval(months => %s)
            GROUP BY 1
            ORDER BY 1
            """,
            (months,),
        )
        return cur.fetchall()


def monthly_ai_cost(months: int = 6) -> list[dict]:
    """Стоимость ИИ по месяцам и моделям.

    Считается по ai_processing_runs.cost_usd. ВАЖНО: до фикса 23.07 (T10) сюда не
    попадали вызовы гейта по ОТКЛОНЁННЫМ статьям: исторические месяцы занижены.
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT to_char(created_at, 'YYYY-MM') AS month,
                   model,
                   -- Радар: отдельной строкой от ленты той же модели (стадии radar_*), агенты
                   -- «Карточка сигнала» (#96), «Отбор» (#130), баллы «Карточки» (#132) и разбор ОС (#189): своими.
                   CASE WHEN stage LIKE 'radar\\_%%' THEN 'radar'
                        WHEN stage IN ('signal_card', 'otbor', 'signal_scores', 'feedback_review') THEN stage
                        ELSE 'feed' END AS area,
                   count(*) AS runs,
                   round(sum(cost_usd)::numeric, 2) AS cost_usd
            FROM ai_processing_runs
            WHERE created_at >= date_trunc('month', now()) - make_interval(months => %s)
            GROUP BY 1, 2, 3
            ORDER BY 1, 5 DESC
            """,
            (months,),
        )
        return cur.fetchall()


def monthly_user_activity(months: int = 6, user_id: int | None = None) -> list[dict]:
    """Разметка пользователей по месяцам: кто сколько чего пометил.

    Читает архив личных отметок (#111): после переноса в общие отметки новые месяцы здесь пусты,     статистика по журналу и за прежние месяцы по архиву, #112.

    СКОУП: user_id=None: все пользователи (только для админа, решается в api).
    Передан user_id: строго его собственная активность. Fail-closed не нужен:
    вызывающий обязан передать user_id для не-админа (см. api.monthly_stats).
    Месяц: по дате статьи, чтобы «разметка за июль» означала июльские новости.
    """
    params: list = [months]
    user_clause = ""
    if user_id is not None:
        user_clause = "AND s.user_id = %s"
        params.append(user_id)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT to_char(COALESCE(a.published_at, a.collected_at), 'YYYY-MM') AS month,
                   s.user_id,
                   u.email,
                   s.status,
                   count(*) AS marks
            FROM user_article_states s
            JOIN users u ON u.id = s.user_id
            JOIN articles a ON a.id = s.article_id
            WHERE COALESCE(a.published_at, a.collected_at)
                  >= date_trunc('month', now()) - make_interval(months => %s)
              {user_clause}
            GROUP BY 1, 2, 3, 4
            ORDER BY 1 DESC, 5 DESC
            """,
            params,
        )
        return cur.fetchall()


def clear_future_published_dates(tolerance_days: int = 2) -> int:
    """Обнулить недостоверные даты публикации из будущего (анонсы-события календаря).

    Статья сохраняется: убирается только ошибочная дата, после чего она
    сортируется/показывается по реальному `collected_at` и перестаёт помечаться
    как «дата в будущем». Идемпотентно. Возвращает число затронутых строк.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE articles SET published_at = NULL, updated_at = now() "
            "WHERE published_at > now() + make_interval(days => %s)",
            (tolerance_days,),
        )
        conn.commit()
        return cur.rowcount


def sources_by_strategy() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT parse_strategy, COUNT(*) AS n FROM sources "
            "GROUP BY parse_strategy ORDER BY n DESC"
        )
        return cur.fetchall()


SOURCE_HEALTH_VERDICTS = ("no_articles", "stale", "ok", "disabled", "archived")


def source_health_report(stale_days: int | None = None, limit: int = 300, verdict: str | None = None) -> list[dict]:
    """Per-source article coverage verdict for operations diagnostics.

    stale («Требует внимания»): последняя загрузка stale_days и больше календарных дней
    назад по Москве, как «N дн. назад» в колонке «Последняя загрузка» (sourceUtils.ts):
    при 7 «7 дн. назад» уже требует внимания, «6 дн. назад»: ещё нет. Скользящие
    7 × 24 ч с колонкой расходились до суток. Сегодня по МСК: с часов окна ленты
    (feed_window._now), тесты их замораживают. Без явного порога:
    config.SOURCE_STALE_DAYS, одно правило для экрана, API, CLI и замеров.

    Архивный источник, отдельный вердикт 'archived', а не 'disabled': архив выключает
    сбор (enabled = FALSE), и раньше он попадал в «Выкл». Экран источников считал
    плитки по этому отчёту вместе с архивом, а список и счётчик в шапке, без него:
    19.09 заказчик видел на одном экране «133 источника» и «173» в каталоге.
    """
    if stale_days is None:
        stale_days = config.SOURCE_STALE_DAYS
    today = feed_window.current().today
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            WITH src AS (
              SELECT s.id, s.name, s.enabled, s.parse_strategy, s.source_type,
                     s.url, s.rss_url, s.listing_url, s.archived_at,
                     COUNT(a.id) AS articles,
                     COUNT(a.id) FILTER (WHERE a.collected_at >= now() - interval '30 days') AS articles_30d,
                     MAX(a.collected_at) AS last_article_at
              FROM sources s
              LEFT JOIN articles a ON a.source_id = s.id
              GROUP BY s.id
            ),
            verdicts AS (
              SELECT *,
                     CASE
                       WHEN archived_at IS NOT NULL THEN 'archived'
                       WHEN NOT enabled THEN 'disabled'
                       WHEN articles = 0 THEN 'no_articles'
                       WHEN %s::date - (last_article_at AT TIME ZONE '{feed_window.MSK.key}')::date >= %s
                         THEN 'stale'
                       ELSE 'ok'
                     END AS verdict
              FROM src
            )
            SELECT *
            FROM verdicts
            WHERE (%s::text IS NULL OR verdict = %s)
            ORDER BY
              CASE verdict
                WHEN 'no_articles' THEN 1
                WHEN 'stale' THEN 2
                WHEN 'ok' THEN 3
                WHEN 'disabled' THEN 4
                ELSE 5
              END,
              articles ASC,
              last_article_at NULLS FIRST,
              name
            LIMIT %s
            """,
            (today, stale_days, verdict, verdict, limit),
        )
        return cur.fetchall()


def top_sources(limit: int = 10) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT s.name, COUNT(a.id) AS n FROM sources s "
            "JOIN articles a ON a.source_id = s.id "
            "GROUP BY s.id, s.name ORDER BY n DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def cross_dup_candidates() -> int:
    """Сколько content_hash встречается более чем у одного URL (кандидаты-перепечатки)."""
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT content_hash FROM articles "
            "  WHERE content_hash IS NOT NULL "
            "  GROUP BY content_hash HAVING COUNT(DISTINCT url) > 1"
            ") t"
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
#  AI processing: articles, cards, tags, scoring, metrics
# ---------------------------------------------------------------------------

def external_refetch_candidates(limit: int = 200) -> list[dict]:
    """Статьи-обрывки у источников зарубежного контура: кандидаты на дозаполнение.

    Локальная дозагрузка их не берёт намеренно (см. get_articles_needing_full_text):
    с РФ-адреса эти сайты отдают 403, а попытка одна и навсегда. Тело для них может
    добрать только внешний воркер, и до 17.09 такого пути не было вовсе: замер
    показал 25 из 25 статей короче 600 знаков у Oil & Gas Journal и Offshore Magazine.

    Берём и уже помеченные failed: прежние пометки ставились локальной дозагрузкой,
    то есть отражают недоступность с РФ, а не непригодность статьи.
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.id, a.url, a.title
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.url IS NOT NULL
              AND s.network_region = 'external'
              AND s.archived_at IS NULL
              AND (
                a.full_text_status IS NULL
                -- Повтор после неудачи — но НЕ каждый цикл. Статусы failed/too_short
                -- ставит сама же внешняя попытка, поэтому без паузы статья
                -- переочередивалась бы вечно: в июле так задача 1181 крутилась в
                -- петле по часу и жгла деньги. Даём сутки на остыть.
                OR (a.full_text_status IN ('failed', 'too_short')
                    AND (a.full_text_fetched_at IS NULL
                         OR a.full_text_fetched_at < now() - interval '1 day'))
              )
              AND length(COALESCE(a.raw_text, '')) < %s
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (config.MIN_FULL_TEXT_CHARS, limit),
        )
        return cur.fetchall()


def get_articles_for_external_refetch(article_ids: list[int]) -> list[dict]:
    """Адреса и заголовки по списку id: то, что уезжает на воркер без базы."""
    if not article_ids:
        return []
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, url, title FROM articles WHERE id = ANY(%s) AND url IS NOT NULL",
            (list(article_ids),),
        )
        return cur.fetchall()


def reprint_candidates(*, days: int = 14, min_overlap: float = 0.35,
                       max_overlap: float = 1.0,
                       max_days_apart: int = 5, limit: int = 200,
                       ru_min_overlap: float | None = None) -> list[dict]:
    """Пары-кандидаты в перепечатки: разные источники, близкие даты, общие слова.

    Правило намеренно широкое: оно отвечает за полноту, а решает модель. Замер на
    случае заказчика от 08.09: настоящие дубли дали пересечение от 36% до 78%, то
    есть порога, который ловит все и не ловит лишнего, не существует.

    Слова режем до 6 знаков вместо лемматизации: «установки/установок/установках»
    сходятся, а тащить морфологию в SQL ради этого незачем. Короче 5 знаков
    отбрасываем: предлоги и «нефть» есть почти везде и только шумят.

    ru_min_overlap: второе сходство, по русскому заголовку карточки статьи (у русской статьи без него: по её
    заголовку): основами слов заголовка, как у «Дедупа» радара (word_forms.title_stems), с этим порогом; None: без
    него. На шести знаках русские глаголы расходятся («продала», «продаст»). Замер 04.11 на видимых карточках ленты за
    25.10-04.11: из 51 пары с русским или исходным заголовком от 0,35 судья счёл одним событием 45, исходный заголовок
    ловил 5 из них; ниже 0,35 лежат и настоящие дубли (Halliburton и Keystone: World Oil и Drilling Contractor, 0,33;
    ADNOC и OpenAI: 0,30). overlap пары: лучшее из двух сходств, верхняя граница полосы max_overlap: по нему же.

    Разные источники, условие, а не настройка: внутри одного издания повтор
    заголовка это серийная сводка, и схлопывание таких пар уже уничтожало сотни
    статей в июле.
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            rf"""
            WITH t AS (
              SELECT a.id, a.source_id, a.title, a.published_at,
                     ARRAY(SELECT DISTINCT left(w, 6)
                           FROM unnest(regexp_split_to_array(
                                lower(regexp_replace(a.title, '[^[:alnum:][:space:]]', ' ', 'g')),
                                '\s+')) w
                           WHERE length(w) >= 5) AS toks,
                     -- Основы word_forms.title_stems: слово из [a-zа-яё0-9] от stem_len букв, первые stem_len знаков,
                     -- без чисел и общих слов отрасли.
                     ARRAY(SELECT DISTINCT left(w, %(stem_len)s)
                           FROM unnest(regexp_split_to_array(
                                lower(COALESCE(NULLIF(c.title_ru, ''), a.title)), '[^a-zа-яё0-9]+')) w
                           WHERE length(w) >= %(stem_len)s AND w !~ '^[0-9]+$' AND NOT (w = ANY(%(stop_words)s))
                     ) AS ru_toks
              FROM articles a
              LEFT JOIN article_reprints r ON r.article_id = a.id
              JOIN sources s ON s.id = a.source_id
              LEFT JOIN article_cards c ON c.article_id = a.id
              WHERE a.created_at > now() - make_interval(days => %(days)s)
                AND length(a.title) > 25
                AND r.article_id IS NULL
                -- Только то, что реально видно в ленте. Замер 17.09 на 71 паре,
                -- признанной дублем: 39 пар состояли ИЗ ДВУХ невидимых статей
                -- (модель звали впустую), а в 6 главной копией становилась
                -- невидимая — то есть пометка не схлопывала дубль, а убирала
                -- новость из ленты совсем. Полезными были 21 пара из 71.
                -- Условия те же, что в ленте (api.articles_feed) и в выпуске; отобрана — с решением
                -- «Отбора» (#130): отклонённая и ждущая его решения в ленте не видны.
                AND s.archived_at IS NULL
                AND NOT a.pending_deletion
                AND {feed_window.selected_sql('a', 'c')}
            ),
            -- Оба сходства пары считаются один раз (MATERIALIZED): иначе условие ниже вычисляло бы пересечения
            -- массивов заново в каждом месте, где они упомянуты. Заголовки — только у пар, прошедших отбор.
            p AS MATERIALIZED (
              SELECT x.id AS a_id, y.id AS b_id,
                     {_jaccard_sql('x.toks', 'y.toks')} AS title_overlap,
                     {_jaccard_sql('x.ru_toks', 'y.ru_toks')} AS ru_overlap
              FROM t x
              JOIN t y ON y.id > x.id
                      AND y.source_id <> x.source_id
                      AND (x.published_at IS NULL OR y.published_at IS NULL
                           OR abs(EXTRACT(EPOCH FROM (x.published_at - y.published_at)))
                              < %(apart)s * 86400)
            )
            SELECT p.a_id, p.b_id, x.title AS a_title, y.title AS b_title,
                   round(GREATEST(p.title_overlap, p.ru_overlap), 3) AS overlap
            FROM p
            JOIN t x ON x.id = p.a_id
            JOIN t y ON y.id = p.b_id
            WHERE (p.title_overlap >= %(overlap)s OR p.ru_overlap >= %(ru_overlap)s)
              AND GREATEST(p.title_overlap, p.ru_overlap) <= %(max_overlap)s
            -- Порядок намеренно НЕ по overlap. Замер 17.09 на живом корпусе: из 318
            -- пар за 14 дней только 57 тривиальных (100%%), а полоса, ради которой
            -- судья и заведён (35-49%% — случай заказчика от 08.09), самая большая:
            -- 132 пары. При ORDER BY overlap DESC любой LIMIT отдавал модели ровно
            -- верхушку, и спорные пары не доезжали до неё никогда. md5 по паре id
            -- даёт порядок, не связанный с силой совпадения, и при этом устойчивый
            -- между прогонами.
            ORDER BY md5(p.a_id::text || ':' || p.b_id::text)
            LIMIT %(limit)s
            """,
            {"days": days, "apart": max_days_apart, "overlap": min_overlap,
             "max_overlap": max_overlap, "limit": limit,
             # Без второго сходства, порог выше любого сходства: пару даёт только исходный заголовок.
             "ru_overlap": ru_min_overlap if ru_min_overlap is not None else 2,
             "stem_len": word_forms.TITLE_STEM_LEN, "stop_words": sorted(word_forms.TITLE_STOP_WORDS)},
        )
        return cur.fetchall()


def _jaccard_sql(left: str, right: str) -> str:
    """Доля общих элементов двух массивов слов (пересечение к объединению); 0, если в одном из них меньше трёх слов:
    по двум общим словам короткие заголовки сходились бы случайно."""
    return (f"(CASE WHEN cardinality({left}) >= 3 AND cardinality({right}) >= 3 THEN"
            f" cardinality(ARRAY(SELECT unnest({left}) INTERSECT SELECT unnest({right})))::numeric"
            f" / NULLIF(cardinality(ARRAY(SELECT unnest({left}) UNION SELECT unnest({right}))), 0) ELSE 0 END)")


def _sync_feed_signal(*article_ids: int) -> None:
    """Сигнал ленты повторяет статью (#95): после записи отбора, сути, заголовка, тега или
    балла: синхронизировать. Лениво: feed_signals сам читает константы этого модуля."""
    from oiltech_digest.db import feed_signals

    feed_signals.sync_quietly([int(value) for value in article_ids])


def resolve_reprint_root(conn, article_id: int, *, max_hops: int = 8) -> int:
    """Корень группы перепечаток: главная копия, которая сама ничьей копией не является.

    Пометки ставятся ПОПАРНО, и без разрешения до корня получалась бы цепочка
    C→A→D: каждая пара выбирает главную независимо. С фильтром ленты, который
    прячет всё, что значится копией, такая цепочка унесла бы из выборки ВСЮ группу
    вместе с оригиналом. Ограничение по шагам: защита от кольца в уже накопленных
    данных, а не теоретическая.
    """
    current = int(article_id)
    for _ in range(max_hops):
        row = conn.execute(
            "SELECT primary_id FROM article_reprints WHERE article_id = %s", (current,)
        ).fetchone()
        if row is None:
            return current
        nxt = int(row[0])
        if nxt == current:
            return current
        current = nxt
    return current


def article_visible_in_feed(conn, article_id: int) -> bool:
    """Видна ли статья в ленте: теми же условиями, что и сама лента.

    Заведено не ради стройности: перепечатки решает модель на внешнем воркере, а
    её ответ это недоверенный вход. Пометка прячет копию, и если главной окажется
    статья, которой в ленте нет (архивный источник, помечена на удаление, отбита
    гейтом или «Отбором», ждёт решения «Отбора»), то новость исчезнет целиком вместо схлопывания дубля.
    """
    row = conn.execute(
        f"""
        SELECT 1 FROM articles a
        JOIN sources s ON s.id = a.source_id
        LEFT JOIN article_cards c ON c.article_id = a.id
        WHERE a.id = %s
          AND s.archived_at IS NULL
          AND NOT a.pending_deletion
          AND {feed_window.selected_sql('a', 'c')}
        """,
        (int(article_id),),
    ).fetchone()
    return row is not None


def _chosen_for_digest(conn, article_id: int) -> bool:
    """Отмечена ли статья «в дайджест»."""
    return conn.execute(
        f"SELECT {marks.article_marked_sql('%s', 'digest')}",
        (int(article_id),),
    ).fetchone()[0]


class _KeptBy(NamedTuple):
    """Почему копию нельзя спрятать перепечаткой (_kept_by_people): как это сказать в отказе и в пометке."""

    label: str  # «статья N …»
    both: str  # «обе копии (N и M) …»
    closed: str  # «статья N …, а копия M: из открытого»
    swapped: str  # причина пометки, когда главной стала эта копия


_KEPT_BY_PEOPLE = {
    "digest": _KeptBy("выбрана в дайджест", "выбраны в дайджест", "выбрана в дайджест закрытого месяца",
                      "главная — выбранная в дайджест"),
    "feedback": _KeptBy("с обратной связью человека", "с обратной связью людей",
                        "с обратной связью человека, из закрытого месяца", "главная — с обратной связью человека"),
}
_KEPT_BY_BOTH = "выбраны в дайджест или с обратной связью людей"


def _kept_by_people(conn, article_id: int) -> str | None:
    """Чьё решение держит статью на виду: её выбрали «в дайджест» (digest) или у её сигнала ленты есть запись блока ОС
    человека (feedback: оценка, причина или комментарий, SIGNAL_HAS_FEEDBACK_SQL, как у плашки «С обратной связью»).
    Решение людей важнее выбора судьи, как у «Дедупа» радара: с правилом по русскому заголовку (04.11) судья склеивает
    в разы больше пар, и карточка ушла бы с экрана вместе с обратной связью. Отметки «шум» и «дубль» перепечатку не
    держат: человек сам счёл карточку лишней. None: ничьё."""
    if _chosen_for_digest(conn, article_id):
        return "digest"
    with_feedback = conn.execute(
        f"SELECT EXISTS (SELECT 1 FROM signals s WHERE s.origin = '{SIGNAL_ORIGIN_FEED}'"
        f" AND s.origin_article_id = %s AND {SIGNAL_HAS_FEEDBACK_SQL})",
        (int(article_id),),
    ).fetchone()[0]
    return "feedback" if with_feedback else None


def mark_article_reprint(*, article_id: int, primary_id: int, similarity: float | None,
                         reason: str | None, decided_by: str = "ai",
                         model: str | None = None) -> None:
    """Пометить статью перепечаткой. Запись обратима: удаления нет намеренно.

    Выбор людей важнее выбора судьи. Копию, выбранную кем-то «в дайджест», не прячем: главную он
    не отмечал, и новость ушла бы из его выпуска и из ленты целиком (29.09; так же радар не прячет
    карточку, выбранную в дайджест, mark_signal_merged). Если главную при этом не выбирал
    никто, главной становится выбранная копия, а прячется прежняя главная вместе со своей группой
    (ревью #84: отказ оставлял в ленте у всех обе копии). Выбраны обе: не прячем ни одну. Так же:
    копию с обратной связью человека (блок ОС у её сигнала ленты, 04.11): _kept_by_people."""
    if int(article_id) == int(primary_id):
        raise ValueError("статья не может быть перепечаткой самой себя")
    duplicate_id = int(article_id)
    with get_connection() as conn:
        # Главной назначаем КОРЕНЬ группы, а не соседа по паре: иначе цепочка
        # C→A→D спрячет из ленты и оригинал.
        primary_id = resolve_reprint_root(conn, int(primary_id))
        if duplicate_id == primary_id:
            raise ValueError("статья уже является корнем своей группы перепечаток")
        kept = _kept_by_people(conn, duplicate_id)
        if kept:
            why = _KEPT_BY_PEOPLE[kept]
            other = _kept_by_people(conn, primary_id)
            if other:
                both = why.both if other == kept else _KEPT_BY_BOTH
                raise ValueError(
                    f"обе копии ({duplicate_id} и {primary_id}) {both} — не прячем ни одну"
                )
            # Выбранная копия становится главной, если может ею быть: сама не спрятана и видна в
            # ленте. Иначе, отказ: прятать видимую ради невидимой значит убрать новость целиком.
            if (resolve_reprint_root(conn, duplicate_id) != duplicate_id
                    or not article_visible_in_feed(conn, duplicate_id)):
                raise ValueError(f"статья {duplicate_id} {why.label} — её не прячем как перепечатку")
            # Выбранная копия из закрытого месяца, а прежняя главная, из открытого: главной стала бы
            # архивная, и новость пропала бы из ленты открытого месяца у всех (ревью #88: копия
            # 30.09 в сентябрьском выпуске, копия 01.10: главная после 05.10). Не переставляем,
            # пара отбивается: обе копии видны, каждая в своём месяце.
            closed = _closed_month_article_ids(conn, [duplicate_id, primary_id], feed_window.current())
            if duplicate_id in closed and primary_id not in closed:
                raise ValueError(
                    f"статья {duplicate_id} {why.closed}, а копия {primary_id} — "
                    "из открытого: главную не переставляем"
                )
            logger.info("reprint_primary_swapped: главная %s, спрятана %s — копия %s",
                        duplicate_id, primary_id, why.label)
            duplicate_id, primary_id = primary_id, duplicate_id
            # Перестановку видно в базе (ревью #88): главная: не та, что выбрал судья, и его довод о
            # главной к этой пометке не относится; что это одно событие, его слова, после «судья:».
            # decided_by: как у вызывающего: иных значений, кроме «ai», в базе нет.
            reason = (why.swapped + (f"; судья: {reason}" if reason else ""))[:500]
        elif not article_visible_in_feed(conn, primary_id):
            # Прятать копию можно только в пользу той, которую читатель увидит.
            raise ValueError(
                f"главная копия {primary_id} не видна в ленте — пометка убрала бы новость целиком"
            )
        conn.execute(
            """
            INSERT INTO article_reprints (article_id, primary_id, similarity, reason, decided_by, model)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (article_id) DO UPDATE
               SET primary_id = EXCLUDED.primary_id,
                   similarity = EXCLUDED.similarity,
                   reason = EXCLUDED.reason,
                   decided_by = EXCLUDED.decided_by,
                   model = EXCLUDED.model
            """,
            (duplicate_id, primary_id, similarity, reason, decided_by, model),
        )
        # Группа всегда в один шаг: копии спрятанной статьи: к её главной (как mark_signal_merged).
        conn.execute(
            "UPDATE article_reprints SET primary_id = %s WHERE primary_id = %s",
            (primary_id, duplicate_id),
        )
        conn.commit()
    # Перепечатка: публикация сигнала главной; её свой сигнал: дубль главного.
    _sync_feed_signal(duplicate_id, primary_id)


def unmark_article_reprint(article_id: int) -> bool:
    """Снять пометку перепечатки: статья возвращается в ленту и в выпуск.

    Без этой операции «обратимо» было бы только на словах: пометка ставится ИИ, и
    у человека должен быть способ её отменить, иначе ошибка модели становится
    необратимой ровно так же, как удаление.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM article_reprints WHERE article_id = %s RETURNING primary_id",
            (int(article_id),),
        )
        row = cur.fetchone()
        conn.commit()
    if row is not None:
        # Статья снова главная своего сигнала; у прежней главной: минус публикация.
        _sync_feed_signal(article_id, int(row[0]))
    return row is not None


def list_article_reprints(limit: int = 50) -> list[dict]:
    """Помеченные перепечатки с объяснением: чтобы решение можно было проверить."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT r.article_id, r.primary_id, r.similarity, r.reason, r.decided_by,
                   r.created_at, d.title AS duplicate_title, p.title AS primary_title,
                   ds.name AS duplicate_source, ps.name AS primary_source
            FROM article_reprints r
            JOIN articles d ON d.id = r.article_id
            JOIN articles p ON p.id = r.primary_id
            JOIN sources ds ON ds.id = d.source_id
            JOIN sources ps ON ps.id = p.source_id
            ORDER BY r.created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def reprint_stats() -> dict:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT count(*) AS total, count(DISTINCT primary_id) AS groups FROM article_reprints"
        )
        return cur.fetchone() or {"total": 0, "groups": 0}


def get_article(article_id: int) -> dict | None:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.id = %s
            """,
            (article_id,),
        )
        return cur.fetchone()


def _otbor_fields_sql(alias: str = "a") -> str:
    """Поля «Отбора» статьи для «Суть и перевод» (#131): категория, связь и применение у её сигнала ленты, если
    «Отбор» её решил; без решения: NULL (гейт этих полей не даёт). Ключи: как у сигнала (pipeline.OTBOR_FIELDS)."""
    return ", ".join(
        f"(SELECT os.{column} FROM signals os WHERE os.origin_article_id = {alias}.id"
        f" AND os.origin = '{SIGNAL_ORIGIN_FEED}' AND os.otbor_decision IS NOT NULL LIMIT 1) AS {column}"
        for column in ("signal_category", "oilfield_relevance", "oilfield_application")
    )


def get_articles_by_ids(article_ids: list[int], include_summary: bool = False) -> list[dict]:
    if not article_ids:
        return []
    # Решение «Отбора» (#130) нужно конвейеру: отклонённую статью он не обрабатывает (article_rejected), ждущую
    # решения не берёт (held_by_otbor); категория, связь и применение «Отбора» идут во вход сути и перевода (#131).
    summary_select = (
        ", c.summary, c.relevant, c.title_ru,"
        f" {feed_window.otbor_decision_sql('a')} AS otbor_decision, {_otbor_fields_sql('a')}"
        if include_summary else ""
    )
    summary_join = "LEFT JOIN article_cards c ON c.article_id = a.id" if include_summary else ""
    placeholders = ", ".join(["%s"] * len(article_ids))
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
                   {summary_select}
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            {summary_join}
            WHERE a.id IN ({placeholders})
            ORDER BY array_position(%s::bigint[], a.id)
            """,
            [*article_ids, article_ids],
        )
        return cur.fetchall()


def _in_some_issue(conn, article_id: int) -> bool:
    """Статья в выпуске: в сохранённом черновике или отмечена «в дайджест». С #84 выпуск строится
    по отметкам, а черновик задаёт только порядок: защита одних черновиков пропускала выбранную, но
    не сохранённую статью (ревью #88). Черновик, любой, и архивный личный (#111): позиция черновика
    держит статью внешним ключом без каскада, и удалить такую статью без force всё равно нельзя."""
    return bool(conn.execute(
        "SELECT EXISTS (SELECT 1 FROM monthly_digest_items WHERE article_id = %s)"
        f" OR {marks.article_marked_sql('%s', 'digest')}",
        (int(article_id), int(article_id)),
    ).fetchone()[0])


def delete_article(article_id: int, *, force: bool = False) -> bool:
    """Физически удалить статью и все её зависимые строки. Возвращает True, если удалена.

    FK на articles в основном БЕЗ ON DELETE CASCADE, поэтому удаляем детей вручную
    в правильном порядке (отметка статьи каскадится сама). По умолчанию НЕ удаляем
    статью из выпуска (сохранённый черновик или отметка «в дайджест»): чтобы не
    рвать историю и не терять отметку; force=True снимает защиту (удалит и ссылки дайджеста).
    Статью агента «Отбор» (#130) удаление не трогает и с force: убирает её из ленты только его решение."""
    with get_connection() as conn:
        if _is_otbor_article(conn, article_id) or (not force and _in_some_issue(conn, article_id)):
            return False
        conn.execute(
            """
            DELETE FROM article_score_items
            WHERE article_score_id IN (SELECT id FROM article_scores WHERE article_id = %s)
            """,
            (article_id,),
        )
        conn.execute("DELETE FROM article_scores WHERE article_id = %s", (article_id,))
        conn.execute("DELETE FROM article_tags WHERE article_id = %s", (article_id,))
        conn.execute("DELETE FROM ai_processing_runs WHERE article_id = %s", (article_id,))
        conn.execute("DELETE FROM article_cards WHERE article_id = %s", (article_id,))
        if force:
            conn.execute("DELETE FROM monthly_digest_items WHERE article_id = %s", (article_id,))
        cur = conn.execute("DELETE FROM articles WHERE id = %s RETURNING id", (article_id,))
        deleted = cur.fetchone() is not None
        conn.commit()
    return deleted


def count_pending_deletion() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT count(*) FROM articles WHERE pending_deletion").fetchone()[0]


def list_pending_deletion(limit: int = 100) -> list[dict]:
    """Помеченные на удаление: заголовок/источник/причина (для просмотра перед purge)."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT a.id, a.title, s.name AS source_name, a.deletion_reason "
            "FROM articles a JOIN sources s ON s.id = a.source_id "
            "WHERE a.pending_deletion ORDER BY a.id LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def purge_pending_deletion(*, force: bool = False) -> int:
    """Физически удалить все помеченные (pending_deletion) статьи. Возвращает число удалённых.
    Использует delete_article (тот же каскад + защита дайджеста при force=False)."""
    with get_connection() as conn:
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM articles WHERE pending_deletion ORDER BY id"
        ).fetchall()]
    deleted = 0
    for article_id in ids:
        if delete_article(article_id, force=force):
            deleted += 1
    return deleted


def unmark_all_pending_deletion() -> int:
    """Снять пометку «на удаление» со всех статей (вернуть в строй). Возвращает число."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE articles SET pending_deletion = FALSE, deletion_reason = NULL, "
            "marked_for_deletion_at = NULL, updated_at = now() WHERE pending_deletion"
        )
        conn.commit()
        return cur.rowcount


def otbor_article_sql(alias: str = "a") -> str:
    """Статья агента «Отбор» (#130): решает он (вставлена после его включения) или уже решил. Удаление статьи
    (delete_article, разовая чистка помеченных recheck-purge) такую статью не трогает."""
    return (f"({alias}.selector IS NOT DISTINCT FROM '{ARTICLE_SELECTOR_OTBOR}'"
            f" OR {feed_window.otbor_decision_sql(alias)} IS NOT NULL)")


def _is_otbor_article(conn, article_id: int) -> bool:
    return conn.execute(f"SELECT 1 FROM articles a WHERE a.id = %s AND {otbor_article_sql('a')}",
                        (int(article_id),)).fetchone() is not None


def article_ids_needing_title_ru() -> list[int]:
    """id статей без русского заголовка: для батч-бэкфилла перевода через воркер."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT a.id FROM articles a
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE c.title_ru IS NULL
            ORDER BY a.id ASC
            """
        )
        return [row[0] for row in cur.fetchall()]


def get_articles_needing_summary(limit: int = 20) -> list[dict]:
    """Статьи, которым нужна AI-суть: отобранные в ленту без сути, в том числе принятые после сбоя записи (повтор).

    ОТКЛОНЁННЫЕ ИСКЛЮЧЕНЫ, и это принципиально: отклонённой статье («Отбором», до него гейтом) суть
    НЕ пишется, и без фильтра она вечно подходила бы под `summary IS NULL` и возвращалась в обработку
    КАЖДЫЙ цикл. Так было при гейте (снят 08.11): он резал ~78%, бюджет цикла (AI_PROCESS_LIMIT) съедали
    повторные отказы одних и тех же статей, свежие вытеснялись из топа (ORDER BY published_at DESC), а
    дорогой гейт (gpt-5.5) жёгся заново на том же наборе. Ждущую «Отбора» статью выборка тоже не берёт.
    Условие отбора то же, что у ленты (feed_window.selected_sql).
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE c.summary IS NULL
              AND {feed_window.selected_sql('a', 'c')}
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


# Каких стадий нет у статьи: сути или перевода. Тега и балла статьи с 08.11 нет (тему и баллы ставит «Карточка» у
# сигнала): условие «нет тега или балла» выбирало бы каждую принятую статью в каждом цикле, и она занимала бы
# AI_PROCESS_LIMIT, не получая ничего.
_STAGES_MISSING_SQL = "(c.summary IS NULL OR c.title_ru IS NULL)"


def _needs_pipeline_from() -> str:
    """Статьи, которым не хватает стадий обработки ленты (суть, перевод): FROM и WHERE для выборки.

    Решает «Отбор» (#130): принятая им статья получает недостающие суть и перевод, отклонённая не получает ничего,
    ждущая его решения тоже, пока он не решит. Статья, решённая до «Отбора» гейтом ленты (история: гейт снят 08.11),
    живёт по его решению: принятая догоняет суть и перевод, остальные не берутся (решать их больше некому). То же
    правило для явного списка статей: held_by_otbor и article_rejected."""
    return f"""
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN LATERAL (SELECT {feed_window.otbor_decision_sql('a')} AS decision) od ON TRUE
            WHERE CASE
              WHEN od.decision IS NOT NULL
                THEN od.decision = '{feed_window.OTBOR_ACCEPT}' AND (c.article_id IS NULL OR {_STAGES_MISSING_SQL})
              ELSE c.relevant IS TRUE AND {_STAGES_MISSING_SQL}
            END
"""


def otbor_verdict(article: dict) -> bool | None:
    """Решение «Отбора» о статье строки выборки (#130): True: принял, False: отклонил, None: не решал. Им
    обработка ленты заменяет гейт у решённой «Отбором» статьи (здесь, на воркере и в местном конвейере)."""
    decision = article.get("otbor_decision")
    return None if decision is None else decision == feed_window.OTBOR_ACCEPT


def article_rejected(article: dict) -> bool:
    """Статья строки выборки отклонена: «Отбором», а без его решения гейтом ленты до «Отбора» (история). Суть и
    перевод ей не пишутся ни в каком пути обработки: гейта, который мог передумать, с 08.11 нет. Строка: из
    get_articles_by_ids с include_summary или get_articles_needing_pipeline."""
    verdict = otbor_verdict(article)
    return verdict is False or (verdict is None and article.get("relevant") is False)


def held_by_otbor(article: dict) -> bool:
    """Статью держит «Отбор» (#130): правило _needs_pipeline_from для явного списка статей (ручной импорт,
    перекачка тела, команда с номерами): отклонённую им обработка ленты не берёт, ждущую его решения (путь «Отбора»,
    гейт её не решал) тоже. Строка: из get_articles_by_ids с include_summary (решение «Отбора», гейт, путь статьи)."""
    verdict = otbor_verdict(article)
    if verdict is not None:
        return not verdict
    return article.get("selector") == ARTICLE_SELECTOR_OTBOR and article.get("relevant") is None


def get_articles_needing_pipeline(limit: int = 20) -> list[dict]:
    """Статьи, которым не хватает сути или перевода заголовка (_needs_pipeline_from).

    Используется process/process-full/background/external enqueue. Старый выбор только по
    ``summary IS NULL`` не поднимал статьи после частичного сбоя на переводе.
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.*, c.summary, c.relevant, c.title_ru,
                   s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category, od.decision AS otbor_decision, {_otbor_fields_sql('a')}
            {_needs_pipeline_from()}
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


# Ключ advisory-lock: выбор статей для ИИ-пакета при выдаче идёт по одному.
_PROCESS_RESERVE_LOCK = 7_290_921


class ArticlesBusy(RuntimeError):
    """Статьи явного списка сейчас в работе у другой задачи: выдачу надо отложить."""


def process_articles_in_work(conn, *, exclude_job_id: int | None = None) -> list[int]:
    """Статьи ИИ-пакетов, уже выданных воркеру (в работе или в записи итога).

    Их payload собран при выдаче: итог ляжет по тексту на тот момент. Этим списком выдача не
    даёт двум пакетам одни статьи (reserve_process_articles), а замена тела полным текстом
    ставит пересчёт статьям, чей пакет ушёл с обрывком (fulltext_recompute)."""
    # Только настоящий массив: payload с "article_ids": null (так его пишет
    # /api/jobs/process) это jsonb null, а не SQL NULL, COALESCE его не пропускает,
    # и выдача падала бы для всех ИИ-задач, пока такая задача выполняется.
    rows = conn.execute(
        """
        SELECT DISTINCT (jsonb_array_elements_text(CASE
                   WHEN jsonb_typeof(payload_json->'reserved_article_ids') = 'array'
                       THEN payload_json->'reserved_article_ids'
                   WHEN jsonb_typeof(payload_json->'article_ids') = 'array'
                       THEN payload_json->'article_ids'
                   ELSE '[]'::jsonb
               END))::bigint
        FROM background_jobs
        WHERE kind = 'process_articles'
          AND status IN ('running', 'finalizing')
          AND (%s::bigint IS NULL OR id <> %s::bigint)
        """,
        (exclude_job_id, exclude_job_id),
    ).fetchall()
    return [int(row[0]) for row in rows]


def reserve_process_articles(job_id: int, *, limit: int, article_ids: list[int] | None = None) -> list[int]:
    """Статьи ИИ-пакета при выдаче: за вычетом тех, что уже в работе у других задач.

    Пакет без article_ids выбирает «статьи без обработки» в момент выдачи; две ИИ-полосы
    (поток дня и пересчёты) взяли бы одни и те же и оплатили бы их дважды: поэтому второй
    ИИ-воркер 18.09 и не ставили. Выбор и запись резерва: в одной транзакции под
    advisory-lock: параллельная выдача ждёт, а не читает резерв до записи.

    Явный список не урезается: соседняя задача могла взять статью со СТАРЫМ текстом
    (пересчёт ставят после перекачки тела), и выброшенная из пересчёта статья осталась бы
    посчитанной по обрывку. Такой список, ArticlesBusy: выдачу откладывают, пока соседка
    не закончит."""
    with get_connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_PROCESS_RESERVE_LOCK,))
        busy = process_articles_in_work(conn, exclude_job_id=job_id)
        if article_ids:
            overlap = sorted(set(int(item) for item in article_ids) & set(busy))
            if overlap:
                raise ArticlesBusy(f"статьи {overlap[:10]} сейчас в работе у другой задачи")
            chosen = [int(item) for item in article_ids]
        else:
            chosen = [
                int(row[0])
                for row in conn.execute(
                    f"""
                    SELECT a.id
                    {_needs_pipeline_from()}
                      AND a.id <> ALL(%s::bigint[])
                    ORDER BY a.published_at DESC NULLS LAST, a.id DESC
                    LIMIT %s
                    """,
                    (busy, limit),
                ).fetchall()
            ]
        conn.execute(
            """
            UPDATE background_jobs
            SET payload_json = payload_json || jsonb_build_object('reserved_article_ids', %s::jsonb)
            WHERE id = %s
            """,
            (Json(chosen), job_id),
        )
        conn.commit()
    return chosen


def defer_claimed_background_job(job_id: int, *, seconds: int = 120) -> bool:
    """Вернуть только что выданную задачу в очередь на потом, не тратя попытку."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE background_jobs
            SET status = 'queued',
                attempts = GREATEST(attempts - 1, 0),
                run_after = now() + (%s::text || ' seconds')::interval,
                claimed_by = NULL,
                lease_token_hash = NULL,
                lease_expires_at = NULL
            WHERE id = %s AND status = 'running'
            """,
            (seconds, job_id),
        )
        conn.commit()
        return cur.rowcount == 1


def find_article_candidates(query: str, limit: int = 20) -> list[dict]:
    terms = [term.strip().lower() for term in query.split() if term.strip()]
    if not terms:
        return []
    conditions = []
    params: list[str] = []
    for term in terms:
        conditions.append("LOWER(a.title || ' ' || COALESCE(a.raw_text, '')) LIKE %s")
        params.append(f"%{term}%")
    where = " OR ".join(conditions)
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id, a.title, a.language, a.published_at, s.name AS source_name,
                   LEFT(COALESCE(a.raw_text, ''), 240) AS snippet
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE {where}
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def upsert_article_card(article_id: int, summary: str, model: str | None = None,
                        title_ru: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO article_cards (article_id, summary, summary_model, title_ru, summary_generated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (article_id) DO UPDATE SET
                summary = EXCLUDED.summary,
                summary_model = EXCLUDED.summary_model,
                title_ru = COALESCE(EXCLUDED.title_ru, article_cards.title_ru),
                summary_generated_at = now(),
                updated_at = now()
            """,
            (article_id, summary, model, title_ru),
        )
        conn.commit()
    _sync_feed_signal(article_id)


def set_article_title_ru(article_id: int, title_ru: str) -> None:
    """Проставить русский заголовок отдельной стадией перевода. Создаёт карточку,
    если её ещё нет (статья могла не пройти суммаризацию), не трогая summary."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO article_cards (article_id, title_ru)
            VALUES (%s, %s)
            ON CONFLICT (article_id) DO UPDATE SET
                title_ru = EXCLUDED.title_ru,
                updated_at = now()
            """,
            (article_id, title_ru),
        )
        conn.commit()
    _sync_feed_signal(article_id)


def list_article_texts_for_terminology_audit(limit: int = 200, article_id: int | None = None) -> list[dict]:
    clauses = ["(COALESCE(c.summary, '') <> '' OR COALESCE(c.title_ru, '') <> '')"]
    params: list = []
    if article_id is not None:
        clauses.append("a.id = %s")
        params.append(article_id)
    params.append(limit)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id, a.title, a.raw_text, a.language, s.name AS source_name,
                   s.category AS source_category, c.summary, c.title_ru
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            WHERE {" AND ".join(clauses)}
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def update_article_terminology_texts(article_id: int, *, summary: str | None = None, title_ru: str | None = None) -> None:
    if summary is None and title_ru is None:
        return
    sets = ["updated_at = now()"]
    params: dict[str, object] = {"article_id": article_id}
    if summary is not None:
        sets.append("summary = %(summary)s")
        params["summary"] = summary
    if title_ru is not None:
        sets.append("title_ru = %(title_ru)s")
        params["title_ru"] = title_ru
    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE article_cards
            SET {", ".join(sets)}
            WHERE article_id = %(article_id)s
            """,
            params,
        )
        conn.commit()
    _sync_feed_signal(article_id)


def get_articles_needing_title_ru(limit: int = 20) -> list[dict]:
    """Статьи без русского заголовка (card.title_ru IS NULL ИЛИ карточки ещё нет).
    Бэкфилл отдельной стадии перевода по всей базе."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE c.title_ru IS NULL
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def upsert_tag(rec: dict) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT id FROM tags
            WHERE name = %s AND (
                (parent_id IS NULL AND %s::bigint IS NULL) OR parent_id = %s::bigint
            )
            """,
            (rec.get("name"), rec.get("parent_id"), rec.get("parent_id")),
        )
        row = cur.fetchone()
        if row:
            tag_id = row[0]
            # Сид ДОПОЛНЯЕТ ключевые слова, а не заменяет. С 12.09 их можно править в UI
            # («Теги» → «Ключевые слова RU/EN»), и прежняя перезапись молча стирала бы
            # правки заказчика при каждом повторном прогоне seed-tags: а прогон случается
            # сам, в bootstrap на деплое. Порядок сохраняем: сначала то, что уже было.
            existing = conn.execute(
                "SELECT COALESCE(keywords_json, '[]'::jsonb), COALESCE(keywords_en_json, '[]'::jsonb), "
                "COALESCE(negative_keywords_json, '[]'::jsonb) "
                "FROM tags WHERE id = %s", (tag_id,)
            ).fetchone()

            def _merge(current, incoming):
                merged, seen = [], set()
                for word in [*(current or []), *(incoming or [])]:
                    key = str(word).strip().lower()
                    if key and key not in seen:
                        seen.add(key)
                        merged.append(word)
                return merged

            conn.execute(
                """
                UPDATE tags
                SET name_en = %(name_en)s,
                    description = %(description)s,
                    keywords_json = %(keywords_json)s,
                    keywords_en_json = %(keywords_en_json)s,
                    -- Стоп-слова сид РАНЬШЕ НЕ ПИСАЛ ВООБЩЕ: ни в UPDATE, ни в INSERT.
                    -- Поэтому в проде они были NULL у всех 18 тегов, механизм подавления
                    -- шума стоял пустым, и он же ломал сохранение экрана «Теги» (422).
                    negative_keywords_json = %(negative_keywords_json)s,
                    sort_order = %(sort_order)s,
                    updated_at = now()
                WHERE id = %(id)s
                """,
                {
                    **rec,
                    "id": tag_id,
                    "keywords_json": Json(_merge(existing[0], rec.get("keywords_json"))),
                    "keywords_en_json": Json(_merge(existing[1], rec.get("keywords_en_json"))),
                    # Стоп-слова ЗАМЕНЯЕМ, а не сливаем: они запрещают, и «случайно
                    # накопленный» запрет молча выкашивал бы статьи. Если вызывающий их
                    # не передал (None): оставляем набранное руками, не затираем.
                    "negative_keywords_json": Json(
                        rec["negative_keywords_json"]
                        if rec.get("negative_keywords_json") is not None
                        else (existing[2] or [])
                    ),
                },
            )
            conn.commit()
            return tag_id

        cur = conn.execute(
            """
            INSERT INTO tags (parent_id, name, name_en, description, keywords_json,
                              keywords_en_json, negative_keywords_json, enabled, sort_order)
            VALUES (%(parent_id)s, %(name)s, %(name_en)s, %(description)s,
                    %(keywords_json)s, %(keywords_en_json)s,
                    %(negative_keywords_json)s, TRUE, %(sort_order)s)
            RETURNING id
            """,
            {
                **rec,
                "keywords_json": Json(rec.get("keywords_json") or []),
                "keywords_en_json": Json(rec.get("keywords_en_json") or []),
                "negative_keywords_json": Json(rec.get("negative_keywords_json") or []),
            },
        )
        tag_id = cur.fetchone()[0]
        conn.commit()
        return tag_id


def list_enabled_tags() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT child.*, parent.name AS parent_name, parent.name_en AS parent_name_en
            FROM tags child
            LEFT JOIN tags parent ON parent.id = child.parent_id
            WHERE child.enabled = TRUE
            ORDER BY child.parent_id NULLS FIRST, child.sort_order, child.id
            """
        )
        return cur.fetchall()


def save_tags(items: list[dict]) -> dict:
    """Bulk-сохранение дерева тегов из UI. Родители обрабатываются раньше детей
    (parent резолвится по имени). Отсутствующие в списке: отключаются (soft delete,
    чтобы не рвать FK на article_tags)."""
    name_to_id: dict[str, int] = {}
    keep: list[int] = []
    # сначала корневые, затем дочерние: чтобы parent_id уже был известен
    ordered = [i for i in items if not i.get("parent_name")] + [i for i in items if i.get("parent_name")]
    with get_connection() as conn:
        for it in ordered:
            parent_id = None
            parent_name = it.get("parent_name")
            if parent_name:
                # `parent_id` (если фронт его прислал) НАДЁЖНЕЕ имени: раньше связь искалась
                # только по имени, и переименование родителя в UI молча делало все его
                # подтеги КОРНЕВЫМИ: без ошибки и без предупреждения. Дерево разваливалось
                # на первом же редактировании, а заказчик как раз собирался расширять теги.
                parent_id = it.get("parent_id") or name_to_id.get(parent_name)
                if parent_id is None:
                    raise ValueError(
                        f"Подтег «{it['name']}» ссылается на родителя «{parent_name}», "
                        "которого нет в сохраняемом списке. Сохранение отменено, "
                        "чтобы подтеги не стали корневыми молча."
                    )
            payload = {
                "parent_id": parent_id,
                "name": it["name"],
                "name_en": it.get("name_en"),
                "description": it.get("description"),
                "keywords_json": Json(it.get("keywords_json") or []),
                "keywords_en_json": Json(it.get("keywords_en_json") or []),
                "negative_keywords_json": Json(it.get("negative_keywords_json") or []),
                "enabled": bool(it.get("enabled", True)),
                "sort_order": it.get("sort_order") or 0,
            }
            previous_name: str | None = None
            if it.get("id"):
                row = conn.execute("SELECT name FROM tags WHERE id = %s", (int(it["id"]),)).fetchone()
                previous_name = row[0] if row else None
                conn.execute(
                    """
                    UPDATE tags SET parent_id=%(parent_id)s, name=%(name)s, name_en=%(name_en)s,
                        description=%(description)s, keywords_json=%(keywords_json)s,
                        keywords_en_json=%(keywords_en_json)s, negative_keywords_json=%(negative_keywords_json)s,
                        enabled=%(enabled)s, sort_order=%(sort_order)s, updated_at=now()
                    WHERE id=%(id)s
                    """,
                    {**payload, "id": int(it["id"])},
                )
                tag_id = int(it["id"])
            else:
                # UPSERT, а не голый INSERT: тег с таким именем под тем же родителем мог
                # существовать и быть выключённым (delete_tag: мягкое удаление). Раньше
                # повторное добавление роняло запрос UniqueViolation → 500 у заказчика,
                # и «удалил, потом снова добавил» превращалось в тупик.
                # Конфликт по тому же выражению, что и idx_tags_name_parent.
                cur = conn.execute(
                    """
                    INSERT INTO tags (parent_id, name, name_en, description, keywords_json,
                                      keywords_en_json, negative_keywords_json, enabled, sort_order)
                    VALUES (%(parent_id)s, %(name)s, %(name_en)s, %(description)s,
                            %(keywords_json)s, %(keywords_en_json)s, %(negative_keywords_json)s, %(enabled)s, %(sort_order)s)
                    ON CONFLICT (name, (COALESCE(parent_id, 0))) DO UPDATE SET
                        parent_id = EXCLUDED.parent_id,
                        name_en = EXCLUDED.name_en,
                        description = EXCLUDED.description,
                        keywords_json = EXCLUDED.keywords_json,
                        keywords_en_json = EXCLUDED.keywords_en_json,
                        negative_keywords_json = EXCLUDED.negative_keywords_json,
                        enabled = EXCLUDED.enabled,
                        sort_order = EXCLUDED.sort_order,
                        updated_at = now()
                    RETURNING id
                    """,
                    payload,
                )
                tag_id = int(cur.fetchone()[0])
            name_to_id[it["name"]] = tag_id
            # СТАРОЕ имя тоже обязано вести к этому id: подтеги в запросе всё ещё несут
            # прежний parent_name, если родителя только что переименовали. Берём его из
            # БД по id: это не зависит от того, прислал ли фронт original_name.
            if previous_name and previous_name != it["name"]:
                name_to_id.setdefault(previous_name, tag_id)
            keep.append(tag_id)
        if keep:
            # СТРАХОВКА от потери таксономии одним нажатием. Сохранение выключает всё,
            # чего нет в присланном списке, и это правильно, пока фронт шлёт ВЕСЬ
            # список. Но 13.09 проверочный запрос с одним тегом выключил разом все 14:
            # неполный список (обрыв загрузки, частичный рендер, чужой скрипт) стирает
            # справочник молча и без следа.
            # Порог намеренно мягкий: сокращение набора: законная операция (мы сами
            # ужали 18 направлений до 13), поэтому запрещаем только обвал БОЛЬШЕ чем
            # наполовину. Осознанная смена таксономии идёт отдельной командой
            # `retire-tags`, которой этот предохранитель не мешает.
            enabled_before = conn.execute(
                "SELECT count(*) FROM tags WHERE enabled"
            ).fetchone()[0]
            would_disable = conn.execute(
                "SELECT count(*) FROM tags WHERE enabled AND id <> ALL(%s)", (keep,)
            ).fetchone()[0]
            if enabled_before >= 4 and would_disable * 2 > enabled_before:
                raise ValueError(
                    f"Сохранение выключило бы {would_disable} тегов из {enabled_before} — "
                    "больше половины справочника. Похоже, список пришёл неполным. "
                    "Изменения отменены; смена таксономии целиком делается командой retire-tags."
                )
            system_row = conn.execute(
                "SELECT id FROM tags WHERE name = %s", (SYSTEM_TAG_UNCLASSIFIED,)
            ).fetchone()
            if system_row and int(system_row[0]) not in keep:
                keep.append(int(system_row[0]))
            conn.execute("UPDATE tags SET enabled=FALSE WHERE id <> ALL(%s)", (keep,))
        conn.commit()
    return {"saved": len(items)}


def disable_tags_except(names: list[str]) -> int:
    """Выключить все теги, кроме перечисленных. Возвращает, сколько выключено.

    Мягко: на теги ссылается article_tags, и удаление уничтожило бы историю
    классификации всего корпуса. Выключенный тег не предлагается модели
    (list_enabled_tags фильтрует по enabled) и не участвует в новой разметке.
    """
    if not names:
        raise ValueError("Пустой список тегов к сохранению — это выключило бы все теги разом")
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE tags SET enabled = FALSE, updated_at = now() "
            "WHERE enabled AND name <> ALL(%s)",
            (names,),
        )
        conn.commit()
        return cur.rowcount or 0


def delete_tag(tag_id: int) -> None:
    """Мягкое удаление тега и его подтегов (enabled=FALSE): FK на article_tags не рвём."""
    with get_connection() as conn:
        row = conn.execute("SELECT name FROM tags WHERE id = %s", (tag_id,)).fetchone()
        if row and row[0] == SYSTEM_TAG_UNCLASSIFIED:
            raise ValueError(
                f"«{SYSTEM_TAG_UNCLASSIFIED}» — служебный приёмник, а не тематика. "
                "Без него статьи, не подошедшие ни к одной теме, уедут в первый тег списка."
            )
        conn.execute(
            "UPDATE tags SET enabled=FALSE, updated_at=now() WHERE id=%s OR parent_id=%s",
            (tag_id, tag_id),
        )
        conn.commit()


def _upsert_scoring_criterion(conn, rec: dict, profile: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO scoring_criteria (profile, name, description, weight, keywords_json,
                                      keywords_en_json, enabled, sort_order)
        VALUES (%(profile)s, %(name)s, %(description)s, %(weight)s, %(keywords_json)s,
                %(keywords_en_json)s, TRUE, %(sort_order)s)
        -- Сид ГАРАНТИРУЕТ СУЩЕСТВОВАНИЕ критериев по умолчанию, но НЕ переопределяет
        -- решения человека. Раньше здесь стояло `enabled = TRUE`, и каждый деплой
        -- воскрешал критерии, которые заказчик выключил в UI. Это не теория: 11.09
        -- заказчик утром перестроил профиль (5 критериев, сумма ровно 100), в 11:40
        -- прошёл деплой брендинга, bootstrap поднял обратно три старых — и через две
        -- минуты он написал «а что случилось со скорингом? там сейчас 9 параметров».
        -- Хуже того, сумма весов стала бы 175 вместо 100, и стадия скоринга падает
        -- целиком на первой же статье (_validate_weights).
        -- Вес и флаг — территория человека (экран «Скоринг»), сид их не трогает.
        -- Ключевые слова дополняем, а не заменяем: их там тоже правят руками.
        ON CONFLICT (profile, name) DO UPDATE SET
            description = COALESCE(scoring_criteria.description, EXCLUDED.description),
            keywords_json = (
                SELECT COALESCE(jsonb_agg(DISTINCT w), '[]'::jsonb)
                FROM jsonb_array_elements(
                    COALESCE(scoring_criteria.keywords_json, '[]'::jsonb) || EXCLUDED.keywords_json
                ) AS w
            ),
            keywords_en_json = (
                SELECT COALESCE(jsonb_agg(DISTINCT w), '[]'::jsonb)
                FROM jsonb_array_elements(
                    COALESCE(scoring_criteria.keywords_en_json, '[]'::jsonb) || EXCLUDED.keywords_en_json
                ) AS w
            ),
            updated_at = now()
        RETURNING id
        """,
        {
            "description": None,
            "sort_order": 0,
            **rec,
            "profile": profile,
            "keywords_json": Json(rec.get("keywords_json") or []),
            "keywords_en_json": Json(rec.get("keywords_en_json") or []),
        },
    )
    return int(cur.fetchone()[0])


def upsert_scoring_criterion(rec: dict, profile: str = ARTICLE_SCORING_PROFILE) -> int:
    """Критерий по имени в профиле: нет: завести включённым; есть: дополнить, не переписывая."""
    profile = check_profile(profile)
    with get_connection() as conn:
        criterion_id = _upsert_scoring_criterion(conn, rec, profile)
        conn.commit()
        return criterion_id


# Сид профилей скоринга: по одному за раз (bootstrap и ручной seed-scoring разом). Ключ свой:
# 7_290_922 всю жизнь держит планировщик (singleton.SCHEDULER_LOCK_KEY), а seed-scoring он
# запускает дочерним процессом в bootstrap: на общем ключе сид ждал бы его вечно.
_SEED_SCORING_LOCK = 7_290_923


def seed_scoring_profile(profile: str, records: list[dict]) -> int:
    """Набор критериев по умолчанию: только в ПУСТОЙ профиль и одной транзакцией.

    Пустой: без единой строки, включённой или выключенной. В непустой профиль сид не
    добавляет ничего (сессия G): у профиля business на проде свой набор с суммой 100, и сид
    со своим набором сделал бы 200: оценка встала бы на локальных путях и раздулась бы на
    NL. Прежний сид по имени к тому же воскрешал переименованное: заказчик сменил имя:
    следующий bootstrap заводил старое заново, включённым.

    Одна транзакция: при сбое на середине не останется профиль из пары критериев с суммой
    55, который следующий сид счёл бы уже заведённым. Возвращает число заведённых
    критериев; 0: профиль не пуст."""
    profile = check_profile(profile)
    try:
        with get_connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SEED_SCORING_LOCK,))
            if conn.execute("SELECT 1 FROM scoring_criteria WHERE profile = %s LIMIT 1", (profile,)).fetchone():
                return 0
            for rec in records:
                _upsert_scoring_criterion(conn, rec, profile)
            conn.commit()
    except pg_errors.UniqueViolation as exc:
        if _violated_index(exc) == "idx_scoring_criteria_name":
            raise RuntimeError(
                f"профиль {profile} не заведён: имя критерия уже занято в другом профиле, а в базе "
                "ещё глобальный индекс имён idx_scoring_criteria_name. Снимите его (DROP INDEX IF EXISTS "
                "idx_scoring_criteria_name, так же делает init-db) и повторите seed-scoring."
            ) from exc
        raise
    return len(records)


def apply_scoring_preset(profile: str, records: list[dict], *, apply: bool = False) -> dict:
    """Сделать набор records единственным активным набором профиля: одной транзакцией.

    Решение владельца 29.09: сид непустой профиль не трогает, поэтому набор на проде меняется
    только так: явно, после сухого прогона. Критерии не удаляются: прежние активные
    выключаются (на них ссылаются подпункты старых баллов), совпавшие по имени включаются,
    недостающие заводятся. Вес, описание и порядок: из набора; ключевые слова уже заведённого
    критерия не трогаются (их правят на экране). Перед записью: сумма набора 100 и имена без
    повторов; перед коммитом: активны ровно критерии набора с суммой 100, иначе откат.
    Другой профиль не затрагивается; повторный прогон ничего не пишет.

    Возвращает «до» и «после» (активные: имя и вес), план по именам и признак записи."""
    profile = check_profile(profile)
    names = [str(rec["name"]) for rec in records]
    if len(set(names)) != len(names):
        raise ValueError("в наборе повторяются имена критериев")
    total = round(sum(float(rec["weight"]) for rec in records), 2)
    if total != 100:
        raise ValueError(f"сумма весов набора — {total}, а нужна 100")
    try:
        with get_connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SEED_SCORING_LOCK,))
            cur = conn.cursor(row_factory=dict_row)
            rows = cur.execute(
                "SELECT id, name, description, weight, enabled, sort_order FROM scoring_criteria "
                "WHERE profile = %s ORDER BY sort_order, id FOR UPDATE",
                (profile,),
            ).fetchall()
            by_name = {row["name"]: row for row in rows}
            plan: dict[str, list[str]] = {"add": [], "enable": [], "update": [], "disable": []}
            for rec in records:
                row = by_name.get(rec["name"])
                if row is None:
                    plan["add"].append(rec["name"])
                elif not row["enabled"]:
                    plan["enable"].append(rec["name"])
                elif (float(row["weight"]), row["description"] or "", int(row["sort_order"] or 0)) != (
                    float(rec["weight"]), rec.get("description") or "", int(rec.get("sort_order") or 0)
                ):
                    plan["update"].append(rec["name"])
            wanted = set(names)
            plan["disable"] = [row["name"] for row in rows if row["enabled"] and row["name"] not in wanted]
            result = {
                "profile": profile,
                "before": [(row["name"], float(row["weight"])) for row in rows if row["enabled"]],
                # Для отката после записи: этот набор включается обратно одним UPDATE по id.
                "before_ids": [int(row["id"]) for row in rows if row["enabled"]],
                "after": [(rec["name"], float(rec["weight"])) for rec in records],
                "plan": plan,
                "changed": any(plan.values()),
                "applied": False,
            }
            if not apply or not result["changed"]:
                return result
            touched = set(plan["add"]) | set(plan["enable"]) | set(plan["update"])
            for rec in records:
                if rec["name"] not in touched:
                    continue
                conn.execute(
                    """
                    INSERT INTO scoring_criteria (profile, name, description, weight, keywords_json,
                                                  keywords_en_json, enabled, sort_order)
                    VALUES (%(profile)s, %(name)s, %(description)s, %(weight)s, %(keywords_json)s,
                            %(keywords_en_json)s, TRUE, %(sort_order)s)
                    ON CONFLICT (profile, name) DO UPDATE SET
                        description = EXCLUDED.description, weight = EXCLUDED.weight,
                        sort_order = EXCLUDED.sort_order, enabled = TRUE, updated_at = now()
                    """,
                    {
                        "profile": profile, "name": rec["name"], "description": rec.get("description"),
                        "weight": rec["weight"], "sort_order": rec.get("sort_order") or 0,
                        "keywords_json": Json(rec.get("keywords_json") or []),
                        "keywords_en_json": Json(rec.get("keywords_en_json") or []),
                    },
                )
            conn.execute(
                "UPDATE scoring_criteria SET enabled = FALSE, updated_at = now() "
                "WHERE profile = %s AND enabled AND name <> ALL(%s)",
                (profile, names),
            )
            active_sum, active_names = conn.execute(
                "SELECT COALESCE(SUM(weight), 0), COALESCE(array_agg(name), '{}') FROM scoring_criteria "
                "WHERE profile = %s AND enabled",
                (profile,),
            ).fetchone()
            if round(float(active_sum), 2) != 100 or sorted(active_names) != sorted(names):
                raise RuntimeError(
                    f"после применения активны {sorted(active_names)} с суммой {active_sum} — откат, ничего не записано"
                )
            conn.commit()
    except pg_errors.UniqueViolation as exc:
        if _violated_index(exc) == "idx_scoring_criteria_name":
            raise ValueError(
                "имя критерия набора уже занято в другом профиле, а в базе ещё глобальный индекс имён "
                "idx_scoring_criteria_name: сначала снимите его (DROP INDEX IF EXISTS idx_scoring_criteria_name)"
            ) from exc
        raise
    result["applied"] = True
    return result


def _violated_index(exc: pg_errors.UniqueViolation) -> str | None:
    diag = getattr(exc, "diag", None)
    return getattr(diag, "constraint_name", None)


def list_enabled_scoring_criteria(profile: str = ARTICLE_SCORING_PROFILE) -> list[dict]:
    """Активные критерии профиля, в порядке экрана.

    Умолчание, business: им оценивается статья ленты. Так его получают все читатели, не
    знающие профилей (конвейер, пакет для NL), и видят ровно
    прежний набор ленты, а не оба профиля с суммой 200 (ADR 0002)."""
    profile = check_profile(profile)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT *
            FROM scoring_criteria
            WHERE enabled = TRUE AND profile = %s
            ORDER BY sort_order, id
            """,
            (profile,),
        )
        return cur.fetchall()


def delete_scoring_criterion(criterion_id: int) -> None:
    """Мягкое удаление критерия (enabled=FALSE): не рвём FK на article_score_items.

    ПРОВЕРЯЕМ сумму весов оставшихся активных критериев. Раньше не проверяли, и это было
    миной: «Сохранить» сумму валидирует, а «Удалить» уходило в БД немедленно. Стоило уйти
    со страницы, не добив веса до 100, и следующая стадия скоринга падала ЦЕЛИКОМ, ещё
    до первой статьи.
    Заказчик 11.09 как раз просил «убрать старые, не актуальные» критерии: по одному это гарантированно ломало бы
    скоринг на каждом шаге. Теперь удаление отклоняется с понятным текстом, а привести
    профиль в порядок можно одним «Сохранить» (bulk), где веса пересчитываются вместе.

    Сумма, в профиле удаляемого критерия (сессия G): по всей таблице с двумя профилями
    она 200, и отказ получало бы любое удаление, а сломать сумму можно было бы соседу.
    """
    with get_connection() as conn:
        found = conn.execute("SELECT profile FROM scoring_criteria WHERE id = %s", (criterion_id,)).fetchone()
        if found is None:
            return  # нечего удалять: как и раньше, без ошибки
        row = conn.execute(
            "SELECT COALESCE(SUM(weight), 0) FROM scoring_criteria "
            "WHERE enabled = TRUE AND profile = %s AND id <> %s",
            (found[0], criterion_id),
        ).fetchone()
        remaining = round(float(row[0]), 2)
        if remaining != 100:
            raise ValueError(
                f"После удаления сумма весов активных критериев станет {remaining}, а нужна 100. "
                "Сначала перераспределите веса и нажмите «Сохранить» — тогда скоринг не упадёт."
            )
        conn.execute(
            "UPDATE scoring_criteria SET enabled = FALSE, updated_at = now() WHERE id = %s",
            (criterion_id,),
        )
        conn.commit()


def save_scoring_criteria(items: list[dict], profile: str = ARTICLE_SCORING_PROFILE) -> dict:
    """Bulk-сохранение профиля критериев (как кнопка «Сохранить» в мокапе).

    Валидирует сумму весов = 100. Существующие обновляются по id, новые вставляются,
    отсутствующие в списке: отключаются (soft delete, чтобы не рвать FK).

    Всё, в пределах профиля (сессия G): «Сохранить» вкладки «Бизнес-сигналы» раньше
    выключило бы весь «Технологический радар», которого нет в её списке. Критерий другого
    профиля по id не правится и в этот не переезжает: отказ, ничего не записано.
    """
    profile = check_profile(profile)
    total = round(sum(float(i.get("weight") or 0) for i in items), 2)
    if total != 100:
        raise ValueError(f"Сумма весов критериев должна быть 100, сейчас {total}")

    keep_ids: list[int] = []
    try:
        with get_connection() as conn:
            for it in items:
                payload = {
                    "profile": profile,
                    "name": it["name"],
                    "description": it.get("description"),
                    "weight": it["weight"],
                    "keywords_json": Json(it.get("keywords_json") or []),
                    "keywords_en_json": Json(it.get("keywords_en_json") or []),
                    "sort_order": it.get("sort_order") or 0,
                }
                if it.get("id"):
                    # Только активный критерий: экран показывает лишь активные, и выключенный id
                    # приходит только из устаревшей вкладки. Без этого условия вкладка, открытая
                    # до apply-scoring-preset, включала прежний набор обратно, а «выключить
                    # лишнее» ниже гасило новый: с ответом 200 (ревью PR #82).
                    cur = conn.execute(
                        """
                        UPDATE scoring_criteria SET name=%(name)s, description=%(description)s,
                            weight=%(weight)s, keywords_json=%(keywords_json)s,
                            keywords_en_json=%(keywords_en_json)s, sort_order=%(sort_order)s,
                            updated_at=now()
                        WHERE id=%(id)s AND profile=%(profile)s AND enabled
                        """,
                        {**payload, "id": int(it["id"])},
                    )
                    if cur.rowcount != 1:
                        raise ValueError(
                            f"Критерий id={int(it['id'])} не найден среди активных в профиле «{profile}»: "
                            "список на экране устарел (набор меняли после его открытия) — обновите страницу."
                        )
                    keep_ids.append(int(it["id"]))
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO scoring_criteria (profile, name, description, weight, keywords_json,
                                                      keywords_en_json, enabled, sort_order)
                        VALUES (%(profile)s, %(name)s, %(description)s, %(weight)s, %(keywords_json)s,
                                %(keywords_en_json)s, TRUE, %(sort_order)s)
                        ON CONFLICT (profile, name) DO UPDATE SET description=EXCLUDED.description,
                            weight=EXCLUDED.weight, keywords_json=EXCLUDED.keywords_json,
                            keywords_en_json=EXCLUDED.keywords_en_json, enabled=TRUE, updated_at=now()
                        RETURNING id
                        """,
                        payload,
                    )
                    keep_ids.append(int(cur.fetchone()[0]))
            conn.execute(
                "UPDATE scoring_criteria SET enabled=FALSE, updated_at=now() "
                "WHERE profile = %s AND id <> ALL(%s)",
                (profile, keep_ids),
            )
            conn.commit()
    except pg_errors.UniqueViolation as exc:
        if _violated_index(exc) == "idx_scoring_criteria_name":
            raise ValueError(
                "Такое имя уже есть у критерия другого профиля, а в базе ещё глобальный индекс имён "
                "idx_scoring_criteria_name. Переименуйте критерий или снимите индекс."
            ) from exc
        raise ValueError("Два критерия профиля с одним именем: имена в профиле должны различаться.") from exc
    return {"saved": len(items), "weight_sum": total, "profile": profile}


def insert_ai_run(rec: dict) -> None:
    # job_id по умолчанию NULL: для локального пути и старых вызовов (не дедуплицируются).
    # ON CONFLICT DO NOTHING (баг H1/T2): повторное применение результата задачи не двоит
    # биллинг: одна строка на (job_id, article_id, stage).
    rec = {"job_id": None, **rec}
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO ai_processing_runs
              (job_id, article_id, stage, provider, model, language, input_tokens, output_tokens,
               total_tokens, cost_usd, status, error_message)
            VALUES (%(job_id)s, %(article_id)s, %(stage)s, %(provider)s, %(model)s, %(language)s,
                    %(input_tokens)s, %(output_tokens)s, %(total_tokens)s, %(cost_usd)s,
                    %(status)s, %(error_message)s)
            ON CONFLICT (job_id, article_id, stage) DO NOTHING
            """,
            rec,
        )
        conn.commit()


def record_radar_ai_usage(job_id: int | None, usage: list[dict] | None) -> int:
    """Расход ИИ прогона радара: в ai_processing_runs, строка на (стадия, модель).

    Статьи у строки нет (article_id NULL), поэтому уникальный ключ (job_id, article_id, stage)
    повтор не ловит: повторное применение итога той же задачи сначала снимает её прежние строки
    радара: одной транзакцией. Стоимость: по ставке модели плюс плата за вызовы web_search."""
    rows = [row for row in usage or [] if int(row.get("calls") or 0) > 0]
    with get_connection() as conn:
        if job_id is not None:
            conn.execute("DELETE FROM ai_processing_runs WHERE job_id = %s AND stage LIKE 'radar\\_%%'", (job_id,))
        for row in rows:
            price_in, price_out = config.price_for_model(row.get("model"))
            input_tokens = int(row.get("input_tokens") or 0)
            output_tokens = int(row.get("output_tokens") or 0)
            cost = (input_tokens * price_in + output_tokens * price_out) / 1_000_000
            cost += int(row.get("web_search_calls") or 0) * config.SIGNAL_WEB_SEARCH_USD_PER_CALL
            conn.execute(
                """
                INSERT INTO ai_processing_runs
                  (job_id, article_id, stage, provider, model, language, input_tokens, output_tokens,
                   total_tokens, cost_usd, status, error_message)
                VALUES (%s, NULL, %s, 'openai', %s, NULL, %s, %s, %s, %s, 'ok', %s)
                """,
                (job_id, row["stage"], row.get("model") or None, input_tokens, output_tokens,
                 input_tokens + output_tokens, round(cost, 6),
                 f"calls={int(row.get('calls') or 0)} web_search={int(row.get('web_search_calls') or 0)}"),
            )
        conn.commit()
    return len(rows)


def record_reprint_usage(job_id: int | None, verdicts: list[dict] | None) -> int:
    """Расход судьи перепечаток: в ai_processing_runs, строка на модель за каждый принятый итог задачи.

    Стадия reprint_review, статьи у строки нет: пара это две статьи. Частичный итог (воркер остановлен) и итог
    остатка той же задачи судят разные пары, поэтому строки складываются, а не заменяют друг друга. Вердикт без
    токенов (сборка NL до 03.11, сбой пары) в счёт не идёт: старая сборка в учёт не писала и раньше."""
    totals: dict[str, list[int]] = {}
    for verdict in verdicts or []:
        if verdict.get("error") or "input_tokens" not in verdict:
            continue
        try:
            input_tokens = max(0, int(verdict.get("input_tokens") or 0))
            output_tokens = max(0, int(verdict.get("output_tokens") or 0))
        except (TypeError, ValueError):
            continue  # итог воркера, недоверенный вход: битая строка не валит запись пометок
        row = totals.setdefault(str(verdict.get("model") or ""), [0, 0, 0])
        row[0] += 1
        row[1] += input_tokens
        row[2] += output_tokens
    if not totals:
        return 0
    with get_connection() as conn:
        for model, (pairs, input_tokens, output_tokens) in totals.items():
            price_in, price_out = config.price_for_model(model)
            cost = (input_tokens * price_in + output_tokens * price_out) / 1_000_000
            conn.execute(
                """
                INSERT INTO ai_processing_runs
                  (job_id, article_id, stage, provider, model, language, input_tokens, output_tokens,
                   total_tokens, cost_usd, status, error_message)
                VALUES (%s, NULL, 'reprint_review', 'openai', %s, NULL, %s, %s, %s, %s, 'ok', %s)
                """,
                (job_id, model or None, input_tokens, output_tokens, input_tokens + output_tokens,
                 round(cost, 6), f"pairs={pairs}"),
            )
        conn.commit()
    return len(totals)


def ai_cost_report() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT stage, COALESCE(language, 'unknown') AS language,
                   COUNT(*) AS runs,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(total_tokens) AS total_tokens,
                   ROUND(SUM(cost_usd)::numeric, 6) AS cost_usd,
                   ROUND(AVG(total_tokens)::numeric, 2) AS avg_tokens_per_run
            FROM ai_processing_runs
            GROUP BY stage, COALESCE(language, 'unknown')
            ORDER BY stage, language
            """
        )
        return cur.fetchall()


def ai_article_cost_report(limit: int = 20, complete_only: bool = True) -> list[dict]:
    """Расход ИИ на одну статью ленты по её стадиям: «Отбор», суть и перевод заголовка. Гейт, тег и балл статьи сняты
    08.11, их строки прошлых статей в отчёт не входят.

    complete_only=True: только статьи с удачными «Отбором» и сутью, то есть прошедшие весь путь статьи. «Карточка» и
    баллы идут на сигнал статьи (стадии signal_card и signal_scores) и в этот отчёт не входят.
    """
    stages_filter = ("HAVING bool_or(r.stage = 'otbor' AND r.status = 'ok')"
                     " AND bool_or(r.stage = 'summary' AND r.status = 'ok')") if complete_only else ""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT
                a.id AS article_id,
                a.title,
                a.language,
                COUNT(*) FILTER (WHERE r.status = 'ok') AS runs,
                COUNT(DISTINCT r.stage) FILTER (WHERE r.status = 'ok') AS stages,
                SUM(r.input_tokens) FILTER (WHERE r.status = 'ok') AS input_tokens,
                SUM(r.output_tokens) FILTER (WHERE r.status = 'ok') AS output_tokens,
                SUM(r.total_tokens) FILTER (WHERE r.status = 'ok') AS total_tokens,
                ROUND(SUM(r.cost_usd) FILTER (WHERE r.status = 'ok')::numeric, 6) AS cost_usd
            FROM ai_processing_runs r
            JOIN articles a ON a.id = r.article_id
            WHERE r.stage IN ('otbor', 'summary', 'translation')
            GROUP BY a.id, a.title, a.language
            {stages_filter}
            ORDER BY cost_usd DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


# Членство в выпуске: одно правило у сборщика (digest_candidates), черновика
# (digest_items_by_article_ids) и состава выпуска (issue_composition): иначе выгрузка и состав,
# который заберёт потребитель выпуска, разошлись бы.
def _issue_article_sql(selected_sql: str) -> str:
    """Статья идёт в выпуск: выбрана «в дайджест», видна, как в ленте, и не из будущего.

    Видимость: та же, что у ленты (feed_window.visible_sql): архивный источник, перепечатка (в
    выпуске нужна одна копия новости: заказчик 22.08: «одну заберу в дайджест, вторую отмечу как
    дубликат»), отсев гейтом и помеченное на удаление в выпуск не идут (последнего до 23.09 не
    было: статья, помеченная на удаление, пряталась из ленты, но оставалась в выпуске)."""
    return (
        f"{selected_sql} AND {visible_sql()}"
        " AND (a.published_at IS NULL OR a.published_at <= now() + interval '2 days')"
    )


def _issue_signal_sql(selected_sql: str) -> str:
    """Карточка радара или материала коллеги идёт в выпуск: выбрана «в дайджест», не дубль.

    Сигнал ленты в выпуске был бы второй копией статьи (#95): статья идёт сама. Материал коллеги (#131): свой
    сигнал, статьи у него нет: без него выбор редактора молча терялся (ревью PR #155). Зрелости «Отклонено» здесь нет
    (29.09): это оценка модели, а экран радара показывает такие карточки с кнопкой «В дайджест», и
    повторная находка перезаписывает зрелость выбранной карточки. Выбор человека: членство."""
    return (f"{selected_sql} AND sig.merged_into_signal_id IS NULL"
            f" AND ({radar_origin_sql('sig')} OR sig.origin = '{SIGNAL_ORIGIN_MATERIAL}')")


def _issue_signal_member_sql(selected_sql: str) -> str:
    """Карточка в составе выпуска без фильтров экрана: _issue_signal_sql и порог балла 0, как у сборщика с порогом 0.
    Одно условие у состава (issue_composition_by_rule) и у числа архива (feed_archive_months, LOW-4 ревью PR #155)."""
    return f"{_issue_signal_sql(selected_sql)} AND sig.score >= 0"


def digest_candidates(month: str | None = None, limit: int = 20, min_score: float = 60,
                      max_score: float | None = None,
                      search: str | None = None, top_tag: str | None = None) -> list[dict]:
    """Статьи и карточки радара, отмеченные «в дайджест» (общая отметка, #111), выпуск месяца
    без привязки к учётке.

    ``month`` опционален: пусто/None: фильтр периода снимается, возвращаются все
    выбранные (превью совпадает с экспортом). Будущие публикации всегда исключены.

    Тема и балл статьи берутся у её публикации ленты (publication_joins): у статьи с сигналом ленты его тема и балл, у
    статьи без сигнала прежние тег и балл. По ним порог, фильтр по теме, поиск и порядок; score_band: ступень второго
    вызова «Карточки» для подписи балла (processing.digest).
    """
    params: dict = {"min_score": min_score, "limit": limit}
    month_clause = ""
    max_score_clause = ""
    signal_max_score_clause = ""
    search_clause = ""
    tag_clause = ""
    if month:
        # Месяц, тем же выражением, что у окна ленты: что видно в ленте за месяц, то и
        # попадает в выпуск этого месяца (feed_window.period_month_sql).
        month_clause = f"AND {period_month_sql('a')} = %(month)s"
        params["month"] = month
    if max_score is not None:
        max_score_clause = "AND COALESCE(pub.total_score, 0) <= %(max_score)s"
        signal_max_score_clause = "AND sig.score <= %(max_score)s"
        params["max_score"] = max_score
    if search:
        search_clause = """
              AND (
                   COALESCE(c.title_ru, a.title) ILIKE %(search)s
                OR COALESCE(c.summary, '') ILIKE %(search)s
                OR COALESCE(pub.tag_name, '') ILIKE %(search)s
                OR COALESCE(pub.parent_tag_name, '') ILIKE %(search)s
              )
        """
        params["search"] = f"%{search}%"
    if top_tag:
        tag_clause = """
              AND (
                   pub.tag_name = %(top_tag)s
                OR pub.parent_tag_name = %(top_tag)s
              )
        """
        params["top_tag"] = top_tag
    # Текст закрытого выпуска не меняется (решение владельца 30.09): правки после закрытия месяца в него не идут.
    issue_window = feed_window.current()
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id, COALESCE(corr.corrected_title, c.title_ru, a.title) AS title, a.url, a.published_at,
                   a.language, a.image_url,
                   s.name AS source_name,
                   COALESCE(corr.corrected_thesis, c.summary) AS summary, TRUE AS selected_for_digest,
                   pub.total_score, pub.score_band,
                   pub.tag_name, pub.parent_tag_name
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            {publication_joins('a')}
            {_article_corrections_lateral(issue_window)}
            WHERE {_issue_article_sql(marks.article_marked_sql('a.id', 'digest'))}
              AND COALESCE(pub.total_score, 0) >= %(min_score)s
              {max_score_clause}
              {month_clause}
              {search_clause}
              {tag_clause}
            ORDER BY pub.total_score DESC NULLS LAST,
                     a.published_at DESC NULLS LAST
            LIMIT %(limit)s
            """,
            params,
        )
        article_rows = cur.fetchall()
        signal_params = dict(params)
        signal_month_clause = ""
        signal_search_clause = ""
        signal_tag_clause = ""
        if month:
            # Месяц карточки, месяц её поступления на радар по Москве: эту дату показывает
            # экран радара, и она не меняется. До 29.09 месяц брался по дате лучшей ссылки, а
            # без неё, по last_seen_at, который сдвигает каждая повторная находка: радар
            # 1-4 числа уносил выбранную карточку в выпуск следующего месяца, а карточку со
            # старой ссылкой выпуск месяца, когда её нашли, не видел вовсе. Выражение: общее с
            # окном отметки «в дайджест» на радаре (feed_window.signal_month_sql).
            signal_month_clause = f"AND {signal_month_sql('sig')} = %(month)s"
        if search:
            signal_search_clause = """
              AND (
                   COALESCE(sig.title_ru, sig.title) ILIKE %(search)s
                OR COALESCE(sig.summary, '') ILIKE %(search)s
                OR COALESCE(sig.thesis, '') ILIKE %(search)s
                OR COALESCE(sig.theme, '') ILIKE %(search)s
              )
            """
        if top_tag:
            signal_tag_clause = "AND sig.theme = %(top_tag)s"
        # Текст закрытого выпуска не меняется (решение владельца 30.09): у карточки закрытого
        # месяца правки коллег: только поданные до его закрытия, ссылка: лучшая из найденных
        # до закрытия (пока такие есть). Окно: то же, что у отказа отметке «в дайджест»
        # (api._guard_signal_digest_month).
        link_before_close = issue_window.before_close_sql(signal_month_sql("sig"), "e.created_at")
        cur.execute(
            f"""
            SELECT sig.id,
                   COALESCE(corr.corrected_title, sig.title_ru, sig.title) AS title,
                   COALESCE(best_evidence.source_url, '') AS url,
                   -- Без даты ссылки — поступление на радар («Поступил» на экране), а не время
                   -- последней находки: его сдвигает каждый прогон радара (30.09).
                   COALESCE(best_evidence.published_at, sig.first_seen_at) AS published_at,
                   -- День в карточке выпуска. Поступление — по Москве, как на экране и как месяц
                   -- выпуска (signal_month_sql); сессия базы в UTC, и 20.09 в 00:15 МСК там ещё
                   -- 19.09 (ревью #88). Дата ссылки — как раньше, днём по поясу сессии.
                   CASE WHEN best_evidence.published_at IS NOT NULL THEN best_evidence.published_at::date
                        ELSE (sig.first_seen_at AT TIME ZONE 'Europe/Moscow')::date END AS published_on,
                   'mixed' AS language,
                   '' AS image_url,
                   COALESCE(best_evidence.publisher, CASE WHEN sig.origin = '{SIGNAL_ORIGIN_MATERIAL}'
                            THEN 'Материал коллеги' ELSE 'Технологический радар' END) AS source_name,
                   COALESCE(corr.corrected_thesis, sig.summary, sig.thesis, '') AS summary,
                   TRUE AS selected_for_digest,
                   sig.score AS total_score,
                   sig.maturity AS score_label,
                   sig.theme AS tag_name,
                   NULL AS parent_tag_name,
                   'signal' AS item_type
            FROM signals sig
            LEFT JOIN LATERAL (
              SELECT e.source_url, e.publisher, e.published_at
              FROM signal_evidence e
              WHERE e.signal_id = sig.id
              ORDER BY {link_before_close} DESC,
                       e.strength DESC, e.published_at DESC NULLS LAST, e.created_at DESC
              LIMIT 1
            ) best_evidence ON TRUE
            {_signal_corrections_lateral("sig", issue_window)}
            WHERE {_issue_signal_sql(marks.signal_marked_sql('sig.id', 'digest'))}
              AND sig.score >= %(min_score)s
              {signal_max_score_clause}
              {signal_month_clause}
              {signal_search_clause}
              {signal_tag_clause}
            ORDER BY sig.score DESC NULLS LAST,
                     COALESCE(best_evidence.published_at, sig.first_seen_at) DESC NULLS LAST
            LIMIT %(limit)s
            """,
            signal_params,
        )
        signal_rows = cur.fetchall()
        combined = [*article_rows, *signal_rows]
        combined.sort(
            key=lambda row: (
                float(row.get("total_score") or 0),
                row.get("published_at") or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )
        return combined[:limit]


def save_monthly_digest(month: str, title: str, items: list[dict], status: str = "draft") -> dict:
    """Сохранить черновик выпуска месяца и заменить его упорядоченный список статей.

    Черновик один на месяц (#111): строка без учётки (уникальный индекс под неё в схеме).
    Личные черновики (user_id задан), архив только для чтения: их не читают и не пишут."""
    with get_connection() as conn:
        digest = conn.execute(
            """
            INSERT INTO monthly_digests (user_id, month, title, status)
            VALUES (NULL, %s, %s, %s)
            ON CONFLICT (month) WHERE user_id IS NULL DO UPDATE SET
                title = EXCLUDED.title,
                status = EXCLUDED.status,
                updated_at = now()
            RETURNING id, month, title, status
            """,
            (month, title, status),
        ).fetchone()
        replace_monthly_digest_items(conn, digest[0], items)
        conn.commit()
        return {"id": digest[0], "month": digest[1], "title": digest[2], "status": digest[3], "items": len(items)}


def replace_monthly_digest_items(conn, digest_id: int, items: list[dict]) -> None:
    """Заменить упорядоченный список статей черновика в транзакции `conn` (без commit)."""
    conn.execute("DELETE FROM monthly_digest_items WHERE digest_id = %s", (digest_id,))
    for idx, item in enumerate(items, start=1):
        conn.execute(
            """
            INSERT INTO monthly_digest_items (digest_id, article_id, sort_order, section, editor_note)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (digest_id, int(item["article_id"]), idx, item.get("section"), item.get("editor_note")),
        )


def get_monthly_digest(month: str) -> dict | None:
    """Черновик выпуска месяца (общий, #111) с упорядоченным списком статей."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT id, month, title, status, created_at, updated_at
            FROM monthly_digests
            WHERE month = %s AND user_id IS NULL
            """,
            (month,),
        )
        digest = cur.fetchone()
        if digest is None:
            return None
        cur.execute(
            """
            SELECT article_id, sort_order, section, editor_note
            FROM monthly_digest_items
            WHERE digest_id = %s
            ORDER BY sort_order, id
            """,
            (digest["id"],),
        )
        return {**digest, "items": cur.fetchall()}


def digest_item_kinds(article_ids: Sequence[int], signal_ids: Sequence[int]) -> tuple[dict[int, str], dict[int, str]]:
    """Категория элементов выпуска для плашки «Технология / Бизнес» (решение владельца 23.10). У статьи: категория
    её сигнала ленты (его ставят «Отбор» и мост ленты), у находки радара, своя; находка радара без категории:
    технология, как на экране радара. Статья без сигнала ленты категории не получает."""
    by_article: dict[int, str] = {}
    by_signal: dict[int, str] = {}
    with get_connection() as conn:
        if article_ids:
            rows = conn.execute(
                """
                SELECT DISTINCT ON (origin_article_id) origin_article_id, signal_category
                FROM signals
                WHERE origin_article_id = ANY(%s) AND signal_category IS NOT NULL
                ORDER BY origin_article_id, merged_into_signal_id NULLS FIRST, id
                """,
                (list(article_ids),),
            ).fetchall()
            by_article = {int(article_id): category for article_id, category in rows}
        if signal_ids:
            rows = conn.execute(
                "SELECT id, COALESCE(signal_category, 'technology') FROM signals WHERE id = ANY(%s)",
                (list(signal_ids),),
            ).fetchall()
            by_signal = {int(signal_id): category for signal_id, category in rows}
    return by_article, by_signal


_ISSUE_MATERIAL_COLUMNS = (
    "s.id AS signal_id, s.signal_category, s.theme, s.event_type, s.technology_maturity, s.event_date,"
    " s.event_date_precision, s.oilfield_relevance, s.score, s.score_profile, s.score_items_json, s.criteria_snapshot,"
    " s.issue_label, s.hypothesis, s.hypothesis_set_at, s.hypothesis_author,"
    " s.card_set_by->>'hypothesis' AS hypothesis_set_by, s.issue_picture"
)
def issue_material_fields(article_ids: Sequence[int],
                          signal_ids: Sequence[int]) -> tuple[dict[int, dict], dict[int, dict]]:
    """Поля материала выпуска (#161, ADR 0006) с его сигнала: категория, тема, тип события и зрелость «Карточки», дата
    события, связь с нефтесервисом, балл с разбивкой. У статьи: её сигнала ленты (он у статьи один, ключ
    feed:<статья>; при категории без значения тоже он, в отличие от плашки digest_item_kinds), у находки радара и
    материала коллеги: свои. Статьи без сигнала ленты (собрана до 01.08.2026) в первом словаре нет."""
    by_article: dict[int, dict] = {}
    by_signal: dict[int, dict] = {}
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        if article_ids:
            cur.execute(
                f"""
                SELECT DISTINCT ON (s.origin_article_id) s.origin_article_id, {_ISSUE_MATERIAL_COLUMNS}
                FROM signals s
                WHERE s.origin_article_id = ANY(%s) AND s.origin = '{SIGNAL_ORIGIN_FEED}'
                ORDER BY s.origin_article_id, {feed_signal_order_sql('s')}
                """,
                (list(article_ids),),
            )
            by_article = {int(row.pop("origin_article_id")): row for row in cur.fetchall()}
        if signal_ids:
            cur.execute(f"SELECT {_ISSUE_MATERIAL_COLUMNS} FROM signals s WHERE s.id = ANY(%s)", (list(signal_ids),))
            by_signal = {int(row["signal_id"]): row for row in cur.fetchall()}
    return by_article, by_signal


def digest_items_by_article_ids(article_ids: list[int], *, month: str | None = None) -> list[dict]:
    """Детали статей сохранённого черновика выпуска по списку id (порядок сохраняется).

    Только статьи, которые ДО СИХ ПОР отмечены «в дайджест»: черновик задаёт порядок, но не
    членство. До 29.09 статья, снятая после сохранения («Из дайджеста» на экране выпуска или
    другой статус в ленте), пропадала из очереди на экране, а превью и выгрузка брали её из
    черновика, пока его не пересохранят.

    `month` («ГГГГ-ММ»): только статьи этого месяца, тем же выражением, что у сборщика
    (digest_candidates) и окна ленты. Проверка месяца стоит на сохранении черновика (с 29.09),
    а черновик, сохранённый до неё через «Все месяцы», хранит и статьи других месяцев (ревью #84).
    Тема и балл берутся у публикации ленты, как у сборщика (publication_joins).
    """
    if not article_ids:
        return []
    member_clause = ""
    member_params: list = []
    if month:
        member_clause += f" AND {period_month_sql('a')} = %s"
        member_params.append(month)
    order_case = "CASE " + " ".join(f"WHEN a.id = %s THEN {index}" for index, _ in enumerate(article_ids, start=1)) + " END"
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.id, COALESCE(corr.corrected_title, c.title_ru, a.title) AS title, a.url, a.published_at,
                   a.language, a.image_url,
                   s.name AS source_name,
                   COALESCE(corr.corrected_thesis, c.summary) AS summary,
                   pub.total_score, pub.score_band,
                   pub.tag_name, pub.parent_tag_name
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            {publication_joins('a')}
            {_article_corrections_lateral(feed_window.current())}
            WHERE a.id = ANY(%s)
              -- Членство и видимость — те же, что у сборщика (_issue_article_sql): выгрузка = то,
              -- что видно. Решение владельца 23.09 «одно правило везде»: статья, которую
              -- перепроверка потом пометила на удаление, выпадает и из сохранённого выпуска.
              AND {_issue_article_sql(marks.article_marked_sql('a.id', 'digest'))}
              {member_clause}
            ORDER BY {order_case}
            """,
            [article_ids, *member_params, *article_ids],
        )
        return cur.fetchall()


def issue_composition(month: str, *, conn=None) -> dict[str, list[int]]:
    """Состав выпуска месяца без привязки к учётке: номера статей и карточек радара.

    Тот же выпуск, что собирают экран, выгрузка и команды на сервере (processing.digest.
    build_digest_content с порогом 0): статьи черновика месяца, которые до сих пор отмечены «в
    дайджест», а нет таких: все отмеченные статьи месяца; плюс отмеченные карточки радара
    месяца. Номера: по возрастанию. `conn`: читать в чужой транзакции (сверка переноса)."""
    with (nullcontext(conn) if conn is not None else get_connection()) as c:
        row = c.execute(
            """
            SELECT COALESCE(array_agg(i.article_id ORDER BY i.sort_order, i.id), '{}')
            FROM monthly_digests d
            JOIN monthly_digest_items i ON i.digest_id = d.id
            WHERE d.month = %s AND d.user_id IS NULL
            """,
            (month,),
        ).fetchone()
        return issue_composition_by_rule(
            c, month,
            article_selected_sql=marks.article_marked_sql("a.id", "digest"),
            signal_selected_sql=marks.signal_marked_sql("sig.id", "digest"),
            draft_article_ids=[int(article_id) for article_id in row[0]],
        )


def issue_composition_by_rule(conn, month: str, *, article_selected_sql: str, signal_selected_sql: str,
                              draft_article_ids: Sequence[int]) -> dict[str, list[int]]:
    """Состав выпуска месяца по заданному правилу отметки «в дайджест» (SQL-условие на a.id /
    sig.id). Общее правило: issue_composition; перенос сверяет им же прежнее правило учётки."""
    article_rows = conn.execute(
        f"""
        SELECT a.id
        FROM articles a
        JOIN sources s ON s.id = a.source_id
        LEFT JOIN article_cards c ON c.article_id = a.id
        WHERE {_issue_article_sql(article_selected_sql)}
          AND {period_month_sql('a')} = %s
        """,
        (month,),
    ).fetchall()
    selected = {int(row[0]) for row in article_rows}
    from_draft = {article_id for article_id in draft_article_ids if article_id in selected}
    signal_rows = conn.execute(
        f"""
        SELECT sig.id
        FROM signals sig
        WHERE {_issue_signal_member_sql(signal_selected_sql)}
          AND {signal_month_sql('sig')} = %s
        """,
        (month,),
    ).fetchall()
    return {
        "article_ids": sorted(from_draft or selected),
        "signal_ids": sorted(int(row[0]) for row in signal_rows),
    }


def max_article_id() -> int | None:
    """Return the current maximum article id, or None if the table is empty."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(id) FROM articles")
        row = cur.fetchone()
        return row[0] if row else None


def get_articles_needing_summary_after(after_id: int, limit: int = 20) -> list[dict]:
    """Articles that have no summary yet and whose id > after_id (streaming checkpoint). Решение гейта и «Отбора»
    (#130), конвейеру: статью, которую держит «Отбор», он не обрабатывает (held_by_otbor)."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category, c.relevant,
                   {feed_window.otbor_decision_sql('a')} AS otbor_decision
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE a.id > %s AND c.summary IS NULL
            ORDER BY a.id ASC
            LIMIT %s
            """,
            (after_id, limit),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
#  Обратная связь человека: оценки + комментарий (требование владельца 12.09)
# ---------------------------------------------------------------------------
# Быстрые причины в один клик. Набор выведен из того, за что заказчик РЕАЛЬНО ставит
# «Шум»: замер 03.09: 239 пометок, из них 34 оказались не «не по теме», а обрывками.
# Без причины эти два случая сливаются, и обучать гейт на такой смеси нельзя: система
# выучит «короткие статьи нерелевантны» вместо «эта тема не наша».
FEEDBACK_REASONS = (
    "off_topic",        # не наша тема
    "incomplete_text",  # обрывок, судить не о чем
    "duplicate",        # уже было
    "bad_translation",  # смысл искажён переводом
    "bad_source",       # дело не в статье, а в источнике
    "good",             # годный сигнал: положительный пример тоже обучает
    "wrong_block",      # не тот блок: это технологический сигнал, а не бизнес
    "other",
)
# Причины про сам сигнал, а не про издание. «Не тот блок» (встреча с заказчиком 21.09,
# решение 5), разметка на будущий маршрут «бизнес / технология»: сейчас она только
# копится, как «Не тот блок» в радаре. У ОС по источнику блока нет: там её не бывает.
ARTICLE_ONLY_FEEDBACK_REASONS = frozenset({"wrong_block"})


def save_feedback_entry(user_id: int, *, article_id: int | None = None,
                        source_id: int | None = None, reason: str | None = None,
                        usefulness: int | None = None, translation: int | None = None,
                        source_quality: int | None = None,
                        comment: str | None = None, clear_reason: bool = False,
                        clear_comment: bool = False) -> dict:
    """Сохранить или обновить карточку обратной связи.

    Одна карточка на пару «человек × сигнал»: повторное сохранение ПРАВИТ её, а не
    плодит дубли: иначе обучающая выборка окажется перекошена теми статьями, которые
    человек открывал чаще.

    `source_id` при ОС о сигнале подставляется из статьи, даже если не передан: так
    накопленное сворачивается по источнику без прохода по всей ленте.

    `clear_reason` снимает причину (повторный клик по чипу) и больше ничего не трогает;
    `clear_comment` так же стирает комментарий (очищенное поле).
    """
    if article_id is None and source_id is None:
        raise ValueError("Обратная связь должна быть привязана к сигналу или к источнику")
    if clear_reason and reason is not None:
        raise ValueError("Снять причину и задать новую одним запросом нельзя")
    if reason is not None and reason not in FEEDBACK_REASONS:
        raise ValueError(f"Неизвестная причина: {reason}")
    if reason in ARTICLE_ONLY_FEEDBACK_REASONS and article_id is None:
        raise ValueError(f"Причина «{reason}» относится к сигналу, а не к источнику")
    for name, value in (("usefulness", usefulness), ("translation", translation),
                        ("source_quality", source_quality)):
        if value is not None and not 1 <= int(value) <= 5:
            raise ValueError(f"Оценка «{name}» должна быть от 1 до 5, получено {value}")

    with get_connection() as conn:
        if article_id is not None and source_id is None:
            row = conn.execute("SELECT source_id FROM articles WHERE id = %s", (article_id,)).fetchone()
            if row is None:
                raise ValueError(f"Статья {article_id} не найдена")
            source_id = int(row[0])

        # COALESCE на UPDATE: частичное сохранение (поставил только оценку) не должно
        # стирать уже написанный комментарий: правка карточки идёт по кусочкам. Причину и
        # комментарий снимают явно (clear_reason, clear_comment): через COALESCE их было не
        # снять вовсе: ошибочный чип, в том числе «Не тот блок», и стёртый текст возвращались.
        conflict = ("(user_id, article_id) WHERE article_id IS NOT NULL" if article_id is not None
                    else "(user_id, source_id) WHERE article_id IS NULL AND source_id IS NOT NULL")
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            INSERT INTO feedback_entries
              (user_id, article_id, source_id, reason, usefulness, translation, source_quality, comment)
            VALUES (%(user_id)s, %(article_id)s, %(source_id)s, %(reason)s,
                    %(usefulness)s, %(translation)s, %(source_quality)s, %(comment)s)
            ON CONFLICT {conflict} DO UPDATE SET
              source_id      = COALESCE(EXCLUDED.source_id, feedback_entries.source_id),
              reason         = CASE WHEN %(clear_reason)s THEN NULL
                                    ELSE COALESCE(EXCLUDED.reason, feedback_entries.reason) END,
              usefulness     = COALESCE(EXCLUDED.usefulness, feedback_entries.usefulness),
              translation    = COALESCE(EXCLUDED.translation, feedback_entries.translation),
              source_quality = COALESCE(EXCLUDED.source_quality, feedback_entries.source_quality),
              comment        = CASE WHEN %(clear_comment)s THEN NULL
                                    ELSE COALESCE(EXCLUDED.comment, feedback_entries.comment) END,
              updated_at     = now()
            RETURNING *
            """,
            {"user_id": user_id, "article_id": article_id, "source_id": source_id,
             "reason": reason, "usefulness": usefulness, "translation": translation,
             "source_quality": source_quality, "comment": comment,
             "clear_reason": bool(clear_reason), "clear_comment": bool(clear_comment)},
        )
        saved = cur.fetchone()
        conn.commit()
        return saved


def get_feedback_entry(user_id: int, article_id: int | None = None,
                       source_id: int | None = None) -> dict | None:
    """Карточка ОС этого человека по сигналу или по источнику."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        if article_id is not None:
            cur.execute(
                "SELECT * FROM feedback_entries WHERE user_id = %s AND article_id = %s",
                (user_id, article_id),
            )
        else:
            cur.execute(
                "SELECT * FROM feedback_entries "
                "WHERE user_id = %s AND source_id = %s AND article_id IS NULL",
                (user_id, source_id),
            )
        return cur.fetchone()


def feedback_training_set(limit: int = 500, reason: str | None = None) -> list[dict]:
    """Выгрузка обратной связи для обучения агентов.

    Отдаёт то, чего у модели нет из самой статьи: вердикт человека, его оценки и текст.
    Заголовок и суть приложены, чтобы набор был самодостаточным: агент читает его без
    доступа к БД (ИИ на проде считается на внешнем воркере, у него БД нет).
    """
    clauses, params = ["f.comment IS NOT NULL OR f.reason IS NOT NULL"], []
    if reason:
        clauses.append("f.reason = %s")
        params.append(reason)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"""
            SELECT f.id, f.reason, f.usefulness, f.translation, f.source_quality,
                   f.comment, f.created_at,
                   a.id AS article_id, a.title, a.url,
                   c.title_ru, c.summary,
                   s.id AS source_id, s.name AS source_name
            FROM feedback_entries f
            LEFT JOIN articles a ON a.id = f.article_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN sources s ON s.id = f.source_id
            WHERE {" AND ".join(clauses)}
            ORDER BY f.created_at DESC
            LIMIT %s
            """,
            [*params, limit],
        )
        return cur.fetchall()


def feedback_source_summary(limit: int = 300) -> list[dict]:
    """Свод оценок по источникам: на что человек жалуется чаще всего.

    Это прямой ответ на «почему не идут технологические новости» и на запрос заказчика
    убрать мёртвые источники: видно, какие источники люди системно метят как негодные.
    """
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT s.id AS source_id, s.name AS source_name,
                   count(*) AS entries,
                   round(avg(f.usefulness)::numeric, 2)     AS avg_usefulness,
                   round(avg(f.translation)::numeric, 2)    AS avg_translation,
                   round(avg(f.source_quality)::numeric, 2) AS avg_source_quality,
                   count(*) FILTER (WHERE f.reason = 'off_topic')       AS off_topic,
                   count(*) FILTER (WHERE f.reason = 'incomplete_text') AS incomplete_text,
                   count(*) FILTER (WHERE f.reason = 'duplicate')       AS duplicate,
                   count(*) FILTER (WHERE f.reason = 'bad_translation') AS bad_translation,
                   count(*) FILTER (WHERE f.reason = 'good')            AS good
            FROM feedback_entries f
            JOIN sources s ON s.id = f.source_id
            GROUP BY s.id, s.name
            ORDER BY count(*) DESC, s.name
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()
