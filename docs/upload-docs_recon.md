<!-- Сгенерировано воркфлоу upload-docs-recon 2026-08-20 (7 агентов, 261 обращение к коду).
Каждое утверждение здесь — со ссылкой файл:строка. Проверять при расхождении с кодом, а не верить на слово. -->

# КОНФОРМАНС-БРИФ: фича «приём файлов» (documents) — OilTech Digest, ветка feat/upload-docs

Источник — только находки шести разведчиков по api / jobs / db / ai / frontend / tests. Всё, чего они не нашли, помечено «не найдено».

Базовый факт, определяющий планирование: **на ветке feat/upload-docs кода фичи нет**. `git diff --stat origin/main...HEAD` пуст, grep по `documents|document_cards|companies|company_aliases` даёт 0 совпадений в `*.py`/`*.sql`, grep по `UploadFile|multipart|= File(|Form(` в `oiltech_digest/`, `web/`, `frontend/src/` — 0 (exit 1). Прототипа для доработки нет; всё строится с нуля поверх существующих швов.

---

## 1. Швы, на которых фичу тестировать

Сверху вниз. Первые четыре — существующие, с работающими образцами; пятый блок — новые, потому что прецедента нет.

### S1. HTTP-граница приложения (верхний шов)
**Что за шов.** `TestClient(api.app)` + подмена `app.dependency_overrides[api.require_user]` словарём пользователя, в `try/finally` с `.clear()`. Ни логина, ни куки, ни фикстуры юзера. Роль — просто ключ `"role"` в словаре; `require_admin` подменять не надо, он читает того же подменённого пользователя.
**Почему он.** Через него проверяется весь путь «загрузил → сущность создана → задача поставлена с правильными очередью/регионом/capability → ответ такой-то», плюс гейты доступа — без БД и без сети.
**Образцы.** `tests/test_api.py:954` `test_manual_article_import_endpoint_enqueues_ai_job` (проверяет тело ответа + **целиком** словарь `captured` с kwargs постановки задачи, `:1022`); `tests/test_api.py:1030` — путь без обработки: `assert "job" not in response.json()` и `assert enqueue_mock == []`; `tests/test_api.py:1566` `_as_user(role)` + пара «403 обычному» / `:1605 test_admin_still_reaches_maintenance_status` («симметрия: админа не заблокировали»); `tests/test_api.py:1687` — при отказе запрос вообще не дошёл до репозитория (`assert seen == {}`).

### S2. HTTP-граница внешнего контура (что уезжает в NL и что применяется обратно)
**Что за шов.** `/api/external-worker/claim | progress | complete | fail` с Bearer-токеном (хэш кладётся в `api.config.EXTERNAL_WORKER_TOKEN_HASH`) и `lease_token` в теле.
**Почему он.** Это ровно та граница, где содержимое документа гидратируется из БД и покидает РФ-контур (`api.py:1091` → `_external_worker_payload`), и та, где результат применяется в БД. Проверять состав payload надо здесь, а не внутри воркера.
**Образцы.** `tests/test_api.py:1148` (claim с Bearer), `:1199` (гидратация payload), `:1277` (применение результата по kind), `:1302` (**флаги читаются из `payload_json`, не из `payload`**), `:1371` (409 на мёртвом lease), `:1387` (политика ретраев).

### S3. Чистая функция стадии — `process_*_payload(payload, heartbeat=None)` без БД и без сети
**Что за шов.** Контракт внешнего контура: `build_*_payload` (в core, единственная с БД) / `process_*_payload` (в воркере, БЕЗ БД) / `apply_*_result` (в core, пишет). Средняя тестируется прогоном на `"offline": True` — детерминированный `OfflineAIClient`.
**Почему он.** Map-reduce по чанкам, heartbeat перед каждым чанком, прерывание батча по `LeaseLost`, накопление ошибок в `item["errors"]` — всё это проверяется без инфраструктуры.
**Образцы.** `tests/test_external_ai.py:4` (офлайн-прогон), `:57` (heartbeat по элементу), `:88` (вызовы repository в apply через monkeypatch), `:184` (прерывание при LeaseLost); `tests/test_recheck_translate.py` — полный скелет build → process → apply на паре стадий.

### S4. Живая БД в изолированной схеме — фикстура `isolated_db`
**Что за шов.** Единственная фикстура набора (`tests/conftest.py`, 48 строк): временная схема `test_<uuid4hex>`, `get_connection` подменён на connect с `SET search_path`, `init_db()`, в finally `DROP SCHEMA CASCADE`.
**Почему он.** Два требования фичи проверяемы только здесь: (а) «документы личные по умолчанию» и (б) «документы не видны ни одному запросу по articles». По тексту SQL (S7) второе доказать нельзя.
**Образцы.** `tests/test_api_integration.py:482` `test_background_jobs_api_hides_other_users_jobs_and_downloads` — **единственный в наборе тест пер-юзерной изоляции на настоящей схеме**: два реальных пользователя вставляются SQL-ом, чужой объект даёт 404 и на статус, и на скачивание. Подготовка данных — `tests/test_api_integration.py:11` (сырой SQL users → sources → tags → articles → …, с комментариями зачем каждая вставка) и `:16` («нужен настоящий пользователь, а не выдуманный id из dependency_overrides» — при FK на `users(id)` в overrides кладётся id реально вставленного юзера).

### S5. Очередь и lease на живой БД
**Что за шов.** `create_background_job(..., queue_name="external-ai", execution_region="external", capability="openai")` → `claim_external_background_job`.
**Почему он.** Новый kind обязан получить те же регрессы, что уже стоят у существующих: живой lease, потолок попыток, переочередь по протухшему lease.
**Образцы.** `tests/test_background_jobs.py:428` `_claimed_external_job` (хелпер), `:447` (живой lease), `:506` (потолок попыток), `:287`/`:553` (переочередь). Предупреждение: `:537` — известный флейк (см. ловушку 16).

### S6. Фейковый AI-клиент, записывающий порядок вызовов по `schema["name"]`
**Что за шов.** Класс с полной сигнатурой `complete_json(instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None)`, складывающий вызовы в список.
**Почему он.** Здесь проверяется и порядок стадий, и **содержимое входа модели** — то есть что чанк документа не срезан до 6000 знаков и что в промпт попали номера страниц.
**Образец.** `tests/test_processing.py:8` `_RecordingClient`, `:62` `assert [c["name"] for c in client.calls] == ["article_relevance"]`.

