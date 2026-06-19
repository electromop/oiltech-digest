# OilTech Digest — handoff на 2026-06-10

_Документ самодостаточный. Фиксирует состояние после большого блока работ по источникам («до РФ»), дайджесту, UI и перформансу._

---

## 1. Главное

Закрыт весь комплекс по **источникам, который возможен без отдельной инфраструктуры**, переработан **дайджест** по референсу, внесены **UI-правки** по фидбэку и устранена **проблема производительности**.

- 12 коммитов в `main`: `adf7a14` → `ab714b1`, всё запушено в `origin/main`, рабочее дерево чистое.
- Прод деплоился поэтапно в течение сессии. **Последние коммиты (`a092781` формат дайджеста, `ab714b1` backfill-images) требуют финального деплоя + запуска `backfill-images` на проде.**

---

## 2. Что сделано

### Источники — комплекс «до РФ инфры» закрыт (≈150 статей собрано)

- **Анти-заморозка `request_parser`** (`adf7a14`) — найден и устранён корень «обрыва 30 мая»: дедуп-заморозка (short-circuit по `last_listing_hash`, break по `last_seen_article_url` / `known_streak`). Корпоративные newsroom без даты в URL застывали на первом улове. Теперь дедуп только через `article_exists`.
- **RSS (6 лент)** — Endeavor (`Oil & Gas Journal` / `Offshore` / `Automation World`: издатель сменил схему фида на `?input={"sectionAlias":"home"}`) + `World Oil` / `Hydrocarbon Processing` / `EIA`.
- **request + newsroom** — `Eni` / `Petrobras` / `IEA` / `CNOOC`.
- **playwright JS-корпораты (12 в реестре)** — `SLB`, `ADNOC`, `SOCAR`, `BP`, `TechnipFMC`, `Halliburton`, `TotalEnergies`, `Aker Solutions`, `Rystad`, `JPT` + `Shell`, `Baker Hughes` (`9be7e35`, `dd42717`).
- **Telegram** (`3ae00f9`) — исправлены неверные username в Excel-сидере: оживлены 4 (`papagaz`, `oilgasinform`, `novayaenergiya`, `energytodaygroup`, +74 статьи); выключены 3 дубля и 2 несуществующих.
- **TLS** (`35cf55e`, `10e00ed`) — `http_client` достраивает неполную цепочку через extended CA-bundle (`extra_ca.pem`: Go Daddy G2 / GeoTrust) вместо небезопасного `verify=False`. Лечит HP / Petroleum Economist / SOCAR.
- **Реестр `source_overrides`** — оверрайды `parse_strategy` / `listing_url` / `rss_url` / `url`, идемпотентный `apply-source-overrides` (переживает пересборку БД).

### Hard-WAF — инфраструктура готова, предел зафиксирован

- **playwright + резидентский прокси** (`c73901e`) — `fetch_rendered` ходит через прокси по `PROXY_HOST_OVERRIDES` (точечно, per-host).
- **Вывод пилота:** Cloudflare/Akamai (`EnergyVoice`, `BCG`, `S&P`, `IHS`, `Bloomberg`) **не пробиваются** playwright+residential (hard-block headless по fingerprint). Нужен **challenge-solver** (2captcha `cf_clearance`) — это инфра-уровень, отложено вместе с РФ. 2captcha residential зона `custom` ротирует случайные страны, гео не контролируется.

### Дайджест (BACKLOG #2)

- **Блок «Главное за период»** (`f97e388`) — 3 KPI-плашки (иконка + число + подпись со склонением: новостей / аналитических материалов / возможностей для бизнеса).
- **Формат карточек по референсу коллеги** (`a092781`) — фото + заголовок сверху, ниже краткое описание и строка «Читать далее | теги».
- **Карточки не рвутся между страницами** — убран форсированный `page-break` каждые 3 (он и разрывал), оставлен `break-inside: avoid`.
- **Футер с иконками соцсетей**; выгрузка — только **PDF / DOCX / HTML**.
- **Фото** (`ab714b1`) — у дайджест-статей `image_url` был пуст → CLI `backfill-images` перефетчит страницы и проставит og:image/twitter:image.

### UI-правки (`e88feda`)

- **Источники**: убраны технические поля (listing strategy/селекторы, «последняя статья»); панель «Диагностика» вынесена из свёрнутого `<details>` — результат виден сразу.
- **Добавить источник**: вместо «RSS URL» — просто ссылка на сайт, RSS ищется автоматически (`discover_feed`); нет ленты → `request`. «Частота» — выпадающий список.
- **Скоринг**: карточки критериев переверстаны (2 колонки: параметр+вес / описание / ключевые слова) + воздух вместо 5 полей в тесный ряд.

### Перформанс (`93ff30f`)

- `/api/articles` больше **не тянет `a.raw_text` целиком** (только `length`). Это лечило «сигналы по ~1 секунде» и **таймаут экрана «Дайджест»** (грузит тот же эндпоинт, `limit=5000`). Корень был не в RAM, а в мегабайтном payload.

---

## 3. Что осталось живым

**Прод-операции (сразу после деплоя):**
1. Запустить `backfill-images` на проде (фото в дайджесте) — `with_image` было 0 из 8.
2. Проверить дайджест-экспорт (формат коллеги, фото, без разрывов) и скорость «Сигналов»/«Дайджеста».

**Дайджест-мелочи (ждут материалов заказчика):**
3. Эмблема-лого «Газпром нефть» в шапку — нужен файл PNG/SVG (сейчас текст).
4. Точные бренд-лого соцсетей + список каналов (сейчас узнаваемые кружки с символами).
5. KPI «аналитика/возможности» — сейчас эвристика по источнику/категории, уточнить точные правила.
6. Настоящий `.docx` (сейчас `.doc` = msword-HTML).

**Продукт:**
7. `P3` — **Теги**: дерево / master-detail вместо плоской inline-формы (BACKLOG #3).
8. Доработки экрана «Источники».

**AI / обработка:**
9. Яндекс ИИ — проверить гипотезу РФ-LLM, дать отчёт.
10. Стриминг-пайплайн (`#13`, за флагом `STREAMING_PIPELINE`).
11. `process-full` — дообработать вторую половину базы.

---

## 4. Инфраструктура — в самый конец (разделение архитектур)

Однотипные инфра-задачи, оставлены напоследок:

- 🇷🇺 **РФ-гео (~30 источников)** — недоступны с NL-VPS → нужен РФ-сервер/forward-proxy.
- 🛡️ **Hard-WAF challenge-solver** — Cloudflare/Akamai через 2captcha `cf_clearance`.

Целевая схема (см. `oiltech-server-infra` в памяти): **NL** = UI + домен + Caddy + БД + парсинг иностранных + OpenAI; **РФ** = парсинг РФ-сайтов (+ опц. РФ-LLM), пишет в общую БД через Wireguard.

---

## 5. Важные файлы

**Backend**
- `oiltech_digest/ingestion/request_parser.py` — анти-заморозка
- `oiltech_digest/ingestion/source_overrides.py` — реестр оверрайдов источников
- `oiltech_digest/ingestion/playwright_parser.py` — playwright + прокси
- `oiltech_digest/ingestion/http_client.py` + `ingestion/extra_ca.pem` — TLS-fallback
- `oiltech_digest/ingestion/article_fetcher.py` — `backfill_images`
- `oiltech_digest/processing/digest.py` + `processing/digest_email_template.html` — дайджест
- `oiltech_digest/api.py`, `oiltech_digest/db/repository.py`

**Frontend**
- `frontend/src/features/sources/SourcesPage.tsx`, `SourceCard.tsx`
- `frontend/src/features/scoring/ScoringPage.tsx`
- `frontend/src/features/digest/DigestPage.tsx`
- `frontend/src/styles/globals.css`

---

## 6. Деплой без потери базы

```bash
cd ~/oiltech-digest && git pull
docker exec oiltech_pg pg_dump -U oiltech oiltech_digest | gzip > ~/backup_$(date +%F).sql.gz   # бэкап
docker compose down && docker compose up -d --build                                              # rebuild
docker exec oiltech_app python -m oiltech_digest.cli apply-source-overrides                       # закрепить реестр
docker exec oiltech_app python -m oiltech_digest.cli backfill-images --limit 300                  # фото для дайджеста
```

⚠️ На проде 1.9 ГБ RAM + swap 2 ГБ — **`down` перед `build`** (иначе OOM). Никогда не делать `docker compose down -v` (удалит volume с БД). БД в named volume `pgdata` — обычная пересборка её не трогает.

---

## 7. Рекомендуемый следующий шаг

1. Финальный деплой + `backfill-images` + визуальная проверка дайджеста и скорости админки.
2. Получить от заказчика лого-эмблему и список соцсетей → добить дайджест 1-в-1.
3. **Теги** — редактор-дерево (BACKLOG #3).
4. Затем — Яндекс ИИ и/или начать проработку РФ-архитектуры (Фаза РФ).
