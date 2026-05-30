"""CRUD and query helpers for sources, articles, auth and AI processing."""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg.types.json import Json

from oiltech_digest import auth, config
from oiltech_digest.db.connection import get_connection

# ---------------------------------------------------------------------------
#  sources
# ---------------------------------------------------------------------------

def upsert_source(rec: dict) -> str:
    """Вставить/обновить источник по name (естественный ключ).

    Ключ — (name, source_type): один бренд может иметь сайт и Telegram-канал с
    одинаковым именем, но разным типом (это разные источники). При конфликте
    обновляются только описательные поля (url, category, priority). rss_url /
    parse_strategy / enabled НЕ трогаем — ими управляет discover-rss.
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
    """Источники-кандидаты на автообнаружение RSS (не Telegram)."""
    query = "SELECT * FROM sources WHERE enabled = TRUE AND parse_strategy <> 'telegram'"
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
                   update_frequency: str | None = None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO sources (name, source_type, url, rss_url, enabled,
                                 parse_strategy, category, update_frequency, priority)
            VALUES (%s, %s, %s, %s, TRUE, 'rss', %s, %s, %s)
            ON CONFLICT (name, source_type) DO UPDATE SET
                url = EXCLUDED.url,
                rss_url = EXCLUDED.rss_url,
                enabled = TRUE,
                parse_strategy = 'rss',
                category = EXCLUDED.category,
                update_frequency = EXCLUDED.update_frequency,
                priority = EXCLUDED.priority,
                updated_at = now()
            RETURNING id
            """,
            (name, source_type, url, rss_url, category, update_frequency, priority),
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


def article_exists(url: str) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT 1 FROM articles WHERE url = %s", (url,)).fetchone()
        return row is not None


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

def create_user(email: str, password: str) -> dict:
    email = auth.normalize_email(email)
    salt_hex, password_hash = auth.hash_password(password)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        if cur.fetchone() is not None:
            raise ValueError("Пользователь с таким email уже существует")
        cur.execute(
            """
            INSERT INTO users (email, password_salt, password_hash)
            VALUES (%s, %s, %s)
            RETURNING id, email, created_at
            """,
            (email, salt_hex, password_hash),
        )
        user = cur.fetchone()
        conn.commit()
        return user


def authenticate_user(email: str, password: str) -> dict | None:
    email = auth.normalize_email(email)
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT id, email, password_salt, password_hash, created_at FROM users WHERE email = %s",
            (email,),
        )
        user = cur.fetchone()
        if user is None:
            return None
        if not auth.verify_password(password, user["password_salt"], user["password_hash"]):
            return None
        return user


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
            SELECT u.id, u.email, u.created_at
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


def count_users() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


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
#  articles
# ---------------------------------------------------------------------------

def insert_article(rec: dict) -> bool:
    """Вставить статью. Дубликаты по url игнорируются (ON CONFLICT DO NOTHING).
    Возвращает True, если строка реально вставлена."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO articles (source_id, title, url, published_at,
                                  raw_text, text_truncated, language, content_hash)
            VALUES (%(source_id)s, %(title)s, %(url)s, %(published_at)s,
                    %(raw_text)s, COALESCE(%(text_truncated)s, FALSE), %(language)s, %(content_hash)s)
            ON CONFLICT (url) DO NOTHING
            RETURNING id
            """,
            rec,
        )
        row = cur.fetchone()
        conn.commit()
    return row is not None


def get_articles_needing_full_text(limit: int = 50) -> list[dict]:
    """Articles whose RSS body is likely only a teaser and needs URL extraction."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.url IS NOT NULL
              AND a.full_text_status IS NULL
              AND (
                COALESCE(a.text_truncated, FALSE) = TRUE
                OR length(COALESCE(a.raw_text, '')) < 800
              )
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def update_article_full_text(article_id: int, raw_text: str | None, text_truncated: bool,
                             status: str, method: str, error: str | None = None) -> None:
    """Store full-text extraction result without losing the RSS teaser on failure."""
    with get_connection() as conn:
        if raw_text is not None:
            conn.execute(
                """
                UPDATE articles
                SET raw_text = %s,
                    text_truncated = %s,
                    full_text_fetched_at = now(),
                    full_text_status = %s,
                    full_text_error = %s,
                    extraction_method = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (raw_text, text_truncated, status, error, method, article_id),
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


# ---------------------------------------------------------------------------
#  Диагностика (для stats)
# ---------------------------------------------------------------------------