### S7. Перехват текста SQL (вспомогательный, не самостоятельный)
**Что за шов.** `FakeCursor`/`FakeConnection` ловят строку SQL, ассерты по подстрокам.
**Образец.** `tests/test_api.py:1619` `_articles_sql` («перехватить SQL, который list_articles реально отправляет в БД»), `:1653`.
**Оговорка.** Годится только как добавка к S4: `FakeCursor` при промахе по подстроке уходит в `else` (`tests/test_api.py:25`) и возвращает **строку статьи**. Запрос по документам, попавший в тот же фейк, молча получит статью, и тест на «документы отдельны» окажется зелёным по неверной причине.

### S8. Фронт — fetchMock, разветвлённый по URL
**Что за шов.** `render(<App/>)` → логин по плейсхолдерам → переход на экран → ввод → клик → `waitFor` на факт вызова нужного URL через `fetchMock.mock.calls`.
**Образец.** `frontend/src/app/App.test.tsx:266` (роутер мока), `:662` «imports article by direct url from admin sources page», `:679` (проверка ушедшего запроса). Запуск: `npm test` в `frontend/`.
**Оговорка.** Мок смотрит только строку URL, тело не разбирает; проверок «в multipart ушёл именно этот файл» в наборе нет — придётся конструировать `File` вручную и ассертить по `init.body`.

### S9. Новые швы — прецедента не найдено, проектировать как чистые функции
Ниже нет ни одного существующего теста-образца; это не пробел разведки, а факт кодовой базы.
- **Парсер файла → (текст, привязка к страницам).** Парсеров PDF/PPTX в проекте нет; `python-docx==1.2.0` есть, но используется **на вывод** (`tests/test_digest.py:285` — .doc-алиас это настоящий docx), не на чтение.
- **Чанкер текста.** Чанкера нет: `digest.py:953 _chunk_news_items` режет **список новостей по 3 для вёрстки страниц**, работает только на списке dict'ов. Токенизатора нет — `tiktoken` в requirements отсутствует, число токенов известно только постфактум из `usage` ответа (`openai_client.py:100`).
- **Верификатор «число ↔ текст страницы».** Механизма сверки утверждения модели с исходным текстом в конвейере **нет ни в одной стадии**. Вся пост-обработка вывода — это две вещи: проверка членства id тега в списке (`pipeline.py:557 _valid_tag_id`) и пересчёт итогового балла кодом (`pipeline.py:427 normalize_score_payload`). Копировать нечего.

Каждый из трёх обязан быть чистой функцией без БД и сети: выше по стеку их проверить нечем — тестов на границу 6000 знаков нет вообще (grep «6000» по `tests/` = 0 совпадений).

---

## 2. Точки подключения

### Бэкенд — HTTP
| Место | Что втыкается |
|---|---|
| `oiltech_digest/api.py:89–306` (блок Pydantic-моделей) | Тела запросов. Суффиксы: `...Request` — постановка задачи, `...Patch` — частичное обновление (все поля Optional), `...Create/...In` — POST/PUT. Модели ответа не описываются: `response_model` нигде не задан (кроме `response_model=None` на `:309`) |
| `oiltech_digest/api.py` — новые эндпоинты рядом с `api.py:696–738` | POST приёма файлов, GET списка, GET карточки, действие «опубликовать в общий корпус» отдельным сегментом пути (`/api/documents/{id}/publish` — по образцу `api.py:775 /api/sources/{id}/scrape`) |
| `oiltech_digest/api.py:326` / `:335` | `require_user` / `require_admin` — готовые гейты |
| `oiltech_digest/api.py:309–323` | Маршруты SPA. Прямая ссылка на `/documents` вернёт 404, пока путь не добавлен по образцу `@app.get("/tasks")`; catch-all нет |
| `oiltech_digest/api.py:1353–1366` `_external_worker_payload` | Ветка `if row["kind"] == "<kind>" and row["queue_name"] == "external-ai"` |
| `oiltech_digest/api.py:1146–1162` (внутри try между `begin_external_background_job_finalize` `:1144` и `finish_external_background_job` `:1166`) | Ветка применения результата |
| `oiltech_digest/api.py:975–1020` | Готовый контракт «поставил → опрашиваю `/api/jobs/{id}` → забираю файл». Отдельный polling для документов не нужен |

### Бэкенд — задачи и AI
| Место | Что втыкается |
|---|---|
| `oiltech_digest/background_jobs.py:278–283` `_HANDLERS` | Регистрация нового kind (сейчас ровно 4: digest_export, process_articles, scrape_source, diagnose_source). Либо — путь «мимо enqueue», как у recheck_relevance/translate_titles: `repository.create_background_job(...)` напрямую (`cli.py:378`, `cli.py:499`) |
| `oiltech_digest/network_policy.py:25` `route_ai_processing()` | Переиспользуется как есть — не привязана к статьям, отдаёт `("external-ai", "external", "openai")` при двух включённых флагах |
| `oiltech_digest/external_worker.py:135–170` `_handle_job` | `elif` по kind: `progress(20)` → `process_*_payload(heartbeat=lambda: _safe_heartbeat(client, job))` → `progress(90)` → `complete`. Иначе `raise ValueError("Unsupported external job kind")` (`:170`) |
| Новый модуль в `oiltech_digest/processing/` | Тройка `build_*_payload` / `process_*_payload` / `apply_*_result` |
| `oiltech_digest/processing/prompts.py` (конец файла, после `SCORE_SCHEMA:169`) | Константы инструкций и схем map- и reduce-стадий. Имена схем **обязаны** отличаться от `article_*` |
| `oiltech_digest/processing/openai_client.py:117–137` `OfflineAIClient.complete_json` | Ветки под новые имена схем; иначе `else: data = {}` (`:136`) и KeyError в офлайне и в тестах |
| `oiltech_digest/config.py:111–138` | `OPENAI_DOC_MODEL` / `OPENAI_DOC_REASONING` по образцу `OPENAI_SCORE_MODEL` (`config.py:137`); `config.py:151` `OPENAI_MODEL_PRICES` — ставки |
| `oiltech_digest/db/repository.py:2425–2443` `insert_ai_run` | Точка учёта расходов. Под `document_id` в сигнатуре и в таблице места нет (см. ловушку 1) |

