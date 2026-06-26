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
#   parse_strategy — обязательно ('playwright' для JS/WAF-сайтов, 'rss' для лент);
#   listing_url    — опционально; None = не трогать (берётся из url/сидера).
#   rss_url        — опционально; для RSS-лент с нестандартным/сменившимся URL фида.
#   url            — опционально; для telegram/прочих с исправленным каналом/адресом.
#   network_region — опционально ('external' = фетчить через зарубежный воркер).
#     Нужен для западных сайтов, к которым с РФ-сервера нет доступа (WAF/таймаут), но
#     которые открываются из NL. Действует ТОЛЬКО при FETCH_EXTERNAL_ENABLED=1 —
#     иначе источник парсится локально как обычно (флаг проверяется в parse_all и
#     enqueue-external-scrape). Каптча/hard-WAF (Cloudflare/Akamai) сюда НЕ помечаем —
#     они 403 и из NL (нужен challenge-solver, отдельный проект).
SOURCE_OVERRIDES: dict[str, dict] = {
    # Проверено на проде:
    "Shell": {"parse_strategy": "playwright"},  # главная отдаёт пресс-релизы (+6 статей)
    "Baker Hughes": {"parse_strategy": "playwright",
                     "listing_url": "https://www.bakerhughes.com/company/news",
                     "network_region": "external"},  # РФ→таймаут playwright, рендерится из NL
    # Endeavor Business Media сменил схему RSS: старый путь /__rss/website-scheduled-content/
    # отдаёт 404, новый формат — query-параметр ?input={"sectionAlias":"home"}. Храним
    # percent-encoded (без сырых {}"" ), чтобы пережить и SQL, и requote_uri в requests.
    # Проверено: ленты живые, со свежими статьями (июнь 2026).
    # Endeavor RSS отдаёт 403 с РФ-сервера (гео-WAF), но работает из NL → external.
    "Oil & Gas Journal": {"parse_strategy": "rss",
                          "rss_url": "https://www.ogj.com/__rss/website-scheduled-content.xml?input=%7B%22sectionAlias%22%3A%22home%22%7D",
                          "network_region": "external"},
    "Offshore Magazine": {"parse_strategy": "rss",
                          "rss_url": "https://www.offshore-mag.com/__rss/website-scheduled-content.xml?input=%7B%22sectionAlias%22%3A%22home%22%7D",
                          "network_region": "external"},
    "Automation World": {"parse_strategy": "rss",
                         "rss_url": "https://www.automationworld.com/__rss/website-scheduled-content.xml?input=%7B%22sectionAlias%22%3A%22home%22%7D",
                         "network_region": "external"},
    # Отраслевые издания с рабочим RSS (подтверждено живым parse на проде). RSS гео-
    # независим — в отличие от request-newsroom, работает с любого IP одинаково.
    "World Oil": {"parse_strategy": "rss", "rss_url": "https://www.worldoil.com/rss?feed=news"},  # +10
    "Hydrocarbon Processing": {"parse_strategy": "rss", "rss_url": "https://www.hydrocarbonprocessing.com/rss?feed=news"},  # +10 (неполная TLS → verify=False fallback)
    "EIA": {"parse_strategy": "rss", "rss_url": "https://www.eia.gov/rss/todayinenergy.xml"},  # +15
    # JS-корпораты, ПОДТВЕРЖДЁННЫЕ на проде (request + listing_url на newsroom отдаёт
    # статьи; анти-заморозка в request_parser держит свежие релизы от застревания):
    "Eni": {"parse_strategy": "request", "listing_url": "https://www.eni.com/en-IT/media.html"},  # +3 на проде
    "Petrobras": {"parse_strategy": "request", "listing_url": "https://agencia.petrobras.com.br/en/mais-recentes"},  # +6 на проде
    "IEA": {"parse_strategy": "request", "listing_url": "https://www.iea.org/news",
            "network_region": "external"},  # +4; с РФ-сервера 403 → external
    "CNOOC": {"parse_strategy": "request", "listing_url": "https://www.cnoocltd.com/english/presscenter/pressreleases/2026/"},  # +6 (годовой путь — обновить в 2027)
    # ВАЖНЫЙ УРОК: WebFetch из US-облака видит SSR-версию newsroom, но на NL-проде те же
    # /news часто отдают JS-навигацию (0 статей у request). Поэтому request оставляем
    # ТОЛЬКО для проверенных живым parse на проде. Ниже — playwright-кандидаты: news-URL
    # найден, но listing рендерится через JS → нужен прод-тест с parse_strategy='playwright'
    # (Chromium в образе), как делали с Shell/Baker Hughes. ПОДТВЕРЖДЕНО на проде
    # (playwright рендерит JS и извлекает валидные статьи; пробивает даже 403, на
    # которых падали request/WebFetch — BP/TechnipFMC):
    "SLB (Schlumberger)": {"parse_strategy": "playwright", "listing_url": "https://www.slb.com/news-and-insights", "network_region": "external"},  # рендер ✓; с РФ таймаут → external
    "ADNOC": {"parse_strategy": "playwright", "listing_url": "https://www.adnoc.ae/en/news-and-media"},  # рендер ✓
    "SOCAR": {"parse_strategy": "playwright", "listing_url": "https://socar.az/socar/en/page/media"},  # +6
    "BP": {"parse_strategy": "playwright", "listing_url": "https://www.bp.com/en/global/corporate/news-and-insights/press-releases.html"},  # +3 (был 403 для request)
    "TechnipFMC": {"parse_strategy": "playwright", "listing_url": "https://www.technipfmc.com/en/media/press-releases/", "network_region": "external"},  # +6 (был 403); с РФ таймаут → external
    "Halliburton": {"parse_strategy": "playwright", "listing_url": "https://www.halliburton.com/en/about-us/press-release", "network_region": "external"},  # +6; с РФ таймаут → external
    "TotalEnergies": {"parse_strategy": "playwright", "listing_url": "https://totalenergies.com/news/press-releases"},  # рендер ✓ (релизы с датами), нет свежее 28 мая
    "Aker Solutions": {"parse_strategy": "playwright", "listing_url": "https://www.akersolutions.com/news/"},  # рендер ✓, нет свежее фев 2026
    "Rystad Energy": {"parse_strategy": "playwright", "listing_url": "https://www.rystadenergy.com/news", "network_region": "external"},  # рендер ✓ (свежак 08 июня); с РФ таймаут → external
    "Journal of Petroleum Technology": {"parse_strategy": "playwright", "listing_url": "https://jpt.spe.org/latest-news", "network_region": "external"},  # рендер ✓; с РФ таймаут → external
    # Wood Mackenzie / Petroleum Economist: listing рендерится, но даёт навигацию/SPA-
    # оболочку — нужен listing_selector (тюнинг отдельно). Помечаем external (с РФ
    # вообще таймаут), чтобы хотя бы рендер шёл из NL; шум отсеет AI-релевантность.
    "Wood Mackenzie": {"parse_strategy": "playwright", "listing_url": "https://www.woodmac.com/press-releases/", "network_region": "external"},
    "Petroleum Economist": {"parse_strategy": "playwright", "listing_url": "https://www.petroleum-economist.com/", "network_region": "external"},
    # Deloitte #84 / QatarEnergy #64 / Weatherford #25 — гео-падают с РФ, но имена в БД
    # не уверены / ценность спорна. Помечаются по id через `set-source-region` (см. CLI).
    # Telegram-каналы с исправленным username (в Excel-сидере были неверные → t.me/s/
    # отдавал пустую ~9.6KB-страницу, posts=0). Правильные проверены на проде (17-20 постов):
    "Газбатюшка": {"parse_strategy": "telegram", "url": "https://t.me/papagaz"},
    "Агентство нефтегазовой информации": {"parse_strategy": "telegram", "url": "https://t.me/oilgasinform"},
    "Новая Энергия": {"parse_strategy": "telegram", "url": "https://t.me/novayaenergiya"},
    "Energy Today": {"parse_strategy": "telegram", "url": "https://t.me/energytodaygroup"},
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
            new_rss = fields.get("rss_url")
            new_url = fields.get("url")
            new_region = fields.get("network_region")
            row = conn.execute(
                "SELECT id, parse_strategy, listing_url, rss_url, url, network_region FROM sources WHERE name = %s",
                (name,),
            ).fetchone()
            if row is None:
                not_found += 1
                logger.warning("source override: источник %r не найден в БД", name)
                continue
            source_id, cur_strategy, cur_listing, cur_rss, cur_url, cur_region = row
            listing_changed = new_listing is not None and (cur_listing or "") != new_listing
            rss_changed = new_rss is not None and (cur_rss or "") != new_rss
            url_changed = new_url is not None and (cur_url or "") != new_url
            region_changed = new_region is not None and (cur_region or "auto") != new_region
            if (cur_strategy == new_strategy and not listing_changed and not rss_changed
                    and not url_changed and not region_changed):
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
            if new_rss is not None:
                sets.append("rss_url = %(rss_url)s")
                params["rss_url"] = new_rss
            if new_url is not None:
                sets.append("url = %(url)s")
                params["url"] = new_url
            if new_region is not None:
                sets.append("network_region = %(network_region)s")
                params["network_region"] = new_region
            conn.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id = %(id)s", params)
            changed += 1
            logger.info("source override: %s → %s%s%s%s%s", name, new_strategy,
                        f" listing={new_listing}" if new_listing else "",
                        f" rss={new_rss}" if new_rss else "",
                        f" url={new_url}" if new_url else "",
                        f" region={new_region}" if new_region else "")
        conn.commit()
    return {"changed": changed, "unchanged": unchanged, "not_found": not_found}
