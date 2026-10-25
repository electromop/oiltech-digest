"""Материалы коллег: что сотрудник прислал боту в рабочую группу Telegram, и его оценка.

Бот живёт на NL (`tg_materials_bot.py`: с РФ-ядра Telegram недоступен), а запись — здесь, на
ядре, через `/api/external-worker/materials` с токеном воркера. Модуль общий для обеих сторон:
шкала оценки и правила полей одни, и бот не примет того, от чего потом откажется ядро.

Шкала — та же, что у карточки радара (список Виктора от 13.09, `SignalRadarPage.tsx`,
`signal_feedback._normalize_verdict`), все девять пунктов, одни и те же значения в базе.

ВНИМАНИЕ про тесты: соединение — `connection.get_connection()` атрибутом модуля (как в
documents_repo): изоляция тестов держится на подмене этого атрибута.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from oiltech_digest import config
from oiltech_digest.db import connection

KINDS = {
    "link": "Ссылка + описание",
    "file": "Файл + описание",
    "event": "Событие — описание своими словами",
}

# (значение, коротко — для кнопки, полностью — как на экране радара). Порядок — как на экране.
VERDICTS: list[tuple[str, str, str]] = [
    ("strong_signal", "Сильный сигнал", "Сильный сигнал — вынести в дайджест / обсуждать"),
    ("approved", "Полезный сигнал", "Полезный сигнал — релевантно, сохранить в базе"),
    ("watch_later", "Наблюдать", "Наблюдать — рано, нужен следующий этап"),
    ("background_material", "Фоновый материал", "Фоновый материал — ориентир или контекст"),
    ("reject", "Низкая ценность / шум", "Низкая ценность / шум — по теме, но без новой ценности"),
    ("wrong_domain", "Не релевантно", "Не релевантно — вне интересов Компании"),
    ("wrong_block", "Не тот блок", "Не тот блок — бизнес-сигнал, а не технология"),
    ("merge_duplicate", "Дубль", "Дубль — тот же сигнал или технологический кластер"),
    ("overrated", "Оценка завышена", "Оценка завышена — сигнал годный, балл слишком высокий"),
]
VERDICT_LABELS = {value: full for value, _short, full in VERDICTS}

TITLE_MAX = 200
DESCRIPTION_MIN = 20
DESCRIPTION_MAX = 4000
COMMENT_MAX = 1000
COMPANIES_MAX = 10
COMPANY_MAX = 100
URL_MAX = 2000
FILES_SUBDIR = "colleague_materials"


class InvalidMaterial(ValueError):
    """Материал не принят; текст — для человека."""


def check_url(value: str) -> str:
    url = (value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or " " in url:
        raise InvalidMaterial("Нужна ссылка вида https://…")
    if len(url) > URL_MAX:
        raise InvalidMaterial("Ссылка слишком длинная")
    return url


def check_title(value: str) -> str:
    title = " ".join((value or "").split())
    if len(title) < 5:
        raise InvalidMaterial("Заголовок короче 5 символов — напишите, о чём материал")
    if len(title) > TITLE_MAX:
        raise InvalidMaterial(f"Заголовок длиннее {TITLE_MAX} символов — сократите до одной строки")
    return title


def check_description(value: str) -> str:
    description = (value or "").strip()
    if len(description) < DESCRIPTION_MIN:
        raise InvalidMaterial(f"Описание короче {DESCRIPTION_MIN} символов — добавьте, что произошло и чем это важно")
    if len(description) > DESCRIPTION_MAX:
        raise InvalidMaterial(f"Описание длиннее {DESCRIPTION_MAX} символов — сократите")
    return description


def parse_companies(value: str) -> list[str]:
    """«Газпром нефть, Halliburton; СИБУР» → список; «-» и пусто — нет компаний."""
    text = (value or "").strip()
    if text in {"", "-", "—", "нет"}:
        return []
    items: list[str] = []
    for part in re.split(r"[,;\n]", text):
        name = " ".join(part.split())
        if name and name.lower() not in {item.lower() for item in items}:
            items.append(name[:COMPANY_MAX])
    if len(items) > COMPANIES_MAX:
        raise InvalidMaterial(f"Не больше {COMPANIES_MAX} компаний")
    return items


def check_verdict(value: str) -> str:
    if value not in VERDICT_LABELS:
        raise InvalidMaterial("Оценка — один из пунктов шкалы")
    return value


def check_comment(value: str | None) -> str | None:
    comment = (value or "").strip()
    if len(comment) > COMMENT_MAX:
        raise InvalidMaterial(f"Комментарий длиннее {COMMENT_MAX} символов")
    return comment or None


def validate(material: dict[str, Any], *, has_file: bool) -> dict[str, Any]:
    """Полный материал от бота → поля строки. Ядро проверяет заново: бот может быть старой сборки."""
    kind = str(material.get("kind") or "")
    if kind not in KINDS:
        raise InvalidMaterial("Неизвестный тип материала")
    url = check_url(str(material.get("url") or "")) if kind == "link" else None
    if kind == "file" and not has_file:
        raise InvalidMaterial("Материал «файл» пришёл без файла")
    if kind != "file" and has_file:
        raise InvalidMaterial("Файл прикладывается только к материалу «файл»")
    companies = material.get("companies") or []
    if not isinstance(companies, list):
        raise InvalidMaterial("Компании — списком")
    companies = parse_companies(", ".join(str(item) for item in companies))
    try:
        tg = {name: int(material[name]) for name in ("tg_chat_id", "tg_message_id", "tg_user_id")}
    except (KeyError, TypeError, ValueError):
        raise InvalidMaterial("Нет отметки сообщения Telegram") from None
    author = " ".join(str(material.get("tg_author") or "").split())[:200]
    if not author:
        raise InvalidMaterial("Нет автора")
    return {
        "kind": kind,
        "title": check_title(str(material.get("title") or "")),
        "description": check_description(str(material.get("description") or "")),
        "url": url,
        "companies": companies,
        "verdict": check_verdict(str(material.get("verdict") or "")),
        "verdict_comment": check_comment(material.get("verdict_comment")),
        **tg,
        "tg_username": (str(material.get("tg_username") or "").lstrip("@")[:64] or None),
        "tg_author": author,
    }


# --- ядро: файлы и база ----------------------------------------------------------------------


def files_dir() -> Path:
    directory = Path(config.UPLOAD_DIR) / FILES_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def store_file(data: bytes, filename: str) -> Path:
    """Имя на диске — отпечаток и расширение (только [a-z0-9]): имя от пользователя в путь не идёт."""
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    suffix = suffix if re.fullmatch(r"[a-z0-9]{1,8}", suffix) else "bin"
    path = files_dir() / f"{hashlib.sha256(data).hexdigest()}.{suffix}"
    if not path.exists():
        path.write_bytes(data)
    return path


def save(fields: dict[str, Any], *, file_name: str | None = None, file_path: str | None = None,
         file_size: int | None = None) -> tuple[dict[str, Any], bool]:
    """Записать материал. Повтор той же отправки (то же сообщение бота) — прежняя строка и False."""
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            INSERT INTO colleague_materials (
              kind, title, description, url, companies_json, verdict, verdict_comment,
              file_name, file_path, file_size,
              tg_chat_id, tg_message_id, tg_user_id, tg_username, tg_author)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tg_chat_id, tg_message_id) DO NOTHING
            RETURNING *
            """,
            (fields["kind"], fields["title"], fields["description"], fields["url"],
             Jsonb(fields["companies"]), fields["verdict"], fields["verdict_comment"],
             file_name, file_path, file_size,
             fields["tg_chat_id"], fields["tg_message_id"], fields["tg_user_id"],
             fields["tg_username"], fields["tg_author"]),
        )
        row = cur.fetchone()
        created = row is not None
        if row is None:
            cur.execute(
                "SELECT * FROM colleague_materials WHERE tg_chat_id = %s AND tg_message_id = %s",
                (fields["tg_chat_id"], fields["tg_message_id"]),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row), created


