<h2 align="center">OilTech Digest: мониторинг нефтесервисных публикаций, технологический радар и месячный выпуск</h2>

<p align="center">
  <a href="requirements.txt"><img src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white" alt="Python 3.13"></a>
  <a href="oiltech_digest/api.py"><img src="https://img.shields.io/badge/FastAPI-0.125-009688?logo=fastapi&logoColor=white" alt="FastAPI 0.125"></a>
  <a href="oiltech_digest/db/schema.sql"><img src="https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL 16"></a>
  <a href="frontend/package.json"><img src="https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black" alt="React 18"></a>
  <a href="frontend/package.json"><img src="https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&logoColor=white" alt="TypeScript 5"></a>
  <a href="docker-compose.yml"><img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose"></a>
</p>

<div align="center">
  <img src="assets/logo.svg" alt="OilTech Digest" width="50%">
</div>

<br>

<p align="center">
  📚 <a href="docs/project_guide.md">Гид по проекту</a>
  &nbsp;|&nbsp;
  🧱 <a href="docs/architecture.md">Архитектура</a>
  &nbsp;|&nbsp;
  🧪 <a href="docs/testing.md">Тестирование</a>
</p>

OilTech Digest собирает публикации нефтесервисной отрасли для **нефтегазовой компании**: новости сайтов с RSS и без него, страницы на JavaScript, открытые каналы Telegram. Каждую новую статью смотрит агент **«Отбор»** и решает, брать ли её. Принятая получает **суть и русский заголовок**, агент **«Карточка»** ставит тему, атрибуты события и **баллы**. **Технологический радар** сам ищет материалы встроенным поиском модели и сводит дубли с лентой. Из отмеченного людьми раз в месяц собирается **выпуск**: PDF в формате эксперта заказчика, письмо и DOCX.

В рабочей установке система разнесена на два контура. **Ядро** (РФ) держит базу PostgreSQL, API на FastAPI, интерфейс на React, планировщик и фоновые воркеры. **Воркеры NL** зовут модели OpenAI и браузер; своей базы у них нет, ядро выдаёт им задачи с полным входом по HTTPS и само записывает итог. Без внешнего контура всё идёт на одном сервере: это режим по умолчанию.

### ✨ Возможности

- 📥 **Сбор из каталога источников** (в сиде 120, новые заводятся с экрана): RSS, разделы новостей без RSS, страницы на JavaScript через Chromium, открытые каналы Telegram.
- 🧹 **Чистый текст**: полный текст по ссылке, проверка, что тело принадлежит статье, срез хвостов вроде «Читайте также» и комментариев.
- 🎯 **«Отбор»**: один агент решает, брать ли публикацию (статью ленты, находку радара, материал коллеги), и ставит категорию, отрасль, связь с нефтесервисом или причину отказа.
- 📝 **«Суть и перевод»**: суть по-русски и русский заголовок по правилам выпуска (до 90 знаков, без тире и названия издания), термины сверяются с нефтегазовым словарём.
- 📇 **«Карточка»**: тема из 13 тематик заказчика, тип события, страны, класс и зрелость технологии, участники с цитатой, дата события; второй вызов ставит баллы.
- 📡 **Технологический радар**: поиск по 13 темам встроенным поиском модели, докачка страниц находок, «Дедуп» с лентой и прошлыми карточками.
- 🔁 **Перепечатки**: одна новость из разных изданий остаётся одной карточкой, копии помечаются и уходят из ленты, пометку можно снять.
- 📰 **Бизнес-лента и радар на экране**: карточки событий с публикациями, фильтры по атрибутам, «всё про компанию» вместе с дочерними обществами, оценки людей.
- 📄 **Месячный выпуск**: PDF в формате эксперта (обложка и листы карточек), письмо и DOCX; заголовок, суть и гипотезу правят прямо в выпуске.
- 💬 **«Материалы коллег»**: бот в Telegram принимает ссылку, файл или событие с оценкой автора; материал проходит «Отбор», итог бот сообщает автору.
- 📑 **Документы**: PDF, DOCX и PPTX до 25 МБ разбираются по фрагментам, у каждого числа в карточке документа указано место в тексте.
- 🔐 **Вход по коду допуска**: новая учётка входит только с одноразовым кодом от подтверждающих (чат Telegram или команда на сервере), открытой регистрации нет.
- 📊 **Учёт расходов ИИ**: каждый вызов модели записан с токенами и стоимостью, сводка на экране «Статистика».

### Что нового

- **[2026.11]** Снята старая цепочка: гейт ленты, тег и балл статьи, прежний поиск радара с судьёй. Брать ли публикацию, решает один агент «Отбор».
- **[2026.11]** PDF выпуска в формате эксперта: обложка на весь первый лист и листы карточек. Заголовок, суть, гипотезу и картинку материала правят на экране «Дайджест».
- **[2026.11]** Радар ставится раз в N суток (в рабочей установке раз в 3 дня, окно поиска 3 дня). Страницы находок добираются через ядро РФ, браузер и чтение PDF.
- **[2026.11]** «Дедуп» сверяет находки радара с лентой: событие, которое уже есть в ленте, второй карточкой не становится.
- **[2026.11]** У нового источника есть дата начала сбора: архив его ленты старше двух недель не собирается.
- **[2026.11]** Шрифт бренда приходит с сервера (`/brand/fonts/`), файлов шрифта в репозитории нет.

---

## Содержание

- [Быстрый старт](#-быстрый-старт)
  - [Установка](#установка)
  - [Запуск в Docker](#запуск-в-docker)
  - [Запуск без Docker](#запуск-без-docker)
  - [Первый вход](#первый-вход)
  - [Тесты](#тесты)
- [Как устроено](#-как-устроено)
- [Агенты и модули](#-агенты-и-модули)
- [Настройка](#-настройка)
- [Развёртывание](#-развёртывание)
- [Документация](#-документация)
- [Ограничения](#-ограничения)
- [Лицензия](#-лицензия)

---

## 🚀 Быстрый старт

### Установка

```bash
git clone https://github.com/electromop/oiltech-digest.git
cd oiltech-digest
cp .env.example .env
```

В `.env` задайте минимум:

```bash
POSTGRES_PASSWORD=...        # пароль базы: без него compose не стартует
DATABASE_URL=postgresql://oiltech:...@localhost:5432/oiltech_digest   # для запуска без Docker
OPENAI_API_KEY=...           # ключ OpenAI для всех вызовов моделей
AUTH_COOKIE_SECURE=0         # только для входа по http на своей машине
```

> **Важно:** у агентов, «Дедупа», судьи перепечаток и разбора документов своя модель без отката на `OPENAI_MODEL`: `OTBOR_MODEL`, `SIGNAL_CARD_MODEL`, `SIGNAL_SCORES_MODEL`, `SIGNAL_RESEARCH_MODEL`, `SIGNAL_DEDUP_MODEL`, `OPENAI_REPRINT_MODEL`, `OPENAI_DOC_MODEL`. Строка пустая: шаг отказывает и пишет причину.

### Запуск в Docker

```bash
# база, разовый bootstrap (схема и сиды), приложение и Caddy
docker compose up -d --build

# плюс конвейер: планировщик, воркер задач и воркер браузера (профиль pipeline)
docker compose --profile pipeline up -d --build

docker compose ps                  # bootstrap: exited (0), остальные running
docker compose logs -f scheduler   # шаги цикла планировщика
```

Интерфейс открывается на `http://localhost:8000`. PostgreSQL слушает только `localhost:5432`, снаружи виден один Caddy (порты 80 и 443). Статью по ссылке можно внести руками: `scripts/manual-add-article.sh <адрес>`.

> **Важно:** образ ставит Chromium для печати PDF, это около 400 МБ. На сервере с малым объёмом памяти собирайте образ при остановленном стеке.

### Запуск без Docker

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium        # печать PDF

docker compose up -d db                      # только PostgreSQL
python -m oiltech_digest.cli init-db         # схема, повтор безопасен
python -m oiltech_digest.cli seed-sources    # каталог источников из data/seed
python -m oiltech_digest.cli seed-tags       # 13 тематик заказчика
python -m oiltech_digest.cli seed-scoring    # критерии баллов

(cd frontend && npm ci && npm run build)     # нужен Node.js, интерфейс в frontend/dist
uvicorn oiltech_digest.api:app --reload --port 8000
```

Конвейер можно пройти руками. Задачи агентов исполняет `jobs-worker`, его держат в отдельном терминале:

```bash
python -m oiltech_digest.cli jobs-worker --queue default --queue ai   # исполнитель задач

python -m oiltech_digest.cli parse --workers 3              # сбор лент в articles
python -m oiltech_digest.cli fetch-full-text --limit 50     # полный текст по ссылке
python -m oiltech_digest.cli enqueue-otbor                  # задачи «Отбора»
python -m oiltech_digest.cli process --limit 20             # суть и перевод принятых статей
python -m oiltech_digest.cli <команда> --help               # описание любой команды
```

Без ключа OpenAI суть и перевод проверяют на заглушке: `process --offline`.

### Первый вход

Открытой регистрации нет (`AUTH_ALLOW_SELF_REGISTRATION=false`), учётки заводит администратор. Новая учётка входит только с кодом допуска: восемь цифр, действует 24 часа, пять попыток.

```bash
python -m oiltech_digest.cli create-user --email <почта> --password <пароль> --admin
python -m oiltech_digest.cli admission-code --email <почта>   # печатает код
```

В Docker те же команды идут через `docker compose exec app python -m oiltech_digest.cli ...`. Код вводят на экране входа после пароля. Если включена отправка в Telegram (`ADMISSION_NOTIFY_ENABLED=1`), код сам уходит в закрытый чат подтверждающих.

### Тесты

```bash
docker compose up -d db
python -m pytest -q              # весь набор, нужна база из DATABASE_URL
docker compose run --rm test     # тот же набор в контейнере с dev-зависимостями

cd frontend
npm ci
npm test                         # Vitest
npm run build                    # проверка типов tsc и сборка Vite
```

Тесты с базой заводят временную схему и удаляют её после себя. Перед первым таким тестом проверяется, что часы базы не скачут; отключить проверку можно `OILTECH_TEST_ALLOW_CLOCK_DRIFT=1`.

---

## 🧭 Как устроено

<div align="center">
  <img src="assets/pipeline.svg" alt="Конвейер: ядро РФ и воркеры NL" width="90%">
</div>

Путь статьи от источника до выпуска:

1. Планировщик (`scripts/docker-scheduler.sh`) раз в цикл (по умолчанию 6 часов) обходит включённые источники, дозагружает полный текст и срезает хвосты. Сайты, закрытые для российских адресов, собирает воркер NL (`FETCH_EXTERNAL_ENABLED=1`).
2. Каждая новая статья, находка радара и материал коллеги уходят «Отбору» пачками по 20. Отклонённую дальше ничто не обрабатывает и не оплачивает.
3. Принятая статья получает суть и русский заголовок.
4. Основной вызов «Карточки» ставит тему и атрибуты события, второй ставит баллы по профилю категории (бизнес или технология) и ступень: сильный, полезный, наблюдать, слабый.
5. Бизнес-лента и технологический радар показывают одну карточку на событие с публикациями под ней. Люди ставят оценки, комментарии и отметку «в дайджест».
6. Отмеченное за месяц собирается в выпуск: PDF печатает Chromium, письмо и DOCX собираются из того же состава.

Радар ставит планировщик первым циклом после 00:00 по Москве, раз в `SIGNAL_DISCOVERY_DAILY_EVERY_DAYS` суток. На каждую из 13 тем идёт один вызов модели со встроенным поиском. Воркер докачивает страницы находок (запросом, через ядро РФ, браузером; у PDF читаются первые страницы), отбрасывает технический брак и сводит находки одного события: правило отбирает пары, модель отвечает, одно ли это событие. Находка события, которое уже есть в ленте, отдаёт ссылку сигналу ленты. Остальное решает «Отбор».

Ядро и воркеры NL говорят только по HTTPS, и связь всегда начинает воркер: забирает задачу (`POST /api/external-worker/claim`), продлевает аренду, сообщает прогресс и сдаёт итог (`complete`, `fail`, на остановке `release`). Ядро хранит только SHA-256 токена воркера. Воркер сообщает ядру сборку и номер контракта (`oiltech_digest/contract.py`), а `check-lanes` поднимает тревогу, если NL не пересобран.

| Контейнер NL | Очередь | Что делает |
|---|---|---|
| `external-worker` | `external-ai` | поток дня: «Отбор», суть и перевод, «Карточка», баллы, перепечатки, документы |
| `external-worker-bulk` | `external-ai-bulk` | пересчёты корпуса и повторы по номерам (`AI_BULK_LANE_ENABLED=1`) |
| `external-worker-fetch` | `external-fetch`, `external-notify` | сбор запросом в три потока, код допуска в Telegram |
| `external-worker-browser` | `external-playwright` | сбор браузером, один поток |
| `external-worker-agents` | `external-agents` | прогон радара: поиск, докачка страниц, «Дедуп» |
| `tg-materials-bot` | нет | бот «Материалы коллег», пишет в ядро запросом |

На ядре работают `db` (PostgreSQL 16), разовый `bootstrap` (`init-db` и сиды), `app` (FastAPI и собранный интерфейс), `worker` (очереди `default` и `ai`), `playwright-worker` (сайты на JavaScript и печать PDF), `scheduler` и `caddy`. Планировщик идёт под замком в PostgreSQL: второй экземпляр шагов не делает и ждёт.

Где что лежит:

```text
oiltech_digest/          ядро и воркеры
  api.py                 FastAPI: интерфейс, API, протокол внешних задач
  cli.py                 все команды: python -m oiltech_digest.cli <команда>
  config.py              настройки из окружения
  lanes.py               полосы внешних воркеров
  ingestion/             сбор: RSS, листинги, браузер, Telegram, полный текст, хвосты
  processing/            суть и перевод, перепечатки, выпуск: письмо, DOCX, PDF
  db/                    schema.sql и запросы к PostgreSQL
  documents/             разбор PDF, DOCX и PPTX
  otbor.py               «Отбор»
  signal_card.py         «Карточка», основной вызов
  signal_scores.py       «Карточка», баллы
  signal_discovery.py    прогон радара
  signal_dedup.py        «Дедуп» радара
  tg_materials_bot.py    бот «Материалы коллег»
frontend/                интерфейс: React, TypeScript, Vite
scripts/                 цикл планировщика, выкат ядра и NL, ручной импорт статьи
tests/                   pytest
docs/                    архитектура, проверка, гид, ADR
data/seed/               сид источников и тематик
assets/                  картинки этого README
```

---

## 🤖 Агенты и модули

| Модуль | Что делает | Полоса NL | Модель | Статус |
|---|---|---|---|---|
| «Отбор» (`otbor.py`) | брать или нет, категория, отрасль, связь с нефтесервисом, причина отказа | `external-ai` | `OTBOR_MODEL` | 🟢 каждый цикл |
| «Суть и перевод» (`processing/pipeline.py`, `signal_summary.py`) | суть и русский заголовок принятой публикации | `external-ai` | `OPENAI_SUMMARY_MODEL`, `OPENAI_TRANSLATE_MODEL` | 🟢 каждый цикл |
| «Карточка» (`signal_card.py`) | тема из 13, атрибуты, страны, участники, дата события | `external-ai` | `SIGNAL_CARD_MODEL` | ✅ `SIGNAL_CARD_ENABLED=1` |
| Баллы «Карточки» (`signal_scores.py`, `signal_scoring.py`) | баллы по критериям профиля, ступень, «Решение платформы» | `external-ai` | `SIGNAL_SCORES_MODEL` | 🟢 каждый цикл |
| Поиск радара (`signal_discovery.py`, `signal_research.py`) | материалы по 13 темам, докачка страниц | `external-agents` | `SIGNAL_RESEARCH_MODEL` | 🟢 раз в N суток |
| «Дедуп» радара (`signal_dedup.py`) | одно событие в одной карточке, сверка с лентой | `external-agents` | `SIGNAL_DEDUP_MODEL` | 🟢 в прогоне радара |
| Перепечатки (`processing/reprints.py`) | одна новость в разных изданиях | `external-ai` | `OPENAI_REPRINT_MODEL` | 🟢 раз в 12 часов |
| Разбор документов (`documents/`) | карточка документа, у каждого числа место в тексте | `external-ai` | `OPENAI_DOC_MODEL` | ✅ внешний контур |
| Бот «Материалы коллег» (`tg_materials_bot.py`) | приём материалов из рабочей группы | свой контейнер | нет | ✅ `TG_MATERIALS_BOT_TOKEN` |
| Код допуска (`admission.py`) | отправка кода в закрытый чат | `external-notify` | нет | ✅ `ADMISSION_NOTIFY_ENABLED=1` |

🟢 работает по циклу планировщика или по событию, нужна только модель в настройках. ✅ есть в коде, включается своей настройкой.

«Решение платформы» ставит код, без модели: сильный сигнал с тремя публикациями и больше получает «подтверждено», с меньшим числом «кандидат», полезный и ниже «наблюдать». Модели «Отбора» и «Карточки» выбирает экзамен на разметке эксперта заказчика: `otbor-exam`, `signal-card-exam`, `card-scores-exam`; отчёт пишется рядом с манифестом пакета.

---

## 🔧 Настройка

Все переменные с пояснениями лежат в [`.env.example`](.env.example) и [`oiltech_digest/config.py`](oiltech_digest/config.py). Ядро читает `.env`, воркеры NL читают `.env.external-worker` (в git его нет).

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `DATABASE_URL` | локальная база | подключение к PostgreSQL |
| `OPENAI_API_KEY` | пусто | ключ OpenAI там, где зовут модели |
| `OPENAI_MODEL` | `gpt-6-luna` | откат стадий без своей модели (суть и перевод) |
| `OTBOR_MODEL`, `SIGNAL_CARD_MODEL`, `SIGNAL_SCORES_MODEL` | пусто | модели «Отбора» и «Карточки», едут воркеру в задаче |
| `OPENAI_SUMMARY_MODEL`, `OPENAI_TRANSLATE_MODEL` | `OPENAI_MODEL` | суть и русский заголовок |
| `SIGNAL_RESEARCH_MODEL` | пусто | поиск радара, задаётся на ядре |
| `SIGNAL_DEDUP_MODEL`, `OPENAI_REPRINT_MODEL` | пусто | «Дедуп» радара и судья перепечаток |
| `SIGNAL_CARD_ENABLED` | `0` | `1` включает основной вызов «Карточки» |
| `SIGNAL_DISCOVERY_DAILY_EVERY_DAYS` | `1` | раз в сколько суток ставить радар |
| `SIGNAL_DISCOVERY_DAYS`, `SIGNAL_RESEARCH_DAYS` | `14`, `30` | период поиска радара в днях, берётся большее из двух |
| `SIGNAL_SCORE_BANDS` | `80,65,40` | нижние границы ступеней «сильный», «полезный», «наблюдать» |
| `SIGNAL_CONFIRMED_PUBLICATIONS` | `3` | сколько публикаций нужно сильному сигналу для «подтверждено» |
| `CYCLE_INTERVAL_SECONDS` | `21600` | цикл планировщика, 6 часов |
| `REPRINTS_INTERVAL_HOURS` | `12` | период поиска перепечаток, `0` выключает шаг |
| `EXTERNAL_WORKERS_ENABLED`, `AI_EXECUTION_REGION` | `0`, `ru` | `1` и `external` отдают вызовы моделей воркерам NL |
| `FETCH_EXTERNAL_ENABLED` | `0` | сбор закрытых для РФ сайтов через NL |
| `LINKEDIN_SOURCES_PER_CYCLE`, `LINKEDIN_SPACING_MINUTES` | `8`, `10` | сколько страниц компаний LinkedIn за цикл и пауза между ними |
| `LINKEDIN_MIN_INTERVAL_HOURS`, `LINKEDIN_BLOCK_COOLDOWN_HOURS` | `20`, `24` | заход на компанию не чаще, пауза всем компаниям после стены входа |
| `EXTERNAL_WORKER_TOKEN_HASH` | пусто | SHA-256 токена воркера, на ядре |
| `CORE_API_URL`, `EXTERNAL_WORKER_TOKEN` | пусто | адрес ядра и токен, на воркере NL |
| `DIGEST_BRANDING_PATH` | файл из пакета | брендинг выпуска, на сервере файл в томе `branding` |
| `BRAND_FONTS_DIR` | `fonts/` рядом с брендингом | каталог шрифта бренда |
| `AUTH_COOKIE_SECURE` | `true` | `0` только для входа по http на своей машине |
| `ADMISSION_NOTIFY_ENABLED` | `0` | `1`: код допуска уходит в Telegram через воркер NL |
| `TG_MATERIALS_BOT_TOKEN`, `TG_MATERIALS_CHAT_IDS` | пусто | бот «Материалы коллег» и его рабочие группы, на NL |
| `PROXY_URL`, `PROXY_HOST_OVERRIDES` | пусто | прокси сбора, общий и по доменам |
| `AI_OFFLINE` | `0` | `1` гонит суть и перевод ленты на заглушке без API |

> **Важно:** при внешнем контуре настройку модели читает тот процесс, который зовёт модель. Модели сути, перевода, «Дедупа», перепечаток и документов задают в `.env.external-worker` на NL, строка в `.env` ядра на них не действует. Модели «Отбора», «Карточки» и поиска радара читает ядро и передаёт воркеру в задаче.

Брендинг выпуска лежит в томе `branding` (`/app/branding/digest_branding.json`), шрифт бренда в его подкаталоге `fonts/`: `brand-sans-regular.ttf`, `brand-sans-bold.ttf`, `brand-sans-condensed-regular.ttf`, `brand-sans-condensed-bold.ttf`. Интерфейс берёт файлы по адресу `/brand/fonts/`, печать PDF и письмо встраивают их из того же каталога. Файла нет: работает запасной шрифт (Arial, Arial Narrow). Почтовые клиенты вырезают встроенный шрифт, а Word берёт шрифт по имени, поэтому имя шрифта бренда, установленного у читателей, задаёт блок `fonts` того же файла (`office_family`, `office_condensed_family`); пусто: Arial.

---

## 🚢 Развёртывание

Ядро и воркеры выкатываются порознь, сначала ядро: оно понимает и прежние сборки воркеров, и новые.

```bash
# ядро РФ, из каталога репозитория на сервере: только названные сервисы
sh scripts/deploy-core.sh --no-schema app worker playwright-worker scheduler
sh scripts/deploy-core.sh --schema app       # если менялся schema.sql

# воркеры NL: по одному, с мягкой остановкой
sh scripts/deploy-nl.sh                       # все шесть контейнеров
sh scripts/deploy-nl.sh external-worker-agents
```

`deploy-core.sh` подтягивает `origin/main` (`git fetch` и `reset --hard`), собирает названные сервисы, пока старые работают, и отказывает, если идут оплаченные ИИ-задачи (`live-ai-leases`, обход `--force`). Затем перезапускает только названные сервисы, ждёт health и запускает сторож `check-lanes`. Если `schema.sql` менялся с прошлого выката, нужен явный выбор: `--schema` (прогон `init-db`) или `--no-schema` (новые таблицы и колонки созданы руками заранее).

`deploy-nl.sh` собирает образы с SHA коммита и перезапускает контейнеры по одному. Воркер возвращает задачи ядру (до 120 с) и обязан отметиться в ядре новой сборкой, иначе выкат останавливается и остальные контейнеры не трогает.

Первое включение внешнего контура:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # токен воркера
printf '%s' '<токен>' | sha256sum                                # его SHA-256 для ядра
```

1. На ядре в `.env`: `EXTERNAL_WORKERS_ENABLED=1`, `AI_EXECUTION_REGION=external`, `EXTERNAL_WORKER_TOKEN_HASH=<SHA-256>`; для сбора закрытых сайтов ещё `FETCH_EXTERNAL_ENABLED=1`.
2. На NL в `.env.external-worker`: `CORE_API_URL`, `EXTERNAL_WORKER_TOKEN`, `OPENAI_API_KEY` и модели стадий воркера; для бота `TG_MATERIALS_BOT_TOKEN` и `TG_MATERIALS_CHAT_IDS`.
3. На NL: `sh scripts/deploy-nl.sh`.

> **Важно:** `init-db` на живой базе идёт одной транзакцией с массовыми UPDATE и держит эксклюзивный замок на `articles` и `background_jobs`: лента и выдача задач NL на это время встают. Поэтому `--schema` только осознанно, а новую колонку проще добавить заранее точечным `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.

> **Важно:** NL не выкатывают во время прогона радара (первый цикл после 00:00 по Москве, поиск идёт до двух часов). Прогон в `external-worker-agents` по частям не сохраняется и начнётся заново, поиск оплатится второй раз. Проверка на ядре: `python -m oiltech_digest.cli external-queues-status`, у `external-agents` должно быть `running=0`.

`Caddyfile` смонтирован в контейнер одним файлом, и после `git reset` контейнер видит прежнюю копию. Новую конфигурацию загружают так:

```bash
docker exec -i oiltech_caddy caddy reload --config /dev/stdin --adapter caddyfile < Caddyfile
```

---

## 📚 Документация

| Тема | Документ |
|---|---|
| Гид по проекту и коду | [docs/project_guide.md](docs/project_guide.md) |
| Архитектура и поток данных | [docs/architecture.md](docs/architecture.md) |
| Проверка по слоям и smoke-сценарии | [docs/testing.md](docs/testing.md) |
| Бот «Материалы коллег»: настройка и выкат | [docs/tg_materials_bot.md](docs/tg_materials_bot.md) |
| Архитектурные решения (ADR) | [docs/adr/](docs/adr/) |

Описание любой команды: `python -m oiltech_digest.cli <команда> --help`. Задачи и замечания ведутся в [GitHub Issues](https://github.com/electromop/oiltech-digest/issues).

---

## 🚧 Ограничения

- Сайты, которые требуют пройти проверку браузера или капчу, не собираются: страница проверки за текст издателя не считается.
- Telegram читается только по открытым каналам через страницу предпросмотра канала, закрытые каналы не поддерживаются.
- LinkedIn читается только по публичной странице компании без входа, через NL: последние 10 постов компании. Аккаунт и куки не используются. Частые заходы LinkedIn закрывает стеной входа, тогда сбор всех компаний встаёт на паузу на сутки.
- OpenAI отвечает 403 на запросы с российских адресов, поэтому при ядре в РФ все вызовы моделей идут через воркеры NL.
- Прогон радара по частям не сохраняется. Встроенный поиск модели оплачивается сверх токенов (`SIGNAL_WEB_SEARCH_USD_PER_CALL`).
- Перепечатки и дубли радара только помечаются, удаления нет. Ошибочную пометку снимают командами `reprints` и `unmerge-signal`.
- Стоимость вызова считается по таблице `OPENAI_MODEL_PRICES` в `config.py`. Модель без своей строки считается по ставке по умолчанию, поэтому новую модель сначала вносят в таблицу.
- Загрузка документов работает только с внешним контуром: без него сервер отказывает на входе.
- Том `documents` с оригиналами загруженных файлов в `pg_dump` не попадает, его копируют отдельно.
- Без Chromium выгрузка PDF отвечает 503.

---

## 📄 Лицензия

Все права защищены. Открытой лицензии у кода нет: использовать и распространять его можно только с разрешения правообладателя.
