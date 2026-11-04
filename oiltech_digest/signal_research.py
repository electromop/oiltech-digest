"""«Режим ChatGPT» радара: находки темы — одним вызовом модели со встроенным поиском OpenAI.

Замечание заказчика 29.09: «ChatGPT по запросу за сентябрь выдал топ-базу из 20 штук», а
радар на поиске Brave и дешёвом судье — слабее. Разница — в сильной модели, которая сама
делает десятки поисковых запросов, отбрасывает SEO-мусор и сравнивает события между собой.
Здесь ровно это: на тему один вызов Responses API с инструментом web_search и строгим JSON.

Модель только находит. Проверяет наш код, как у любой находки радара: страница докачивается
(живая ли ссылка и о том ли она), дальше — наш судья (категория, дата события, смешанные
события, тема, баллы профиля), ревью пачки, дедуп и запись на ядре. Модуль без базы — идёт
на NL-воркере вместе с run_discovery.

При «Отборе» (#131, finds_only) поиск только находит и по смыслу не отсеивает ничего: ни «не технологию» по своей
оценке, ни событие без названной технологии, ни дату вне периода, и потолка числа находок темы нет — решает «Отбор».
Остаются технические проверки (verify_research_evidence; разделы «Поиск → …» — rejected_sections.SEARCH_SECTIONS); тип
события, компания, суть и «почему важно» модели поиска — черновые данные находки, во вход агентов они не идут.

С #138 при «Отборе» поиск ещё и широкий: все материалы нефтесервиса и нефтегаза за период (технологии, контракты,
сделки, программы бурения, регулирование, рынок) — RESEARCH_INSTRUCTIONS_BROAD. Ссылка, которая уже есть в статьях
сбора, новой находкой не становится (это решает прогон радара), а сайты, которые парсер собирает целиком, поиск
получает как «уже есть, ищи дальше» (covered_sites_block). Модель и уровень — из задачи прогона, решение ядра.
"""

from __future__ import annotations

from datetime import date, timedelta
import re
import time
from typing import Any, Callable

import requests

from oiltech_digest import config as app_config
from oiltech_digest.processing.openai_client import AIClientError, output_budget
from oiltech_digest.processing.pipeline import make_client

EVIDENCE_SOURCE = "openai_web_search"
# Технические причины отсева поиска — по ним разделы «Поиск → …» (rejected_sections, #130, #131). Ссылку не подтвердили
# ни страница, ни источники поиска; страница не открылась, хотя адрес из найденного, — во вход «Отбора» нечего дать:
# текст у находки только от модели поиска, а это черновые данные; страница-список (лента новостей, оглавление номера,
# страница пресс-релизов или портфеля) — не одна публикация (черновая проверка разбора разметки 23.10: заголовок
# короче пяти слов ловит 7 из 8 таких страниц без лишних).
NO_LINK_OR_TITLE = "нет ссылки или заголовка"
UNCONFIRMED_LINK = "страница не открылась и адрес не из найденного"
PAGE_NOT_FETCHED = "страница не открылась, текста издателя нет"
LIST_PAGE = "заголовок короче пяти слов: похоже на ленту, оглавление или раздел сайта, а не на одну публикацию"
LIST_PAGE_TITLE_WORDS = 5
# Тип события от модели поиска. not_technology — поиск сам признал событие не технологическим
# (премия, контракт без технологии, сделка): отбрасывается до судьи, чтобы бизнес-новости не
# тратили вызовы судьи (прогон 01.10: 29 бизнес-карточек из 62). other — технологическое событие
# вне перечня, его решает судья. До 17.10 other значил и то и другое, и вместе с бизнесом
# отсекались квалификации (AiP, NTQ, JIP), испытательные центры и рубежи применения: у всех 28
# карточек «нет технологии» на проде технология была названа.
TECH_EVENT_KINDS = ("deployment", "field_test", "pilot", "product_launch", "technology_contract", "first_purchase",
                    "qualification", "rnd", "other")
EVENT_KINDS = (*TECH_EVENT_KINDS, "not_technology")

RESEARCH_INSTRUCTIONS = """Ты — аналитик технологического радара нефтесервисной компании
(бурение, заканчивание, ГРП, КРС, добыча, промысловая инфраструктура, HSE, цифровизация).

Найди в интернете главные ТЕХНОЛОГИЧЕСКИЕ события по теме за указанный период — те, что важны
НЕФТЕСЕРВИСУ. Тема задаёт направление, а не отрасль: событие должно быть в нефтегазе или
нефтесервисе (оператор, сервисная компания, промысел, скважина, трубопровод промысла) либо из
другой отрасли, но с очевидным применением на конкретной операции нефтесервиса. Не приноси:
коммунальную и сетевую энергетику, солнечные и ветровые станции для городов и курортов,
грузоперевозки по общим трассам, общие SaaS и ИИ-сервисы, судоходство без связи с промыслом —
даже если тема («Энергетика», «Логистика», «Автоматизация») формально их покрывает. Событие — один
конкретный факт: внедрение, промышленное испытание, пилот, запуск продукта с первым заказчиком,
измеримый технический эффект (KPI), квалификация технологии, НИОКР с конкретным результатом
(прототип, испытательный центр), контракт на технологию. Не событие: обзор рынка, прогноз, отчёт
аналитиков, рейтинг, вебинар, каталог услуг, реклама без факта применения.

Что заказчик считает сигналом (по его ТОП-базе): выпуск продукта или версии с первым применением,
полевые и ресурсные испытания, пилот у оператора, первая крупная закупка технологии, промышленное
внедрение с цифрами — у крупных сервисных компаний и у небольших производителей, в России и в мире.
Перенос из горнодобычи, производства, транспорта и энергетики — да, если применение на операции
нефтесервиса очевидно (защита от столкновений техники, автономный буровой станок, ИИ-контроль
качества на edge).

Нужны ТЕХНОЛОГИЧЕСКИЕ сигналы, не бизнес-новости. Не приноси: контракт или тендер без новой
технологии («получила контракт на бурение 20 скважин»), слияния и поглощения, финансирование и
инвестиции, FID, финансовые итоги, назначения, расширение мощностей и логистических хабов без
новой технологии. Контракт — только если он про внедрение конкретной технологии, и тогда в
technology опиши её. Для каждого события назови technology — что за технология (продукт,
метод, система); если технологию назвать нельзя, это не наш сигнал. Тип события — event_kind:
- deployment — промышленное внедрение или применение у заказчика, в том числе рубеж применения
  с цифрами (число скважин или объектов, KPI, рекорд);
- field_test — полевые или ресурсные испытания;
- pilot — пилот у оператора;
- product_launch — выпуск продукта или версии;
- technology_contract — контракт на внедрение конкретной технологии;
- first_purchase — первая крупная закупка технологии;
- qualification — квалификация или сертификация технологии: одобрение классификационного общества
  (AiP, NTQ, type approval), завершение JIP или программы квалификации;
- rnd — НИОКР: прототип или демонстрация разработки, открытие испытательного центра или стенда,
  совместная разработка технологии;
- other — другое технологическое событие;
- not_technology — не технологическое событие: премия или рейтинг, контракт, тендер или соглашение
  без новой технологии, сделка, финансирование, назначение, ход стройки, стандарт или руководство,
  мероприятие, обзор. Такое лучше не приносить вовсе.

Правила:
- дата события — внутри периода; событие вне периода не включай;
- ссылка — первоисточник: пресс-релиз или новость компании-участника, отраслевое издание
  (World Oil, JPT, Offshore, Hart Energy, Rigzone, Нефтегаз.ру, «Нефтегазовая вертикаль»,
  сайты «Газпром нефти», «Роснефти», SLB, Halliburton, Baker Hughes и т. п.). Не агрегаторы,
  не пресс-релизы маркетинговых отчётов (researchandmarkets, openpr), не SEO-блоги;
- ссылка — на страницу самого события, а не на раздел сайта или главную;
- одно событие — один пункт; разные события одной компании — разные пункты;
- ищи и на английском, и на русском; российские события важны не меньше мировых: пресс-службы
  «Газпром нефти», «Роснефти», ЛУКОЙЛа, «Татнефти», «Транснефти», производителей оборудования
  и ИТ, Нефтегаз.ру, Агентство нефтегазовой информации, «Нефтегазовая вертикаль», ComNews;
- кроме громких новостей — пресс-релизы производителей о продуктах и испытаниях: такие события
  заказчик ценит не меньше;
- лучше меньше, но настоящих: не добавляй событие, ссылку на которое ты не нашёл поиском;
- title: по-русски, по правилам заголовка (кто что сделал, до 90 знаков, без тире и скобок);
- summary и why_important — по-русски, 1–3 предложения, с фактами (кто, что, где, цифры).
Упорядочь по значимости для нефтесервиса: первое — самое важное."""

def _research_schema(name: str, kind_field: str, kinds: tuple[str, ...]) -> dict[str, Any]:
    """Схема ответа поиска: события с первоисточником; у прежнего поиска — тип события, у широкого (#138) — вид
    материала. Строгий JSON: все поля обязательны."""
    properties: dict[str, Any] = {field: {"type": "string"} for field in (
        "title", "company", "event_date", "summary", "why_important", "source_url", "publisher")}
    properties["source_kind"] = {"type": "string", "enum": ["primary", "trade_press", "secondary"]}
    properties["technology"] = {"type": "string"}
    properties[kind_field] = {"type": "string", "enum": list(kinds)}
    item = {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}
    return {"name": name, "schema": {"type": "object", "additionalProperties": False, "required": ["events"],
                                     "properties": {"events": {"type": "array", "items": item}}}}


RESEARCH_SCHEMA = _research_schema("radar_topic_research", "event_kind", EVENT_KINDS)

# Широкий поиск (#138, спека #128): при «Отборе» поиск ищет все материалы нефтесервиса и нефтегаза за период, а не
# только технологии, — что брать, решает «Отбор». Вид материала от модели поиска — черновые данные находки (во вход
# агентов не идёт), поэтому перечень без «не технологии»: отбрасывать по нему нечего.
MATERIAL_KINDS = ("technology", "contract", "deal", "drilling_program", "regulation", "market", "other")

RESEARCH_INSTRUCTIONS_BROAD = """Ты — аналитик, который следит за нефтегазовой отраслью и нефтесервисом
(бурение, заканчивание, ГРП, КРС, добыча, промысловая инфраструктура, HSE, цифровизация).

Найди в интернете материалы по теме за указанный период: все значимые публикации о нефтегазе и
нефтесервисе, а не только о технологиях. Что из найденного брать, решит следующий этап, поэтому
не отбирай по своей оценке, интересно ли это заказчику. Ищи:
- технологии: внедрение, испытания, пилоты, запуск продуктов, НИОКР с результатом, квалификация;
- контракты и тендеры: подряды на бурение и сервис, поставки оборудования, итоги тендеров;
- сделки: слияния и поглощения, совместные предприятия, партнёрства, крупные инвестиции;
- программы бурения и разработки: планы бурения, ввод скважин и месторождений, FID;
- регулирование: законы, налоги, квоты, стандарты, решения государства и регуляторов;
- рынок: итоги и планы сервисных и нефтегазовых компаний, обзоры и прогнозы рынка нефтесервиса.
Тема задаёт направление поиска. Из других отраслей (горнодобыча, производство, транспорт,
энергетика) — только то, у чего очевидно применение на операции нефтесервиса. Вид материала —
material_kind:
- technology — технология и её применение;
- contract — контракт, подряд, тендер;
- deal — сделка, совместное предприятие, партнёрство, инвестиции;
- drilling_program — программа бурения или разработки, ввод скважин и месторождений;
- regulation — регулирование, налоги, стандарты, решения государства;
- market — итоги и планы компаний, обзор или прогноз рынка;
- other — другое.
Если материал о технологии, назови её в technology (продукт, метод, система), иначе technology —
пустая строка.

Правила:
- материал опубликован внутри периода;
- одно событие — один пункт со ссылкой на первоисточник: пресс-релиз или новость
  компании-участника, отраслевое или деловое издание; разные события одной компании — разные пункты;
- ссылка — на страницу самого материала, а не на раздел сайта, ленту новостей или главную; не
  агрегаторы и не SEO-блоги;
- ищи и на английском, и на русском; российские события важны не меньше мировых: пресс-службы
  нефтегазовых и сервисных компаний, производителей оборудования и ИТ, отраслевые издания;
- лучше меньше, но настоящих: не добавляй материал, ссылку на который ты не нашёл поиском;
- title: по-русски, по правилам заголовка (кто что сделал, до 90 знаков, без тире и скобок);
- summary и why_important — по-русски, 1–3 предложения, с фактами (кто, что, где, цифры).
Упорядочь по значимости для нефтесервиса: первое — самое важное."""

RESEARCH_SCHEMA_BROAD = _research_schema("radar_topic_research_broad", "material_kind", MATERIAL_KINDS)

# Модели, которые OpenAI снимает 11.12.2026: исходное поколение gpt-5 (с датой в имени — тоже). gpt-5.4, gpt-5.5 и
# новее не снимаются.
_RETIRED_MODEL_RE = re.compile(r"^gpt-5(?:-mini|-nano)?(?:-\d{4}-\d{2}-\d{2})?$", re.IGNORECASE)


def retired_model(model: str) -> bool:
    return bool(_RETIRED_MODEL_RE.match((model or "").strip()))


_STRENGTH = {"primary": 0.9, "trade_press": 0.82, "secondary": 0.6}
RESEARCH_ATTEMPTS = 2
RESEARCH_RETRY_PAUSE_SECONDS = 20.0
RETRY_HINT = ("\n\nПредыдущая попытка не нашла ни одного события. Поищи шире: на английском и на русском, "
              "по крупным сервисным компаниям и операторам, по отраслевым изданиям темы.\n")


def research_period(days: int | None = None, period_end: date | None = None) -> tuple[date, date]:
    """Период поиска темы: days дней до period_end (по умолчанию — сегодня). Один и для промпта, и
    для списка уже известного, который поиску передаёт run_discovery."""
    end = period_end or date.today()
    return end - timedelta(days=int(days or app_config.SIGNAL_RESEARCH_DAYS)), end


def covered_sites_block(sites: list[str]) -> str:
    """«Уже есть, ищи дальше» (#138): сайты, которые парсер собирает целиком, — их материалы у нас уже есть, и тратить
    на них поиски незачем (сами ссылки статей сбора новыми находками всё равно не станут)."""
    sites = [site for site in sites if site]
    if not sites:
        return ""
    return ("Эти сайты наш парсер собирает целиком, их материалы у нас уже есть: не трать на них поиски, ищи дальше, "
            "на других сайтах:\n" + ", ".join(sites))


def _add_usage(stats: dict[str, Any], response: Any) -> None:
    """Расход — за все попытки: платим и за пустую."""
    data = response.data or {}
    stats["web_search_calls"] += int(data.get("_web_search_calls") or 0)
    stats["input_tokens"] += int(getattr(response, "input_tokens", 0) or 0)
    stats["output_tokens"] += int(getattr(response, "output_tokens", 0) or 0)


def research_topic(
    topic: str,
    *,
    period_end: date | None = None,
    days: int | None = None,
    limit: int | None = None,
    topic_context: str = "",
    client_factory: Callable[[], Any] | None = None,
    heartbeat: Callable[[], None] | None = None,
    feedback: str = "",
    finds_only: bool = False,
    model: str | None = None,
    reasoning: str | None = None,
) -> dict[str, Any]:
    """События темы за период с первоисточниками. Ошибка модели — пустой итог со статусом,
    а не падение прогона: у темы остаётся поиск Brave, если он включён. finds_only (#131) — поиск только
    находит: без смысловых отсевов и без потолка числа находок, решает «Отбор»; и ищет широко (#138): все материалы
    нефтесервиса и нефтегаза, а не только технологии.

    Модель и уровень прогон радара передаёт свои — из задачи (решение ядра, #138): пустая модель — ошибка темы, а
    не откат на строку .env воркера. None — вызов на ядре (тесты, разовые команды): настройка ядра."""
    start, end = research_period(days, period_end)
    count = int(limit or app_config.SIGNAL_RESEARCH_EVENTS_PER_TOPIC)
    used_model = app_config.SIGNAL_RESEARCH_MODEL if model is None else model
    effort = app_config.SIGNAL_RESEARCH_REASONING if reasoning is None else reasoning
    stats: dict[str, Any] = {"status": "ok", "provider": "openai_web_search", "model": used_model,
                             "period": [start.isoformat(), end.isoformat()], "events": 0, "dropped": [],
                             "feedback_chars": len(feedback.strip()),
                             "web_search_calls": 0, "input_tokens": 0, "output_tokens": 0}
    if not used_model:
        return {**stats, "status": "error", "error": "модель поиска не задана (SIGNAL_RESEARCH_MODEL в .env ядра)",
                "evidence": []}
    instructions, schema = ((RESEARCH_INSTRUCTIONS_BROAD, RESEARCH_SCHEMA_BROAD) if finds_only
                            else (RESEARCH_INSTRUCTIONS, RESEARCH_SCHEMA))
    prompt = (
        f"Тема: {topic}\n"
        f"Период: с {start.isoformat()} по {end.isoformat()}\n"
        # Сколько просить у модели — настройка и при «Отборе»: потолок снят в нашем коде (finds_only ответ не режет).
        f"Сколько событий: до {count}\n"
        + (f"\nЧто входит в тему:\n{topic_context.strip()}\n" if topic_context.strip() else "")
        # Обратная связь заказчика: что одобрено, что отклонено, кому доверяет, что уже есть.
        + (f"\n{feedback.strip()}\n" if feedback.strip() else "")
    )
    client = (client_factory or (lambda: make_client(False)))()
    response = None
    errors: list[str] = []
    # Модель с поиском отвечает по-разному от вызова к вызову: сравнительный прогон 30.09 —
    # на теме автоматизации первая попытка дала события, повтор — пустой список после 15
    # поисков. Пустой ответ и временный сбой — ещё одна попытка с подсказкой искать шире.
    beat = heartbeat or (lambda: None)
    for attempt in range(1, RESEARCH_ATTEMPTS + 1):
        # Признак продвижения перед каждой попыткой: вызов с поиском молчит до
        # SIGNAL_RESEARCH_TIMEOUT_SECONDS, а воркер NL снимает задачу как зависшую после
        # 1200 с без признаков (external_worker._JOB_STALL_SECONDS) — две попытки подряд
        # без beat подходили к порогу вплотную.
        beat()
        attempt_prompt = prompt if attempt == 1 else prompt + RETRY_HINT
        try:
            response = client.research_json(
                instructions,
                attempt_prompt,
                schema,
                model=used_model,
                reasoning_effort=effort,
                # Ответ — до count событий по ~150 токенов плюс рассуждение между поисками.
                max_output_tokens=output_budget(1500 + 250 * count, effort),
                timeout=app_config.SIGNAL_RESEARCH_TIMEOUT_SECONDS,
            )
        except AttributeError as exc:  # клиент без поиска (офлайн) — повтор не поможет
            return {**stats, "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:300]}", "evidence": []}
        except (AIClientError, requests.RequestException) as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:300]}")
            response = None
            if attempt < RESEARCH_ATTEMPTS:
                # Короткий обрыв сети (прогон 30.09: DNS на 9 темах подряд) без паузы съедал
                # обе попытки за секунду.
                time.sleep(RESEARCH_RETRY_PAUSE_SECONDS * attempt)
            continue
        _add_usage(stats, response)
        if (response.data or {}).get("events"):
            break
        errors.append("пустой список событий")
    stats["attempts"] = attempt
    if errors:
        stats["retries"] = errors
    if response is None:
        return {**stats, "status": "error", "error": errors[-1] if errors else "no response", "evidence": []}
    data = response.data or {}
    cited = {_url_key(url) for url in data.get("_cited_urls") or []}
    stats["model"] = getattr(response, "model", None) or stats["model"]
    evidence = []
    events = data.get("events") or []
    for event in events if finds_only else events[:count]:
        item, reason = _event_to_evidence(event, topic, start=start, end=end, cited=cited, finds_only=finds_only)
        if item is None:
            # Отсеянное не пропадает: ядро сохранит его карточкой «Отсеяно» для разбора заказчиком.
            stats["dropped"].append({
                "title": str(event.get("title") or "")[:300], "reason": reason,
                "url": _strip_tracking(str(event.get("source_url") or "").strip()),
                "summary": str(event.get("summary") or "")[:900],
                "publisher": str(event.get("publisher") or "")[:200],
                "event_date": str(event.get("event_date") or "")[:10],
                "technology": str(event.get("technology") or "")[:300],
            })
            continue
        evidence.append(item)
    stats["events"] = len(evidence)
    return {**stats, "evidence": evidence}


def _event_to_evidence(
    event: dict[str, Any],
    topic: str,
    *,
    start: date,
    end: date,
    cited: set[str],
    finds_only: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    url = str(event.get("source_url") or "").strip()
    title = re.sub(r"\s+", " ", str(event.get("title") or "")).strip()
    if not re.match(r"https?://[^/\s]+\.[^/\s]+", url) or not title:
        return None, NO_LINK_OR_TITLE
    url = _strip_tracking(url)
    event_date = _parse_date(event.get("event_date"))
    # Дата вне периода — модель нарушила условие; без даты оставляем: судья найдёт её сам. При «Отборе» дата
    # модели — черновые данные: старое событие решает «Отбор» по тексту.
    if not finds_only and event_date and not (start - timedelta(days=3) <= event_date <= end + timedelta(days=1)):
        return None, f"дата события {event_date.isoformat()} вне периода"
    technology = re.sub(r"\s+", " ", str(event.get("technology") or "")).strip()
    event_kind = str(event.get("event_kind") or "").strip()
    # Нужны технологические сигналы: до судьи не доходит то, что поиск сам признал не технологией,
    # и находка без названной технологии. Причины раздельные — по общей «нет технологии» нельзя
    # было понять, что резал тип события. Старый ответ без полей (схема до 02.10) — не отбрасываем.
    # При «Отборе» категорию ставит он: бизнес-событие поиска не теряется как «не технология».
    if "event_kind" in event and not finds_only:
        if event_kind not in TECH_EVENT_KINDS:
            return None, "не технологическое событие (по оценке поиска)"
        if not technology:
            return None, "не названа технология"
    summary = str(event.get("summary") or "").strip()
    why = str(event.get("why_important") or "").strip()
    kind = str(event.get("source_kind") or "secondary")
    material_kind = str(event.get("material_kind") or "").strip()
    return {
        "article_id": None,
        "source_url": url,
        "title": title,
        "title_ru": title,
        "publisher": str(event.get("publisher") or "").strip() or _domain(url),
        "published_at": event_date.isoformat() if event_date else None,
        "evidence_type": "news",
        "extracted_fact": " ".join(part for part in (summary, f"Технология: {technology}." if technology else "", why) if part),
        "summary_ru": summary,
        "strength": _STRENGTH.get(kind, 0.6),
        "topic": topic,
        "raw_payload": {
            "evidence_source": EVIDENCE_SOURCE,
            # Русский заголовок модели поиска: докачка страницы ставит в title заголовок страницы, а «Дедупу» прогона
            # нужен и этот (пара английской страницы с русской статьёй ленты, замер 04.11). Черновые данные.
            "research_title": title,
            "company": str(event.get("company") or "").strip(),
            "source_kind": kind,
            "technology": technology,
            "event_kind": event_kind or None,
            # Вид материала широкого поиска (#138) — черновые данные, как тип события прежнего.
            **({"material_kind": material_kind} if material_kind else {}),
            "why_important": why,
            "research_summary": summary,
            # Сослалась ли модель на этот адрес в найденном — признак, что он из поиска, а
            # не из памяти модели. Вместе с докачкой страницы решает, верить ли ссылке.
            "cited_by_search": _url_key(url) in cited,
        },
    }, ""


def verify_research_evidence(evidence: list[dict[str, Any]], *,
                             finds_only: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """После докачки страниц: оставить подтверждённые находки.

    Подтверждена, если страница открылась (full_text_fetched) или модель сослалась на адрес в
    найденном (cited_by_search: сайт мог закрыться от бота). Ни то ни другое — вероятно,
    выдуманная ссылка: такой находке судья поверил бы на слово. Сводку модели возвращаем в
    summary_ru — докачка заменила её текстом страницы, а судье нужны обе.

    При «Отборе» (finds_only, #131) во вход агента идёт текст издателя: находка без открытой страницы и
    страница-список (is_list_page) — технический отсев поиска, а не публикация."""
    kept, dropped = [], []
    for item in evidence:
        payload = item.get("raw_payload") or {}
        if payload.get("evidence_source") != EVIDENCE_SOURCE:
            kept.append(item)
            continue
        reason = None
        if not (payload.get("full_text_fetched") or payload.get("cited_by_search")):
            reason = UNCONFIRMED_LINK
        elif finds_only and not payload.get("full_text_fetched"):
            reason = PAGE_NOT_FETCHED
        elif finds_only and is_list_page(str(item.get("title") or "")):
            reason = LIST_PAGE
        if reason is None:
            kept.append({**item, "summary_ru": payload.get("research_summary") or item.get("summary_ru")})
        else:
            dropped.append({"title": str(item.get("title") or "")[:300], "url": item.get("source_url"),
                            "reason": reason,
                            "summary": str(payload.get("research_summary") or item.get("extracted_fact") or "")[:900],
                            "publisher": item.get("publisher"), "event_date": item.get("published_at")})
    return kept, dropped


def is_list_page(title: str) -> bool:
    """Страница-список, а не одна публикация: заголовок короче LIST_PAGE_TITLE_WORDS слов («Press Releases»,
    «News», «JPT October 2026 Issue»). Черновая проверка: у статьи о событии заголовок обычно длиннее. Иероглиф —
    слово: китайский заголовок без пробелов иначе считался бы одним словом (на разметке эксперта 23.10 те же 7 из 8
    страниц-списков без лишних)."""
    return len(_TITLE_WORD_RE.findall(title)) < LIST_PAGE_TITLE_WORDS


_TITLE_WORD_RE = re.compile(r"[\u3400-\u9fff]|[^\W\u3400-\u9fff]+")


def is_research_evidence(item: dict[str, Any]) -> bool:
    return (item.get("raw_payload") or {}).get("evidence_source") == EVIDENCE_SOURCE


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _strip_tracking(url: str) -> str:
    # Поиск OpenAI дописывает utm_source=openai — в ключе ссылки ему не место.
    url = re.sub(r"([?&])utm_[a-z]+=[^&#]*", r"\1", url)
    return re.sub(r"[?&]+(#|$)", r"\1", url).rstrip("?&")


def _url_key(url: str) -> str:
    value = re.sub(r"^https?://", "", _strip_tracking(str(url or "")).strip().lower())
    return value.split("#", 1)[0].rstrip("/").removeprefix("www.")


def _domain(url: str) -> str:
    return _url_key(url).split("/", 1)[0]