def list_materials(*, limit: int = 200, tg_user_id: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    where, params = ("WHERE tg_user_id = %s", [tg_user_id]) if tg_user_id is not None else ("", [])
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            f"SELECT * FROM colleague_materials {where} ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            (*params, max(1, min(int(limit), 500)), max(0, int(offset))),
        )
        return [dict(row) for row in cur.fetchall()]


def count_materials(*, tg_user_id: int | None = None) -> int:
    where, params = ("WHERE tg_user_id = %s", [tg_user_id]) if tg_user_id is not None else ("", [])
    with connection.get_connection() as conn:
        return int(conn.execute(f"SELECT count(*) FROM colleague_materials {where}", params).fetchone()[0])


def status(row: dict[str, Any]) -> tuple[str, str]:
    """Где материал в воронке. До оценки материалов (следующий шаг) — «принят»; с сигналом — его номер."""
    if row.get("signal_id"):
        return "signal", f"Стал сигналом №{row['signal_id']}"
    return "accepted", "Принят на платформу"


def publication_text(row: dict[str, Any]) -> tuple[str, str | None]:
    """Текст материала как публикации — вход агентов (#131, спека #128) и колонка пакета эксперту (#129): описание
    автора; у файла и текст файла, в тех же пределах, что статья (обрезает publication_text). Файл не прочитался —
    только описание и почему (вторым значением)."""
    if not row.get("file_path"):
        return row["description"], None
    from oiltech_digest.documents.model import DocumentError
    from oiltech_digest.documents.parsing import parse_document

    try:
        document = parse_document(Path(row["file_path"]).read_bytes())
    except (OSError, DocumentError) as exc:
        return row["description"], f"файл не прочитан: {exc}"
    return row["description"] + "\n\n" + "\n".join(anchor.text for anchor in document.anchors), None


def as_publication(row: dict[str, Any]) -> dict[str, Any]:
    """Строка материала как публикация агентов (#131) и пакета эксперту (#129): text — publication_text, file_note —
    почему файл не прочитан. Издатель — сайт ссылки (signal_card_pack._publisher_label): источника у материала нет."""
    text, note = publication_text(row)
    return {**row, "text": text, "file_note": note}


def get_material(material_id: int) -> dict[str, Any] | None:
    with connection.get_connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM colleague_materials WHERE id = %s", (material_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def brief(row: dict[str, Any]) -> dict[str, Any]:
    """Строка для экрана и бота: без пути на диске."""
    state, state_label = status(row)
    return {
        "id": row["id"],
        "kind": row["kind"],
        "kind_label": KINDS.get(row["kind"], row["kind"]),
        "title": row["title"],
        "description": row["description"],
        "url": row.get("url"),
        "companies": list(row.get("companies_json") or []),
        "verdict": row["verdict"],
        "verdict_label": VERDICT_LABELS.get(row["verdict"], row["verdict"]),
        "verdict_comment": row.get("verdict_comment"),
        "file_name": row.get("file_name"),
        "file_size": row.get("file_size"),
        "has_file": bool(row.get("file_path")),
        "author": row["tg_author"],
        "author_username": row.get("tg_username"),
        "signal_id": row.get("signal_id"),
        "status": state,
        "status_label": state_label,
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }
