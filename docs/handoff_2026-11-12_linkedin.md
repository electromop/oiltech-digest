# Страницы компаний LinkedIn — ветка `feat/linkedin-company-posts` (12.11)

Описание PR и хендофф одним файлом. Перед слиянием в `main` файл можно убрать: рабочие документы
живут вне репозитория (#184).

## Зачем

Компании нефтесервиса и стартапы часто сначала пишут в LinkedIn: о пилотах, внедрениях, новых
сервисах. Стартапы без своей пресс-службы пишут только там. Пример 11.11: Halliburton — сервис
IntelliTTCE (оценка цементажа через НКТ до ликвидации скважины), 10.11: Baker Hughes — первая
установка бурения на гибкой трубе для Saudi Aramco.

## Как работает

Новый тип источника `linkedin` (`oiltech_digest/ingestion/linkedin_parser.py`):
- источник — одна компания, адрес `https://www.linkedin.com/company/<имя>/`;
- сбор только через NL (`network_region = 'external'`, очередь `external-playwright`): из РФ
  LinkedIn закрыт;
- без входа, без аккаунта и кук. Страница компании без входа отдаёт последние 10 постов в разметке
  schema.org (`SocialMediaPosting`): полный текст, точная дата, адрес поста. Один рендер на компанию,
  посты по одному не открываются;
- репосты чужих авторов отбрасываются; «жирный» Юникод (Welltec: «𝗔 𝗴𝗿𝗼𝘂𝗻𝗱𝗯𝗿𝗲𝗮𝗸𝗶𝗻𝗴…») приводится к
  обычным буквам (NFKC); заголовок — первое предложение поста без ссылок `lnkd.in`;
- пост становится статьёй ленты и дальше идёт обычным путём: предфильтр, «Отбор», суть и перевод,
  «Дедуп»;
- посты не уходят в дозагрузку текста (`external_refetch_candidates`) и в `discover-rss`.

**Бережный режим.** Замер 12.11: шестой заход за две минуты с одного адреса — стена входа (код 999),
следующий — проверка Cloudflare. Поэтому:
- компания — не чаще раза в `LINKEDIN_MIN_INTERVAL_HOURS`;
- за цикл планировщика — не больше `LINKEDIN_SOURCES_PER_CYCLE` компаний, давние первыми;
- задачи цикла разнесены на `LINKEDIN_SPACING_MINUTES` (`run_after`): браузерный воркер NL один поток,
  без разноса компании шли бы подряд;
- одна попытка на задачу; вторая задача той же компании, пока первая в очереди, не ставится;
- стена входа или проверка → `stats.listing = blocked:authwall | blocked:challenge` (а не «новостей
  нет») и пауза всем компаниям LinkedIn на `LINKEDIN_BLOCK_COOLDOWN_HOURS`
  (`sources.external_cooldown_until`): блок по адресу NL, а не по компании. Задачи, поставленные до
  блока, на паузе не выдаются.

**Контракт NL 12** (`contract.py`): сборка 11 источник `linkedin` не знает; ядро ей такую задачу не
выдаёт, а валит с причиной «пересоберите NL».

## Настройки (ядро, `.env`)

Значения по умолчанию рассчитаны на цикл 6 ч; задавать не нужно.

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `LINKEDIN_MIN_INTERVAL_HOURS` | `20` | заход на компанию не чаще |
| `LINKEDIN_SOURCES_PER_CYCLE` | `8` | компаний за цикл планировщика (8 × 4 цикла = 32 захода в сутки) |
| `LINKEDIN_SPACING_MINUTES` | `10` | пауза между задачами компаний одного цикла |
| `LINKEDIN_BLOCK_COOLDOWN_HOURS` | `24` | пауза всем компаниям после стены входа или проверки |

Нужно, как и для прочих западных источников: `EXTERNAL_WORKERS_ENABLED=1`, `FETCH_EXTERNAL_ENABLED=1`.
На NL новых переменных нет.

## Выкат

1. Ядро: `sh scripts/deploy-core.sh --no-schema app scheduler`. Схема не меняется: `run_after`,
   `external_cooldown_until`, `collect_since` уже есть.
2. NL: `sh scripts/deploy-nl.sh` (владелец). До пересборки `check-lanes` показывает
   `contract_mismatch`, задачи LinkedIn старой сборке не выдаются.
3. Завести компании:

   ```bash
   docker exec oiltech_app python -m oiltech_digest.cli add-linkedin-source welltec --name "Welltec (LinkedIn)"
   docker exec oiltech_app python -m oiltech_digest.cli add-linkedin-source sekal-as --name "Sekal (LinkedIn)"
   ```

   Имя — из адреса страницы (`linkedin.com/company/<имя>`); принимается и адрес целиком. Повтор
   обновляет тот же источник. Новый источник берёт посты не старше двух недель (`collect_since`).
4. Ближайший цикл поставит задачи; руками — `enqueue-external-scrape`.

## Проверка после выката

- итог задачи `scrape_source` у источника LinkedIn: `stats.listing = ok`, `listing_candidates` > 0;
- `blocked:authwall` или `blocked:challenge` — LinkedIn закрыл страницы для адреса NL; все компании
  стоят на паузе сутки (`SELECT name, external_cooldown_until FROM sources WHERE parse_strategy = 'linkedin'`);
- статьи: `SELECT a.published_at, a.title FROM articles a JOIN sources s ON s.id = a.source_id
  WHERE s.parse_strategy = 'linkedin' ORDER BY a.id DESC LIMIT 20`.

Снять паузу раньше: `UPDATE sources SET external_cooldown_until = NULL WHERE parse_strategy = 'linkedin'`.

## Проверено

- pytest: 1978 passed, 4 skipped, 0 упавших (полный прогон); после правки NFKC
  `tests/test_linkedin_parser.py` — 24 passed. Без правок кодировки (UTF-8, NFKC) их тесты падают.
- vitest: 175, `tsc -b` — чисто.
- Вживую с машины разработчика (не NL), путь воркера целиком (`external_fetch.process_payload`):

  | Компания | `listing` | Постов | Последний |
  |---|---|---|---|
  | Halliburton | ok | 10 | 12.11 |
  | Baker Hughes | ok | 10 | 11.11 |
  | Welltec | ok | 8 | 05.11 |
  | Sekal | ok | 4 | 16.04 |
  | SLB (`slbglobal`) | blocked | — | — (пятый-шестой заход подряд) |

  Повторный заход на Sekal через минуту — `blocked:challenge`: стена распознаётся, а не считается
  «новостей нет».

**Не проверено:** отдаёт ли LinkedIn страницы без входа адресу NL (дата-центр может получать стену
чаще домашнего адреса). Видно по первым итогам задач.

## Известное

- У Sekal половина постов — вакансии («Drilling Analyst», «Service Delivery Engineer»). В сборщике не
  режутся, отсеивает «Отбор».
- Посты без технологии (праздники, карьерные истории, «спасибо, что зашли на стенд») — тоже «Отбор».
- Без входа видно только 10 последних постов: компания, которая пишет больше 10 постов за 20 ч, часть
  потеряет. У замеренных — 2–5 постов в неделю.
- Диагностика источника с экрана (`source-diagnose`) идёт с ядра РФ и для LinkedIn не работает —
  смотреть итог задачи сбора.
- Адрес компании ≠ бренд: SLB — `slbglobal`, а `slb` — посторонняя британская организация.

## Ревью — на что смотреть

- `linkedin_parser.page_status`: стена приходит с кодом 999, общий рендер её не считает блоком и отдаёт
  страницу; конечный адрес — из `<base href>`, который вписывает рендер.
- `cli.cmd_enqueue_external_scrape`: отбор компаний (`due_sources`), `run_after`, одна попытка.
- `external_fetch.apply_scrape_result` / `build_scrape_source_payload`: пауза всем после блока и отказ
  выдавать ждавшие задачи.
- `repository.create_background_job(delay_seconds=…)` — новый необязательный параметр, у прочих задач 0.

## Дальше

- Список компаний: крупные сервисные (SLB `slbglobal`, Weatherford, NOV, Expro, TechnipFMC, ChampionX,
  Tenaris) и стартапы из ТОП-20 заказчика — согласовать с заказчиком.
- Через неделю: сколько постов LinkedIn прошло «Отбор» и сколько из них новые, а не перепечатки
  пресс-релизов; как часто бывает `blocked`.