def count_sources() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]


def count_articles() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]


def sources_by_strategy() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT parse_strategy, COUNT(*) AS n FROM sources "
            "GROUP BY parse_strategy ORDER BY n DESC"
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


def get_articles_by_ids(article_ids: list[int], include_summary: bool = False) -> list[dict]:
    if not article_ids:
        return []
    summary_select = ", c.summary" if include_summary else ""
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


def get_articles_needing_summary(limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN article_cards c ON c.article_id = a.id
            WHERE c.summary IS NULL
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def get_articles_needing_relevance(limit: int = 20) -> list[dict]:
    """Статьи с готовой сутью, но без проверки релевантности (card.relevant IS NULL)."""
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, c.summary, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            WHERE c.summary IS NOT NULL AND c.relevant IS NULL
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def set_article_relevance(article_id: int, relevant: bool, reason: str | None,
                          model: str | None = None) -> None:
    """Записать вердикт релевантности. Нерелевантные → status='rejected'."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE article_cards
            SET relevant = %s,
                relevance_reason = %s,
                relevance_model = %s,
                status = CASE WHEN %s THEN status ELSE 'rejected' END,
                updated_at = now()
            WHERE article_id = %s
            """,
            (relevant, reason, model, relevant, article_id),
        )
        conn.commit()


