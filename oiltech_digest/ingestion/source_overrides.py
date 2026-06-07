"""Версионируемые оверрайды для источников с JS-рендером / WAF-защитой.

Зачем: для части иностранных сайтов нужен парсинг через headless Chromium
(`parse_strategy='playwright'`) и/или конкретный URL раздела новостей
(`listing_url`), которого нет в Excel-сидере. Чтобы эта настройка была
воспроизводимой (переживала пересоздание БД, а не жила только в проде через
ручной SQL), держим её здесь и применяем идемпотентно после seed.

Применение: `python -m oiltech_digest.cli apply-source-overrides`
(в Docker вызывается в bootstrap после seed-sources).

При смене стратегии/листинга сбрасываем request-состояние
(`last_listing_hash`, `last_seen_*`), иначе первый playwright-парс коротит на
старом хэше от прежней (request) попытки и добавляет 0.
"""

from __future__ import annotations

import logging

from oiltech_digest.db.connection import get_connection

logger = logging.getLogger(__name__)

# Ключ — точное имя источника (sources.name). Значения:
#   parse_strategy — обязательно ('playwright' для JS/WAF-сайтов);
#   listing_url    — опционально; None = не трогать (берётся из url/сидера).
SOURCE_OVERRIDES: dict[str, dict] = {
    # Проверено на проде:
    "Shell": {"parse_strategy": "playwright"},  # главная отдаёт пресс-релизы (+6 статей)
    "Baker Hughes": {"parse_strategy": "playwright",
                     "listing_url": "https://www.bakerhughes.com/company/news"},
    # Группа 🟡 (Playwright рендерит, нужен правильный news-URL) — добавляем после проверки:
    # "Weatherford": {"parse_strategy": "playwright", "listing_url": "..."},
    # "OPEC": {"parse_strategy": "playwright", "listing_url": "..."},
    # "Kuwait Oil Company": {"parse_strategy": "playwright", "listing_url": "..."},
    # "BCG Energy": {"parse_strategy": "playwright", "listing_url": "..."},
}


def apply_overrides() -> dict:
    """Идемпотентно применить оверрайды. Меняет строку только если что-то реально
    изменилось, и тогда же сбрасывает request-состояние. Возвращает статистику."""
    changed = 0
    unchanged = 0
    not_found = 0
    with get_connection() as conn:
        for name, fields in SOURCE_OVERRIDES.items():
            new_strategy = fields["parse_strategy"]
            new_listing = fields.get("listing_url")
            row = conn.execute(
                "SELECT id, parse_strategy, listing_url FROM sources WHERE name = %s",
                (name,),
            ).fetchone()
            if row is None:
                not_found += 1
                logger.warning("source override: источник %r не найден в БД", name)
                continue
            source_id, cur_strategy, cur_listing = row
            listing_changed = new_listing is not None and (cur_listing or "") != new_listing
            if cur_strategy == new_strategy and not listing_changed:
                unchanged += 1
                continue

            sets = [
                "parse_strategy = %(strategy)s",
                "last_listing_hash = NULL",
                "last_seen_article_url = NULL",
                "last_seen_published_at = NULL",
                "updated_at = now()",
            ]
            params = {"id": source_id, "strategy": new_strategy}
            if new_listing is not None:
                sets.append("listing_url = %(listing_url)s")
                params["listing_url"] = new_listing
            conn.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id = %(id)s", params)
            changed += 1
            logger.info("source override: %s → %s%s", name, new_strategy,
                        f" listing={new_listing}" if new_listing else "")
        conn.commit()
    return {"changed": changed, "unchanged": unchanged, "not_found": not_found}