### БД
| Место | Что втыкается |
|---|---|
| `oiltech_digest/db/schema.sql` — новая секция под шапкой `-- =====` **перед строкой 334** | `CREATE TABLE IF NOT EXISTS documents / document_cards / document_facts / document_tags / companies / company_aliases` + их `CREATE INDEX IF NOT EXISTS idx_<таблица>_<колонки>` |
| `oiltech_digest/db/schema.sql:334+` («Idempotent upgrades») | Только сюда — `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` к **существующим** таблицам и всё, что зависит от новых колонок |
| `oiltech_digest/db/repository.py` — новая секция в конце файла с шапкой-разделителем | Функции документов (образцы шапок: `:25 # sources`, `:224`, `:453`, `:1227`, `:1398`, `:1697`) |
| `oiltech_digest/db/repository.py:22–23` | `Literal`-словарь статусов документа + `get_args` — принятая в проекте замена CHECK-констрейнту |
| `oiltech_digest/readiness.py:10` `REQUIRED_TABLES` | Если таблицы документов считать обязательными. Регресса на состав списка нет: `tests/test_cli.py:9` мокает `schema_check` целиком |
| `tests/conftest.py:34–40` | Ещё одна строка `monkeypatch.setattr(<новый модуль>, "get_connection", connect)`, если модуль импортирует имя, а не модуль |

### Инфраструктура
- `requirements.txt` (12 строк): **нет `python-multipart`**, нет парсеров PDF/PPTX.
- `Caddyfile`, блок `(oiltech_routes)` строки 6–15: место для лимита размера тела. Сейчас там только `encode zstd gzip` и `reverse_proxy app:8000`.
- `docker-compose.yml:121–123` — образец общего тома (`exports:/app/exports`, `branding:/app/branding`) для оригиналов файлов. `docker-compose.external-worker.yml:1–12` — у внешнего воркера **секции volumes нет вовсе**.
- `docker-compose.yml:114` `BACKGROUND_JOB_QUEUES: default,ai`, `:140` `playwright`, `docker-compose.external-worker.yml:10` `external-ai,external-fetch,external-playwright` — если вводится новое имя очереди/capability, дописывать сюда.

### Фронт
| Место | Что втыкается |
|---|---|
| `frontend/src/app/App.tsx:27` | Литерал `"documents"` в union `ScreenId` (без него ничего не примет id, strict: true) |
| `frontend/src/app/App.tsx:57` `screens` | Объект `{id, label, eyebrow, title, description, status}` |
| `frontend/src/app/App.tsx:141` `navGroups` | Пункт сайдбара в одной из групп |
| `frontend/src/app/App.tsx:34` `ADMIN_SCREENS` | Решение по доступу к экрану (для личных документов — **не** добавлять) |
| `frontend/src/app/App.tsx:165` `URL_ADDRESSABLE` | Если нужна ссылка `?screen=documents` |
| `frontend/src/app/App.tsx:244–304` | Ветка монтирования с обязательными `onUnauthorized={resetSession} showToast={showToast}` |
| новая `frontend/src/features/documents/` | `DocumentsPage.tsx` (именованный экспорт), при росте — `DocumentCard.tsx`, `documentUtils.ts` + `documentUtils.test.ts` |
| новый `frontend/src/api/documents.ts` | Функции поверх `apiFetch`; для multipart `apiFetch` **не годится** (ловушка 17) |
| `frontend/src/api/types.ts` (после `MonthlyStats:480`) | Типы `Document`, `DocumentCard`, `DocumentFact` |
| `frontend/src/styles/globals.css` (конец, рядом с `.manualArticleGrid:2375`) | Классы формы/карточки + правило под `input[type="file"]`, которого сейчас нет |
| `frontend/src/app/App.test.tsx:266` | Ветки мока под `/api/documents*` + сценарий |
| `docs/testing.md:24–28` | Раздел «Что покрыто сейчас» ведётся руками |

---

## 3. Конвенции, обязательные к соблюдению

### A. HTTP-слой
1. Эндпоинты — декораторами прямо в `api.py`, без APIRouter и версий; путь `/api/<ресурс-во-множественном>`, слова через дефис, действие отдельным сегментом. `api.py:659`, `:775`, `:1022`.
2. Тело запроса — Pydantic-модель из шапки файла (`api.py:89–306`). Ответы собираются вручную, `response_model` не используется.
3. PATCH различает «не передано» и «передано None»: `model_dump(exclude_unset=True)` / `model_fields_set`. `api.py:742`, `:601`.
4. Query-параметры валидируются объектом `Query(...)` с `ge/le/pattern`. `api.py:475`, `:477`.
5. Форма ответа: мутация → `{"ok": True, ...}` (`api.py:648`, `:918`); список → **голый JSON-массив** без конверта (`api.py:841`); одиночный объект → dict через `_clean`.
6. Наружу ничего не уходит мимо `_clean` (`api.py:1429`): datetime/date → isoformat, Decimal → float, psycopg Json → `.obj`.
7. Ошибки — только `raise HTTPException` в хендлере; **глобальных `@app.exception_handler` нет ни одного** (grep = 0). Коды: 400 валидация/бизнес-правило, 404 нет объекта, 409 неверное состояние, 503 внешняя зависимость. `api.py:1007`, `:1009`, `:1289`.
8. `ValueError`/доменное исключение конвертируется в 400 на границе API. `api.py:701`, `:866`.
9. Валидация допустимых значений на границе API — **единственная** защита: CHECK-ов в БД нет, неизвестное значение молча исчезает из всех выборок. `api.py:84–88`.

### B. Пер-юзерная изоляция
10. Изоляция **не автоматическая**: ни middleware, ни RLS, ни общего Depends. `user_id` прокидывается руками в каждом хендлере. `api.py:538`, `:557`, `:938`, `:951`, `:970`.
11. Гейт обязан стоять на API, а не только во фронте — записано в коде как вывод аудита изоляции 24.07: `api.py:1201–1205`, `frontend/src/app/App.tsx:37`.
12. Правило выбора гейта: чтение/работа со своими данными — `require_user`; запись общих для всех настроек и данных — `require_admin` (`api.py:846`, `:917`, `:699`).
13. Фоновые задачи скоупятся по колонке `user_id`, но **админ намеренно видит все**: `user_id = None` для роли admin. `api.py:983`, `:1348–1351`, тест `tests/test_api.py:1094`.
14. Модель статей — «общая сущность + личное состояние» (`user_article_states`, составной PK, `ON DELETE CASCADE` на обе стороны, `schema.sql:254–264`). Для документов эта модель **не подходит по постановке**: документ личный целиком, а не только его статус.
15. Чтение личного состояния — `LEFT JOIN ... AND uas.user_id = %s` + `COALESCE(uas.status,'new')`; `user_id` — **первый** `%s` в params, потому что JOIN стоит выше WHERE. `api.py:536–537`.
16. Запись личного состояния — upsert по составному PK с `COALESCE(%s, <таблица>.<колонка>)`: None = «не трогать». `repository.py:373–382`.

