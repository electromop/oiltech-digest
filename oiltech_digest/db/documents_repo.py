"""Доступ к данным загруженных документов.

Отдельный модуль, а не часть repository.py, по двум причинам: repository.py уже за три
тысячи строк, и — важнее — документ не должен случайно попасть ни в один запрос по articles.

ВНИМАНИЕ про тесты: соединение берётся как `connection.get_connection()`, атрибутом модуля,
а НЕ через `from ... import get_connection`. Изоляция тестов держится на подмене этого
атрибута; модуль, импортировавший имя напрямую, уходит в боевую схему мимо подмены —
на этом уже обжигались (source_overrides).
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from oiltech_digest.db import connection


def _conn():
    return connection.get_connection()


def create_document(
    *,
    owner_user_id: int,
    filename: str,
    storage_path: str,
    kind: str,
    size_bytes: int,
    content_sha256: str,
    attestation_text: str,
) -> int:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO documents (owner_user_id, filename, storage_path, kind, size_bytes,
                                   content_sha256, attested_at, attestation_text)
            VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
            RETURNING id
            """,
            (owner_user_id, filename, storage_path, kind, size_bytes, content_sha256, attestation_text),
        )
        document_id = int(cur.fetchone()[0])
        conn.commit()
        return document_id


def document_by_hash(owner_user_id: int, content_sha256: str) -> dict | None:
    """Повторная загрузка того же файла тем же владельцем возвращает существующий документ."""
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT * FROM documents WHERE owner_user_id = %s AND content_sha256 = %s",
            (owner_user_id, content_sha256),
        )
        return cur.fetchone()


def get_document(document_id: int, owner_user_id: int | None = None) -> dict | None:
    """owner_user_id=None — только для внутренних путей (воркер). Из HTTP всегда передавать
    владельца: проверка существования без проверки владельца отдаёт чужой документ."""
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        if owner_user_id is None:
            cur.execute("SELECT * FROM documents WHERE id = %s", (document_id,))
        else:
            cur.execute(
                "SELECT * FROM documents WHERE id = %s AND owner_user_id = %s",
                (document_id, owner_user_id),
            )
        return cur.fetchone()


def list_documents(owner_user_id: int, limit: int = 100) -> list[dict]:
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT d.*, c.essence, c.doc_type, c.publisher,
                   (SELECT count(*) FROM document_facts f WHERE f.document_id = d.id) AS fact_count
            FROM documents d
            LEFT JOIN document_cards c ON c.document_id = d.id
            WHERE d.owner_user_id = %s
            ORDER BY d.created_at DESC
            LIMIT %s
            """,
            (owner_user_id, limit),
        )
        return cur.fetchall()


def set_status(document_id: int, status: str, error_message: str | None = None) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE documents SET status = %s, error_message = %s, updated_at = now() WHERE id = %s",
            (status, error_message, document_id),
        )
        conn.commit()


def save_anchors(document_id: int, anchors: list[tuple[int, str]], anchor_unit: str) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM document_anchors WHERE document_id = %s", (document_id,))
        cur.executemany(
            "INSERT INTO document_anchors (document_id, number, text) VALUES (%s, %s, %s)",
            [(document_id, n, t) for n, t in anchors],
        )
        cur.execute(
            """UPDATE documents SET anchor_unit = %s, anchor_count = %s, text_chars = %s,
                                    status = 'parsed', updated_at = now()
               WHERE id = %s""",
            (anchor_unit, len(anchors), sum(len(t) for _, t in anchors), document_id),
        )
        conn.commit()


def get_anchors(document_id: int) -> list[dict]:
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT number, text FROM document_anchors WHERE document_id = %s ORDER BY number",
            (document_id,),
        )
        return cur.fetchall()


def save_card(document_id: int, card: dict[str, Any], model: str | None) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO document_cards (document_id, doc_type, publisher, doc_date, language,
                                        essence, summary_json, claims_json, model, generated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (document_id) DO UPDATE SET
                doc_type = EXCLUDED.doc_type, publisher = EXCLUDED.publisher,
                doc_date = EXCLUDED.doc_date, language = EXCLUDED.language,
                essence = EXCLUDED.essence, summary_json = EXCLUDED.summary_json,
                claims_json = EXCLUDED.claims_json, model = EXCLUDED.model,
                generated_at = now()
            """,
            (
                document_id,
                card.get("doc_type"),
                card.get("publisher"),
                card.get("doc_date"),
                card.get("language"),
                card.get("essence"),
                __import__("json").dumps(card.get("summary") or [], ensure_ascii=False),
                __import__("json").dumps(card.get("claims") or [], ensure_ascii=False),
                model,
            ),
        )
        conn.commit()


def replace_facts(document_id: int, facts: list[dict]) -> None:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM document_facts WHERE document_id = %s", (document_id,))
        cur.executemany(
            """INSERT INTO document_facts (document_id, value, unit, context, anchor, verified)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            [
                (document_id, f.get("value"), f.get("unit"), f.get("context"),
                 f.get("anchor"), bool(f.get("verified")))
                for f in facts
            ],
        )
        conn.commit()


def get_card(document_id: int) -> dict | None:
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM document_cards WHERE document_id = %s", (document_id,))
        return cur.fetchone()


def get_facts(document_id: int) -> list[dict]:
    with _conn() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT * FROM document_facts WHERE document_id = %s ORDER BY anchor NULLS LAST, id",
            (document_id,),
        )
        return cur.fetchall()


def delete_document(document_id: int, owner_user_id: int) -> str | None:
    """Возвращает storage_path удалённого документа — файл сносит вызывающий:
    файловая система транзакцией не покрывается."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM documents WHERE id = %s AND owner_user_id = %s RETURNING storage_path",
            (document_id, owner_user_id),
        )
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else None


def record_ai_run(
    *,
    job_id: int | None,
    document_id: int,
    stage: str,
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    status: str = "ok",
    error_message: str | None = None,
) -> None:
    """Дедуп по частичному уникальному индексу (job_id, document_id, stage) WHERE document_id
    IS NOT NULL: повторное применение результата задачи не двоит счёт за OpenAI."""
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO ai_processing_runs (job_id, document_id, stage, model, input_tokens,
                                            output_tokens, total_tokens, cost_usd, status, error_message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (job_id, document_id, stage, model, input_tokens, output_tokens,
             input_tokens + output_tokens, cost_usd, status, error_message),
        )
        conn.commit()