def get_articles_needing_tags(limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, c.summary, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_tags at ON at.article_id = a.id
            WHERE c.summary IS NOT NULL AND c.relevant IS TRUE AND at.id IS NULL
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def get_articles_needing_scores(limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.*, c.summary, s.name AS source_name, s.priority AS source_priority,
                   s.category AS source_category
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_scores sc ON sc.article_id = a.id
            WHERE c.summary IS NOT NULL AND c.relevant IS TRUE AND sc.id IS NULL
            ORDER BY a.published_at DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


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


def upsert_article_card(article_id: int, summary: str, model: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO article_cards (article_id, summary, summary_model, summary_generated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (article_id) DO UPDATE SET
                summary = EXCLUDED.summary,
                summary_model = EXCLUDED.summary_model,
                summary_generated_at = now(),
                updated_at = now()
            """,
            (article_id, summary, model),
        )
        conn.commit()


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
            conn.execute(
                """
                UPDATE tags
                SET name_en = %(name_en)s,
                    description = %(description)s,
                    keywords_json = %(keywords_json)s,
                    keywords_en_json = %(keywords_en_json)s,
                    sort_order = %(sort_order)s,
                    updated_at = now()
                WHERE id = %(id)s
                """,
                {
                    **rec,
                    "id": tag_id,
                    "keywords_json": Json(rec.get("keywords_json") or []),
                    "keywords_en_json": Json(rec.get("keywords_en_json") or []),
                },
            )
            conn.commit()
            return tag_id

        cur = conn.execute(
            """
            INSERT INTO tags (parent_id, name, name_en, description, keywords_json,
                              keywords_en_json, enabled, sort_order)
            VALUES (%(parent_id)s, %(name)s, %(name_en)s, %(description)s,
                    %(keywords_json)s, %(keywords_en_json)s, TRUE, %(sort_order)s)
            RETURNING id
            """,
            {
                **rec,
                "keywords_json": Json(rec.get("keywords_json") or []),
                "keywords_en_json": Json(rec.get("keywords_en_json") or []),
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
    (parent резолвится по имени). Отсутствующие в списке — отключаются (soft delete,
    чтобы не рвать FK на article_tags)."""
    name_to_id: dict[str, int] = {}
    keep: list[int] = []
    # сначала корневые, затем дочерние — чтобы parent_id уже был известен
    ordered = [i for i in items if not i.get("parent_name")] + [i for i in items if i.get("parent_name")]
    with get_connection() as conn:
        for it in ordered:
            parent_id = name_to_id.get(it.get("parent_name")) if it.get("parent_name") else None
            payload = {
                "parent_id": parent_id,
                "name": it["name"],
                "name_en": it.get("name_en"),
                "description": it.get("description"),
                "keywords_json": Json(it.get("keywords_json") or []),
                "keywords_en_json": Json(it.get("keywords_en_json") or []),
                "enabled": bool(it.get("enabled", True)),
                "sort_order": it.get("sort_order") or 0,
            }
            if it.get("id"):
                conn.execute(
                    """
                    UPDATE tags SET parent_id=%(parent_id)s, name=%(name)s, name_en=%(name_en)s,
                        description=%(description)s, keywords_json=%(keywords_json)s,
                        keywords_en_json=%(keywords_en_json)s, enabled=%(enabled)s,
                        sort_order=%(sort_order)s, updated_at=now()
                    WHERE id=%(id)s
                    """,
                    {**payload, "id": int(it["id"])},
                )
                tag_id = int(it["id"])
            else:
                cur = conn.execute(
                    """
                    INSERT INTO tags (parent_id, name, name_en, description, keywords_json,
                                      keywords_en_json, enabled, sort_order)
                    VALUES (%(parent_id)s, %(name)s, %(name_en)s, %(description)s,
                            %(keywords_json)s, %(keywords_en_json)s, %(enabled)s, %(sort_order)s)
                    RETURNING id
                    """,
                    payload,
                )
                tag_id = int(cur.fetchone()[0])
            name_to_id[it["name"]] = tag_id
            keep.append(tag_id)
        if keep:
            conn.execute("UPDATE tags SET enabled=FALSE WHERE id <> ALL(%s)", (keep,))
        conn.commit()
    return {"saved": len(items)}


def delete_tag(tag_id: int) -> None:
    """Мягкое удаление тега и его подтегов (enabled=FALSE) — FK на article_tags не рвём."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE tags SET enabled=FALSE, updated_at=now() WHERE id=%s OR parent_id=%s",
            (tag_id, tag_id),
        )
        conn.commit()


def upsert_article_tag(article_id: int, tag_id: int, confidence: float,
                       rationale: str | None, model: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO article_tags (article_id, tag_id, confidence, rationale, model)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (article_id) DO UPDATE SET
                tag_id = EXCLUDED.tag_id,
                confidence = EXCLUDED.confidence,
                rationale = EXCLUDED.rationale,
                model = EXCLUDED.model,
                created_at = now()
            """,
            (article_id, tag_id, confidence, rationale, model),
        )
        conn.commit()


def upsert_scoring_criterion(rec: dict) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO scoring_criteria (name, description, weight, keywords_json,
                                          keywords_en_json, enabled, sort_order)
            VALUES (%(name)s, %(description)s, %(weight)s, %(keywords_json)s,
                    %(keywords_en_json)s, TRUE, %(sort_order)s)
            ON CONFLICT (name) DO UPDATE SET
                description = EXCLUDED.description,
                weight = EXCLUDED.weight,
                keywords_json = EXCLUDED.keywords_json,
                keywords_en_json = EXCLUDED.keywords_en_json,
                enabled = TRUE,
                sort_order = EXCLUDED.sort_order,
                updated_at = now()
            RETURNING id
            """,
            {
                **rec,
                "keywords_json": Json(rec.get("keywords_json") or []),
                "keywords_en_json": Json(rec.get("keywords_en_json") or []),
            },
        )
        criterion_id = cur.fetchone()[0]
        conn.commit()
        return criterion_id


def list_enabled_scoring_criteria() -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT *
            FROM scoring_criteria
            WHERE enabled = TRUE
            ORDER BY sort_order, id
            """
        )
        return cur.fetchall()


def delete_scoring_criterion(criterion_id: int) -> None:
    """Мягкое удаление критерия (enabled=FALSE) — не рвём FK на article_score_items."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE scoring_criteria SET enabled = FALSE, updated_at = now() WHERE id = %s",
            (criterion_id,),
        )
        conn.commit()


def save_scoring_criteria(items: list[dict]) -> dict:
    """Bulk-сохранение профиля критериев (как кнопка «Сохранить» в мокапе).

    Валидирует сумму весов = 100. Существующие обновляются по id, новые вставляются,
    отсутствующие в списке — отключаются (soft delete, чтобы не рвать FK).
    """
    total = round(sum(float(i.get("weight") or 0) for i in items), 2)
    if total != 100:
        raise ValueError(f"Сумма весов критериев должна быть 100, сейчас {total}")

    keep_ids: list[int] = []
    with get_connection() as conn:
        for it in items:
            payload = {
                "name": it["name"],
                "description": it.get("description"),
                "weight": it["weight"],
                "keywords_json": Json(it.get("keywords_json") or []),
                "keywords_en_json": Json(it.get("keywords_en_json") or []),
                "sort_order": it.get("sort_order") or 0,
            }
            if it.get("id"):
                conn.execute(
                    """
                    UPDATE scoring_criteria SET name=%(name)s, description=%(description)s,
                        weight=%(weight)s, keywords_json=%(keywords_json)s,
                        keywords_en_json=%(keywords_en_json)s, sort_order=%(sort_order)s,
                        enabled=TRUE, updated_at=now()
                    WHERE id=%(id)s
                    """,
                    {**payload, "id": int(it["id"])},
                )
                keep_ids.append(int(it["id"]))
            else:
                cur = conn.execute(
                    """
                    INSERT INTO scoring_criteria (name, description, weight, keywords_json,
                                                  keywords_en_json, enabled, sort_order)
                    VALUES (%(name)s, %(description)s, %(weight)s, %(keywords_json)s,
                            %(keywords_en_json)s, TRUE, %(sort_order)s)
                    ON CONFLICT (name) DO UPDATE SET description=EXCLUDED.description,
                        weight=EXCLUDED.weight, keywords_json=EXCLUDED.keywords_json,
                        keywords_en_json=EXCLUDED.keywords_en_json, enabled=TRUE, updated_at=now()
                    RETURNING id
                    """,
                    payload,
                )
                keep_ids.append(int(cur.fetchone()[0]))
        if keep_ids:
            conn.execute(
                "UPDATE scoring_criteria SET enabled=FALSE, updated_at=now() WHERE id <> ALL(%s)",
                (keep_ids,),
            )
        conn.commit()
    return {"saved": len(items), "weight_sum": total}


def replace_article_score(article_id: int, total_score: float, score_label: str,
                          explanation: str, items: list[dict],
                          model: str | None = None) -> None:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO article_scores (article_id, model, total_score, score_label, explanation)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (article_id) DO UPDATE SET
                model = EXCLUDED.model,
                total_score = EXCLUDED.total_score,
                score_label = EXCLUDED.score_label,
                explanation = EXCLUDED.explanation,
                updated_at = now()
            RETURNING id
            """,
            (article_id, model, total_score, score_label, explanation),
        )
        score_id = cur.fetchone()[0]
        conn.execute("DELETE FROM article_score_items WHERE article_score_id = %s", (score_id,))
        for item in items:
            conn.execute(
                """
                INSERT INTO article_score_items
                  (article_score_id, criterion_id, keyword_score, ai_score, final_score, rationale)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    score_id,
                    item["criterion_id"],
                    item.get("keyword_score"),
                    item.get("ai_score"),
                    item.get("final_score"),
                    item.get("rationale"),
                ),
            )
        conn.commit()


def insert_ai_run(rec: dict) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO ai_processing_runs
              (article_id, stage, provider, model, language, input_tokens, output_tokens,
               total_tokens, cost_usd, status, error_message)
            VALUES (%(article_id)s, %(stage)s, %(provider)s, %(model)s, %(language)s,
                    %(input_tokens)s, %(output_tokens)s, %(total_tokens)s, %(cost_usd)s,
                    %(status)s, %(error_message)s)
            """,
            rec,
        )
        conn.commit()


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
    """Cost of processing one article through AI stages.

    complete_only=True returns articles that have successful summary, tagging and
    scoring runs, which is the cleanest estimate for one full processing cycle.
    """
    stages_filter = "HAVING COUNT(DISTINCT r.stage) = 3" if complete_only else ""
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
            WHERE r.stage IN ('summary', 'tagging', 'scoring')
            GROUP BY a.id, a.title, a.language
            {stages_filter}
            ORDER BY cost_usd DESC NULLS LAST, a.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def digest_candidates(month: str, limit: int = 20, min_score: float = 60) -> list[dict]:
    with get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT a.id, a.title, a.url, a.published_at, a.language,
                   s.name AS source_name,
                   c.summary, c.selected_for_digest,
                   sc.total_score, sc.score_label,
                   t.name AS tag_name, parent.name AS parent_tag_name
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN article_cards c ON c.article_id = a.id
            LEFT JOIN article_scores sc ON sc.article_id = a.id
            LEFT JOIN article_tags at ON at.article_id = a.id
            LEFT JOIN tags t ON t.id = at.tag_id
            LEFT JOIN tags parent ON parent.id = t.parent_id
            WHERE to_char(COALESCE(a.published_at, a.collected_at), 'YYYY-MM') = %s
              AND COALESCE(c.status, 'new') = 'digest'
              AND c.relevant IS NOT FALSE
            ORDER BY sc.total_score DESC NULLS LAST,
                     a.published_at DESC NULLS LAST
            LIMIT %s
            """,
            (month, limit),
        )
        return cur.fetchall()