### C. Фоновые задачи и внешний контур
17. `enqueue(kind, payload, *, user_id, queue_name, execution_region, capability, max_attempts=3)` возвращает **строку БД**, не id. `background_jobs.py:33–42`.
18. `enqueue` отвергает kind, которого нет в `_HANDLERS`, **до** записи в БД. `background_jobs.py:44–45`.
19. Обработчик: `(payload: dict, job_id: int) -> dict`; payload читается из колонки `payload_json`. `background_jobs.py:148–149`.
20. `network_policy` не решает по типу задачи — отдаёт `ExecutionDecision(queue_name, execution_region, capability, reason)`, вызывающий передаёт все три поля в enqueue. `network_policy.py:11–17`, `:26–28`.
21. Payload для воркера строится **лениво, в момент claim**, ключ — пара `(kind, queue_name)`. `api.py:1091`, `:1356–1363`.
22. Воркер не имеет доступа к БД и не должен его иметь; `process_*_payload` обязана валидировать полученный payload и падать явно, если справочники не приехали. `external_ai.py:67–70`.
23. Результат — самоописывающийся конверт: маркерный булев флаг + `kind` + `stats` + поэлементный список; ошибка одного элемента не валит батч (`item["errors"]`). `external_ai.py:72–78`, `:157–159`.
24. Heartbeat зовётся **перед каждой единицей работы** из тела обработки; `LeaseLost` — единственное исключение, прерывающее батч, прочие сбои heartbeat глотаются. `external_ai.py:79–88`.
25. 409 на heartbeat = «результат не примут»: воркер бросает `LeaseLost` и не шлёт ни complete, ни fail. `external_worker.py:127–129`, `:172–176`.
26. Применение результата делает **core**, не воркер; ветка apply стоит строго внутри try между `begin_external_background_job_finalize` и `finish_external_background_job`, падение apply откатывает `finalizing→running`. `api.py:1144–1145`, `:1163–1166`.
27. Стоимость пишется постадийно в `ai_processing_runs` при apply, по строке на `(job_id, article_id, stage)`, с `ON CONFLICT DO NOTHING`. `external_ai.py:410–425`, `repository.py:2439`.
28. Прогон пишется независимо от вердикта — вызов оплачен, даже если элемент отклонён. `external_ai.py:240–247`.

### D. AI-стадии
29. Полный список стадий — ровно пять (`relevance, summary, translation, tagging, scoring`), все — вызовы `client.complete_json` в `pipeline.py:311,324,337,377,393`. Шестой нет.
30. Форма стадии: чистая функция-вызов возвращает `AIResponse` и **не трогает БД**; батч-функция рядом пишет в repository и логирует прогон. `pipeline.py:310` против `pipeline.py:37`.
31. Единственная точка входа в модель — `complete_json(instructions, user_input, schema, max_output_tokens=900, model=None, reasoning_effort=None)`. `openai_client.py:50–52`.
32. Вход модели — плоский текст «ключ: значение» построчно, **не JSON**; справочники дописываются хвостом. `pipeline.py:484–493`, `:395`.
33. Выход — строгая JSON Schema: `strict: True`, `additionalProperties: False`, все свойства в `required`. Имя схемы — идентификатор стадии и ключ диспетчеризации офлайн-клиента. `openai_client.py:63–71`, `prompts.py:119`.
34. Промпты и схемы — UPPER_CASE-константы в `prompts.py`; `pipeline.py` только импортирует. `prompts.py:6`, `:118`, `pipeline.py:14–25`.
35. Дорогая/качество-критичная стадия получает свою модель и свой reasoning через env-оверрайд с откатом на `OPENAI_MODEL`. `pipeline.py:398–399`, `config.py:137`.
36. Токены **никогда** не считаются локально — берутся из `usage` ответа; стоимость — свойство `AIResponse.cost_usd`. `openai_client.py:96–101`, `:35–40`.
37. Выход модели **никогда** не пишется в БД сырым: код валидирует/пересчитывает. `pipeline.py:557–562`, `:427`, `:289`.
38. Офлайн-клиент — обязательная часть контракта стадии. `openai_client.py:117–137`.

### E. БД и схема
39. Инструмента миграций нет. Миграция = идемпотентный `schema.sql`, целиком прогоняемый `init_db()` **одним `conn.execute` в одной транзакции**. `connection.py:19–25`.
40. Схема применяется сама на каждом деплое одноразовым контейнером bootstrap; `app` стартует только после его успеха. `docker-compose.yml:37`, `:42–44`.
41. PK везде `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` (`schema.sql:64`); чисто связочные пер-юзерные таблицы — составной PK без суррогата (`schema.sql:262`).
42. Имена: таблицы snake_case во мн. числе; FK-колонка `<сущность>_id`; JSONB с суффиксом `_json`; время TIMESTAMPTZ с `DEFAULT now()`. `schema.sql:99`, `:312`, `:111–112`.
43. Индексы — `idx_<таблица>_<колонки>`, уникальный-дубль с суффиксом `_unique`, все `IF NOT EXISTS`. Все 34 индекса схемы следуют шаблону. `schema.sql:114–115`.
44. В схеме **нет** CHECK, ENUM и триггеров (grep = 0/0/0). Словарь статусов — TEXT + комментарий + `Literal` в Python как источник правды. `repository.py:22–23`, `schema.sql:259`.
45. `updated_at` не обновляется автоматически — каждая UPDATE проставляет `updated_at = now()` руками. `repository.py:104`.
46. Функции репозитория — модульные, с русским docstring, открывают своё соединение `with get_connection() as conn` и явно зовут `conn.commit()` (60 вызовов на файл). Транзакции на несколько функций никто не держит. `repository.py:101–107`.
47. Выборки — `cur = conn.cursor(row_factory=dict_row)` → `list[dict]`; скаляры — `conn.execute(...).fetchone()[0]`. `repository.py:70–74`, `:365–366`.
48. JSONB пишется только через `Json(_jsonable(...))`. `repository.py:485`, `:1211–1224`.
49. Уникальность с «общей» NULL-owner строкой делается **парой** частичных индексов: `UNIQUE (user_id, month)` + `UNIQUE (month) WHERE user_id IS NULL`. `schema.sql:205–206`.
50. Расширений Postgres схема не подключает: `CREATE EXTENSION` и `vector` в schema.sql — 0 совпадений. pgvector/pg_trgm нет.

### F. Фронт
51. Роутера нет: `useState<ScreenId>` + необязательный `?screen=`. react-router не установлен. `App.tsx:174`, `package.json:12`.
52. Экран = папка `features/<feature>/` с `<Feature>Page.tsx`, **именованный** экспорт, без index.ts; чистая логика — в `<feature>Utils.ts`, и только у неё есть юнит-тест.
53. Контракт пропсов страницы: `onUnauthorized: () => void` и `showToast: ToastWriter`; тип `ToastWriter` объявляется локально в каждом файле заново. `SourcesPage.tsx:17`.
54. Ошибки: локальная `handleError(error, fallback)` в каждой странице — 401 → `onUnauthorized()`, иначе `showToast(msg, "error")`. Общего хелпера нет. `SourcesPage.tsx:136`.
55. API-слой: тонкий модуль на домен, каждая функция — одна строка `apiFetch<T>(path, init)`, типы — в общем `api/types.ts`. `api/sources.ts:62`.
56. Единственная точка сети — `apiFetch/apiFetchText/apiDownload`, `credentials: "same-origin"`, кука, ошибка — `ApiError` со `status`. `api/client.ts:16`.
57. Стили — один глобальный `globals.css` и строковые className; каркас: `section.screenStack > header.screenHeader > section.panel > div.panelHeader`. `SourcesPage.tsx:341`.
58. Форма: `<label className="field"><span>Подпись</span><input/></label>` в грид-сетке, кнопка `.primaryButton`/`.ghostButton`. `SourcesPage.tsx:407`.
59. Весь UI-текст русский, зашит в JSX; технические слова бэкенда наружу не выносятся («Собираем статьи…», не job id). `SourcesPage.tsx:81`.
60. Роль знает только `App` (из `/api/auth/me`), раздаёт пропсом; контекста/провайдера нет. `App.tsx:185`, `:262`.

### G. Тесты
61. Плоский `tests/`, файлы `test_*.py`, функции `def test_*`, ни одного класса-контейнера, ни одного `async def test_`.
62. **Конфига pytest нет вообще** (ни pytest.ini, ни pyproject, ни setup.cfg, ни tox.ini). Ноль `@pytest.mark`, ноль `pytest.skip`, ноль `importorskip`.
63. `unittest.mock` не используется нигде — только `monkeypatch.setattr` на атрибут модуля. `tests/test_api.py:1003`, `:976`.
64. Аргументы замоканной функции собираются в `captured` и сверяются **целиком одним assert**. `tests/test_api.py:1022`.
65. Данные интеграционных тестов пишутся сырым SQL с RETURNING id и явным commit; фабрик и фикстур данных нет. `tests/test_api_integration.py:18`.
66. OpenAI не вызывается никогда — тремя приёмами: подмена `make_client`, флаг `"offline": True`, `OfflineAIClient`. `tests/test_processing.py:58`, `tests/test_external_ai.py:6`, `openai_client.py:105`.
67. Каждый гейт прав закрывается **парой** тестов: «обычному 403» + «админ по-прежнему проходит». `tests/test_api.py:1605`.
68. Тест-регресс несёт докстринг с датой/номером инцидента, ценой ошибки и объяснением, почему прежние тесты баг не ловили. `tests/test_api.py:1305`.
69. Файловые артефакты — только `tmp_path` + подмена каталога в модуле; в репозиторий тесты не пишут. `tests/test_digest.py:224`.
70. Команды: весь набор — `PYTHONPATH=. python -m pytest` (`README.md:182`) или `docker compose run --rm test` (`README.md:183`, сервис под профилем `test`, `docker-compose.yml:224`). Текущее состояние: **251 passed** за ~9с при живом Postgres.

---

## 4. Ловушки — по убыванию опасности

**1. [ДЕНЬГИ, P0] Учёт стоимости прибит к статьям на уровне схемы.** `ai_processing_runs.article_id` — FK на `articles(id)` (`schema.sql:283`), дедуп биллинга держится на `UNIQUE (job_id, article_id, stage)` (`schema.sql:398`). Расход на документ туда не положить: id документа FK не пустит, а через `article_id = NULL` — можно, но **дедуп перестаёт работать (NULL-ы в UNIQUE различны, `schema.sql:395–396`)**, и повторное применение результата удваивает счёт. Это ровно баг H1/T2, который для статей уже чинили. Обе функции записи прогона требуют статью: `pipeline.py:575–580` тянет поля из article-дикта, `external_ai.py:410` принимает `article_id` первым позиционным.

**2. [ДЕНЬГИ, P0] Защита от повторной оплаты OpenAI действует только для `kind='process_articles'`.** `repository.py:1108–1115`: `... AND kind = 'process_articles' AND ai_started_at IS NOT NULL`. Новый локальный AI-kind по умолчанию попадёт в общую переочередь (`repository.py:1118–1125`) и оплатит модель второй раз — инцидент 24.07 (задача 1181, 6 кругов при `max_attempts=3`). Флаг `ai_started_at` ставится ровно в одном месте кода: `background_jobs.py:210`.

**3. [ДЕНЬГИ, P0] Забытый маркерный флаг в результате = деньги без следов.** Ветки apply срабатывают при совпадении **и** kind, **и** флага (`api.py:1147`, `:1149`, `:1161`). Без флага задача честно становится `ok`, результат лежит в `result_json`, но в БД не записано ничего и в `ai_processing_runs` не попало ни цента.

**4. [ДЕНЬГИ] Падение на N-м чанке переплачивает за все предыдущие.** Любое исключение обработчика ловится общим except и уводит задачу в ретрай **с нуля**, частичного сохранения нет (`background_jobs.py:156–158`). Ретраев и бэкоффа в самом AI-клиенте тоже нет: один `requests.post` с таймаутом 60с, любая 4xx/5xx — сразу `AIClientError` (`openai_client.py:77–87`). На map-reduce из 34 чанков это 34 независимых шанса уронить разбор.

**5. [ДЕНЬГИ] Продолжение работы после 409 heartbeat.** Инцидент 24.07: ~80 минут петли heartbeat 409 → OpenAI 200 → 409 со скоростью ~$11/час, и это **не попало даже в `ai_processing_runs`**, потому что complete тоже отвергался. Docstring `LeaseLost` (`external_ai.py:44–53`) описывает инцидент дословно.

**6. [ДЕНЬГИ] Модель молча откатится на слабую и дешёвую, а цена посчитается неверно.** Пер-стадийные модели читаются из окружения того процесса, который реально зовёт OpenAI, — это внешний воркер в Амстердаме, и его `.env` **не в git** (`config.py:120–121`, прецедент 2026-06). Незаписанный там `OPENAI_DOC_MODEL` откатится на `OPENAI_MODEL` (дефолт `gpt-5-nano`, `config.py:112`). Стоимость считается матчингом префикса по таблице из 4 записей (`config.py:151–157`); модель вне таблицы падает на nano-ставку 0.05/0.40 и занижает счёт (для gpt-5.5 — ~100×).

**7. [ДЕНЬГИ, тесты] Глобального предохранителя от реального платного вызова OpenAI в тестах НЕТ.** `config` при импорте подхватывает `.env` из корня (`config.py:12`), где лежит боевой ключ; клиент бракует только пустой ключ (`openai_client.py:53`). Защищает исключительно ручной monkeypatch в каждом тесте. Для map-reduce по чанкам цена такой дыры кратно выше, чем для одной статьи.

**8. [УТЕЧКА / КЛАСС ДАННЫХ, P0] `payload_json` отдаётся клиенту целиком, а админ читает любую чужую задачу.** `api.py:1338` `"payload": _clean(row.get("payload_json") or {})` внутри `_job_payload`, отдаваемого из `/api/jobs` и `/api/jobs/{id}`; `api.py:1349–1350` — для админа без `user_id`. Положить текст личного документа (или его чанки) в `payload_json` = утечка из «личного по умолчанию» режима без единой строки нового кода. В payload класть **только ссылки/id**.

**9. [КЛАСС ДАННЫХ, P0] Граница РФ→NL проходит ровно в `_external_worker_payload` и ничем не обвязана.** `api.py:1353–1366` гидратирует содержимое из БД и отдаёт наружу; для статей это уже так (`external_ai.py:33–41` кладёт полные строки статей). Для внутренних документов ГПН это тот же шов, и **проверки класса данных на нём нет**. Смежное: внешний воркер физически не видит файлов — у контейнера **нет ни одного volume** (`docker-compose.external-worker.yml:1–12`), значит наружу может уйти только то, что вошло в JSON ответа `/claim`.

**10. [ИЗОЛЯЦИЯ, P0] Копирование PATCH-образца статьи теряет проверку владельца.** `api.py:638–646` проверяет существование в `articles` и **не проверяет владельца** — потому что статьи глобальны, а личное лежит в `user_article_states`. Для документа нужны и `exists`, и owner-фильтр в одном запросе. Механизма, который поймает такую ошибку автоматически, нет: ни middleware, ни RLS, ни CHECK — только дисциплина в каждом отдельном SQL (`repository.py:2683–2689`; там же — прецедент функции, которая **принимала** `user_id` и нигде его не использовала, создавая ложное ощущение фильтрации).

**11. [ИЗОЛЯЦИЯ] Публикация в общий корпус через NULL-владельца воспроизведёт баг «чужое побеждает личное».** `ORDER BY <выражение> DESC` в Postgres по умолчанию NULLS FIRST, поэтому строка с `user_id IS NULL` вставала **перед** личной и `LIMIT 1` отдавал её (проверено на проде) — `repository.py:2648–2656`, исправлено на `ORDER BY (user_id IS NOT NULL) DESC, updated_at DESC`.

**12. [ARTICLES, тихо] `/api/health` без авторизации считает `SELECT COUNT(*) FROM articles`.** `api.py:444–448` — сигнатура без Depends. Любой счётчик по документам, добавленный туда, станет публичным. Обратная сторона: «прятать» документы почти не от чего — единственный запрос по `articles` строится в `GET /api/articles` жёстким списком JOIN (`api.py:544–563`), отдельная таблица в него не попадёт сама.

**13. [ARTICLES, тихо] Формат результата воркера прибит к статьям.** `apply_process_result` обходит `result["articles"]` и делает `int(item["article_id"])` **без защиты** (`external_ai.py:345–346`; та же завязка в `apply_recheck_result:234`, `apply_translate_result:326`). Документ, положенный в тот же список, либо упадёт по KeyError, либо — что хуже — его id уйдёт в repository-функции статей.

**14. [ARTICLES, тихо] Слепое копирование `article_tags` обрежет фичу.** `idx_article_tags_article_unique` по `article_id` (`schema.sql:189`) означает: у статьи ровно **один** тег, а upsert `ON CONFLICT (article_id) DO UPDATE SET tag_id = EXCLUDED.tag_id` (`repository.py:2212–2228`) перетирает предыдущий, а не добавляет. У документа сущностей много.

**15. [ARTICLES, тесты] `FakeCursor` отдаёт строку СТАТЬИ по промаху подстроки.** `tests/test_api.py:13` — одна ветка на `article_score_items`, `:25` `else:` → строка с title "Directional drilling automation". Новый запрос по документам, попавший в тот же `FakeConnection`, молча получит статью, и тест на главное требование фичи будет зелёным по неверной причине. Требование «документы не видны запросам по articles» проверять **только на живой БД** (S4).

**16. [ПРОД НЕ ПОДНИМЕТСЯ] `python-multipart` не установлен, и FastAPI падает не на запросе, а на **определении маршрута**.** Проверено запуском: `.venv/bin/python -c "... UploadFile = File(...)"` → `RuntimeError: Form data requires "python-multipart" to be installed` (fastapi 0.125.0, python_multipart installed: False); `pip show python-multipart` → not found. Это падение при импорте `oiltech_digest.api` — контейнер `app` не встанет вообще.

**17. [ПРОД НЕ ПОДНИМЕТСЯ] Индекс/констрейнт/UPDATE по новой колонке в CREATE-секции роняет init-db на проде.** На живой базе `CREATE TABLE IF NOT EXISTS` — no-op, колонки ещё нет → `column "user_id" does not exist`. Записано прямо в схеме как предупреждение: `schema.sql:329–332`, тот же инцидент в `docs/handoff_plan_2026-07-01.md:93`. Усиление: весь `schema.sql` — **одна транзакция**, падение любой строки откатывает весь файл, bootstrap падает, `app` по `condition: service_completed_successfully` не поднимается. Следствие: `CREATE INDEX CONCURRENTLY` в этот файл положить нельзя.

**18. [ПАМЯТЬ / РАЗМЕР, P0] Лимита на размер тела запроса нет НИГДЕ.** `grep -rniE "request_body|max_size|max_request|body_size|client_max"` по `Caddyfile`, `docker-compose.yml`, `oiltech_digest/` → 0 совпадений; uvicorn запускается голой командой (`Dockerfile:41`); единственный middleware в api.py — логгер (`api.py:65`). Ограничение на размер PDF/PPTX вводит сама фича, унаследовать нечего. Контекст сервера: прод РФ-core — 1.9G RAM.

**19. [ПАМЯТЬ / ТАЙМАУТ] Гидрированный payload обязан уложиться в 30-секундный HTTP-таймаут claim.** `external_worker.py:68–77` (`timeout=30`), complete — 60с (`:98–103`). Текст 60-страничного документа пойдёт целиком одним JSON-телом через канал РФ→NL. Плюс: payload **пересобирается при каждом claim**, а не сохраняется при постановке (`api.py:1091`) — после переочереди воркер получит свежую гидрацию, и любые изменения строки `documents` между попытками молча меняют вход модели.

**20. [ПАМЯТЬ / ОБРЕЗКА] Переиспользование `_article_prompt` молча срежет документ до 6000 знаков.** `pipeline.py:492` и `:507` — `_compact(article.get('raw_text') or '', 6000)`, `_compact` — просто `text[:limit]` (`pipeline.py:552–554`). Это **единственная** защита от переполнения входа во всём конвейере; тестов на эту границу нет (grep «6000» по `tests/` = 0). Симметрично на выходе: исчерпание `max_output_tokens` не даёт частичного ответа — `_extract_output_text` бросает исключение (`openai_client.py:155–167`); дефолт 900, максимум среди текущих стадий 1800 (`pipeline.py:397`). И `"verbosity": "low"` зашита в payload для **всех** стадий без параметра (`openai_client.py:63–64`).

**21. [ТИХАЯ ПУСТАЯ РАБОТА] Забытая ветка в `_external_worker_payload` не даёт ошибки.** Сработает финальный `return _clean(payload)` (`api.py:1363`), и воркер получит валидный JSON без текста документа — только id и флаги — и сделает пустую работу.

**22. [ЗАДАЧА УМИРАЕТ] Смертельная комбинация «нет локального обработчика + выключен внешний контур».** `route_ai_processing` вернёт `("ai", "ru")` (`network_policy.py:28`), локальный воркер подписан на `default,ai` (`docker-compose.yml:114`) и **не смотрит `execution_region`** при claim (`repository.py:516–518` против `:642–645`), заберёт задачу и терминально завалит её как неизвестный kind (`background_jobs.py:140–142`). То есть выключение `EXTERNAL_WORKERS_ENABLED` не откладывает задачи, а убивает их. Зеркальная ловушка: имя очереди — свободный текст без валидации (`repository.py:462`), задача в очереди без подписчиков висит в `queued` вечно, без единой ошибки в логах.

**23. [ПОТЕРЯ ДАННЫХ, прецедент] Чтение ключа, которого в строке нет.** `get_background_job` делает `SELECT *`, поэтому ключи — это **колонки** (`payload_json`/`result_json`), а не `payload`/`result`. Чтение `job.get("payload")` молча даёт `{}` и все булевы флаги становятся False. Так физически потеряли ~2000 статей (баг T3): `api.py:1150–1155`, тест-регресс `tests/test_api.py:1305`, тот же урок для `result_json` — `cli.py:446–449`.

**24. [ИЗОЛЯЦИЯ] Не передал `user_id` в enqueue — владелец-не-админ никогда не увидит свою задачу.** Скоуп читает колонку, а не payload (`api.py:1351`, `repository.py:499–501`). Для «личных по умолчанию» документов это ломает весь экран статуса загрузки.

**25. [БЕЗОПАСНОСТЬ ФАЙЛОВ] `GET /api/jobs/{id}/download` отдаёт `FileResponse` по пути из `result_json` без containment-проверки** — проверяется только существование файла (`api.py:1011–1020`, `background_jobs.py:272–275`). Копировать этот паттерн для выдачи оригинала документа = копировать и отсутствие проверки.

**26. [ТЕСТЫ ЗЕЛЕНЫ ПО НЕВЕРНОЙ ПРИЧИНЕ] Изоляция тестов держится на `search_path` подключения.** Любой код, открывающий соединение мимо пропатченного `get_connection` (прямой `psycopg.connect`, импорт **имени** `get_connection`, свой пул), уйдёт в боевую public-схему. Пропатчены только 4 модуля (`tests/conftest.py:34–40`); `readiness.py:8` и `ingestion/manual_import.py:11` импортируют напрямую и **не** пропатчены. Смежное: `init_db()` создаёт таблицы в изолированной схеме, но `list_tables()`/`schema_check()` жёстко фильтруют `table_schema = 'public'` (`connection.py:33`, `readiness.py:29`) — тест «таблицы документов есть» через их возврат увидит боевую схему.

**27. [ТЕСТЫ] `BACKGROUND_JOB_INLINE` по умолчанию ВКЛЮЧЁН** (`config.py:37`) — незамоканный `enqueue` реально выполнит хендлер в фоновом потоке во время теста (`background_jobs.py:55`). Гасить явно: `tests/test_background_jobs.py:150`.

**28. [ТЕСТЫ] 39 из 251 тестов требуют живого Postgres и при его отсутствии ПАДАЮТ ОШИБКОЙ, а не скипаются.** Замер: `212 passed, 39 errors` (test_background_jobs 21, test_api_integration 9, test_repository_integration 6, test_source_overrides 2, test_benchmarks 1). Фикстура коннектится без try/except и без skip (`tests/conftest.py:30`).

**29. [ТЕСТЫ] Известный флейк.** `test_lost_external_job_dies_after_max_attempts_instead_of_looping` (`tests/test_background_jobs.py:537`) упал 1 раз из 4 полных прогонов (`assert 0 == 1`); завязан на настенное время и срок lease, причина не установлена. При добавлении своего kind в ту же таблицу прогонять набор несколько раз — одиночный зелёный прогон не доказательство.

**30. [ТЕСТЫ] «Ближайший образец» покрывает не то, что кажется.** Коммит 4733ee9 добавил 196 строк `ingestion/manual_import.py` и 112 строк тестов — **все в test_api.py, где `import_manual_article` замокан целиком** (`tests/test_api.py:957`), и ноль новых файлов в `tests/`. То есть разбор/скачивание/дедуп ручного импорта не покрыты ничем. Копировать образец, не заметив этого, — воспроизвести ту же дыру для парсинга документов.

**31. [ФРОНТ] `apiFetch` сам ставит `Content-Type: application/json`, если в init есть body.** `api/client.ts:12`. Прогнать через него `FormData` = отправить multipart **без boundary**; сервер тело не разберёт. Плюс `apiFetch` безусловно делает `response.json()` (`:35`) — ответ на загрузку обязан быть непустым JSON. Плюс текст ошибки = сырое тело: на 413/415 пользователь увидит `{"detail":"..."}` в тосте (`:26` → `SourcesPage.tsx:143`).

**32. [ФРОНТ] Модель «один busy + один тост» не тянет пакетную загрузку.** Оверлей рисуется одновременно во всех панелях экрана (`SourcesPage.tsx:351,382,425`); тост один на приложение, перетирается, гаснет через 2800мс (`App.tsx:181`, `:192`). Для N файлов нужен словарь по id — образец `PendingJobMap` (`SourcesPage.tsx:25`).

**33. [ФРОНТ] Error boundary отсутствует во всём фронте** (grep `ErrorBoundary|componentDidCatch` = 0). Исключение в рендере карточки документа (факт без страницы, обращение к undefined) гасит всё приложение в белый экран.

**34. [ФРОНТ] Типы на прод уезжают непроверенными.** Docker собирает `npm exec vite build` **без `tsc`** (`Dockerfile:13`, тех-долг T12 в `docs/architecture.md:283`), CI в репозитории нет (`.github` отсутствует) — ни vitest, ни tsc автоматически никем не гоняются. Смежное: тест на main уже красный — `?screen=sources` не в `URL_ADDRESSABLE` (`App.tsx:165`), прогон `1 failed | 13 passed`.

**35. [ФРОНТ, мелкое] Новый экран автоматически появится в нижнем меню телефона** — оно рендерит весь массив `screens` без отдельного списка (`App.tsx:555`). Стиля под `input[type="file"]` нет, зато `.field input` навязывает ему высоту текстового поля (`globals.css:433`); drag-and-drop зон в стилях нет ни одной.

---

## 5. Открытые технические вопросы — спека обязана ответить

Кода-ответа нет ни на один; все помечены источником «не найдено».

**Извлечение и страницы**
1. Чем парсить PDF/PPTX/DOCX — **не найдено ни одной библиотеки-парсера** в requirements (python-docx есть, но на вывод). Выбор, лицензия, вес в образе, поведение на защищённых/сканированных PDF.
2. Откуда берётся **номер страницы** у каждого факта — как парсер отдаёт привязку «текст → страница», и что считается страницей у PPTX (слайд) и DOCX (страниц в исходнике нет). Контракта не найдено.
3. Что считать «подтверждением числа» в верификаторе: точное совпадение подстроки? нормализация разрядов/пробелов/запятых? единиц? Прецедента сверки вывода модели с текстом в конвейере нет вообще. И: верификация — код или отдельный AI-вызов (у второго варианта своя цена).
4. Что делать с неподтверждённым фактом: пометить и показать, скрыть, или завалить карточку. Не найдено.

**Map-reduce и деньги**
5. Как нарезать: одна задача на документ с внутренним map-reduce vs задача-на-чанк (прецедент пакетной постановки — `cli.py:375`, нарезка id на чанки). Первый вариант упирается в 30-секундный claim и в «падение на 21-м чанке из 34 = переплата», второй — в сборку reduce между задачами.
6. Сохраняются ли map-результаты чанков в БД, чтобы ретрай не переплачивал. Прецедента частичного сохранения нет.
7. Куда писать `ai_processing_runs` для документа: новая колонка `document_id` + новый уникальный индекс, отдельная таблица, или `article_id = NULL` без дедупа. Схема сейчас не поддерживает ни один вариант из коробки.
8. Какая модель и какой reasoning (`OPENAI_DOC_MODEL`), её ставки в `OPENAI_MODEL_PRICES`, и кто прописывает её в `.env` внешнего воркера (файл не в git).
9. Ожидаемая стоимость разбора одного документа — оценки не найдено.

**Данные и изоляция**
10. **Разрешено ли вообще** отправлять внутренние документы на OpenAI через воркер в Амстердаме. Кода-проверки класса данных на этой границе нет; решение внешнее по отношению к коду.
11. «Публикация в общий корпус»: гейт `require_admin` или `require_user`? Прецедент `api.py:1201–1205` говорит, что действие, затрагивающее всех, ставится под админа. И модель хранения: NULL-owner строка + пара частичных индексов (как `monthly_digests`, `schema.sql:205–206`) vs флаг `is_public` — код даёт прецедент только первого и вместе с ним баг NULLS FIRST.
12. Удаление документа: `ON DELETE CASCADE` в схеме или ручной каскад как `delete_article` (`repository.py:1741–1772`). У articles FK в основном без CASCADE; отступление надо назвать явно.
13. Дедупликация загрузок (тот же файл дважды) — правил не найдено; у статей есть `content_hash`/уникальный url, у документов аналога нет.
14. Квоты на пользователя (число файлов, суммарный объём) — не найдено.
15. Retention оригиналов на томе и бэкап тома — правил не найдено.
16. Входят ли таблицы документов в `readiness.REQUIRED_TABLES` (`readiness.py:10`, сейчас 9 таблиц).

**Оценка и сущности**
17. Шкала балла документа: критерии откуда — переиспользуются общие `scoring_criteria` (сейчас они целиком уезжают в payload статей, `external_ai.py:38–40`) или свои? Итог считает код, как `normalize_score_payload` (`pipeline.py:427`)? Контракта нет.
18. `companies` / `company_aliases`: правила нормализации названий, слияния алиасов, разрешения конфликтов — прецедента сопоставления сущностей в проекте не найдено.
19. Применима ли к документам стадия перевода (`translation` есть для статей) и на каком языке карточка.

**Границы и инфраструктура**
20. Лимит размера файла, число файлов в пакете, где именно ставится ограничение (Caddy `(oiltech_routes)` 6–15 / FastAPI / приложение) и какой код возвращается (413/400).
21. Имя очереди и capability: переиспользовать `external-ai`/`openai` или заводить свои (тогда правка обоих docker-compose).
22. Путь на общем томе для оригиналов + монтирование в `docker-compose.yml` (образец `exports`/`branding`, `:121–123`) и как отдаётся оригинал наружу с containment-проверкой (в существующем `download` её нет).
23. UX пакетной загрузки: как показывать статус N файлов при одном busy и одном перетираемом тосте; нужен ли адресуемый URL карточки (роутера нет — карточка может быть только состоянием внутри страницы, образец `ArticlesPage.tsx:44`).
24. Нужен ли документам локальный путь обработки (двухголовая схема как у `scrape_source`: `ingestion/external_fetch.py` + `background_jobs.py:242`) или только внешний — от этого зависит, регистрировать ли kind в `_HANDLERS` и вносить ли его в guard `repository.py:1112`.