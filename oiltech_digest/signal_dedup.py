"""Дубли радара: одно событие: одна карточка.

Перенос механизма перепечаток MVP-1 (`processing/reprints.py`): дешёвое правило
отбирает пары-кандидаты, судья отвечает на один вопрос «одно ли это событие»,
а группы и главную карточку выбирает код.

Ключа сигнала для этого не хватало: он хеширует адрес или первые слова факта, а одно
событие приходит разными статьями и разными кластерами. Прогон 18.09 дал четыре
карточки про покупку Velocity Geomatics компанией ZenaTech в одной теме, две: про
завод сорбентов «Татнефти», четыре: про FleetRabbit, пять обзоров лития из пластовых вод.

Судья зовёт OpenAI, поэтому работает на внешнем воркере (`run_discovery`);
ядро только пишет решения (`apply_discovery`).

Одно событие: одна карточка во всей базе (блок А5 схемы агентов, 04.11): в снимке прогона и видимые сигналы
ленты (origin feed). Они сверяются с находками прогона и со свежими карточками радара, но не между собой (_eligible):
ленту с лентой склеивают перепечатки (processing/reprints.py). Прогон радара прячет только карточки радара: сигнал
ленты дублем не становится никогда (assign_duplicates). Замер прогона 51351 (01-04.11): у 9 из 24 находок то же
событие уже было в ленте под другой ссылкой, у пяти принятых: видимой карточкой."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import contextvars
import itertools
import re
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from oiltech_digest import config as app_config, word_forms
from oiltech_digest.processing.openai_client import output_budget

SIGNAL_DUPLICATE_INSTRUCTIONS = """Ты сверяешь две карточки технологического радара и отвечаешь на ОДИН вопрос:
про одно ли они событие.

Одно событие — один конкретный факт: одна сделка, один запуск, один контракт, одно
внедрение, одно испытание, один пилот, одна новая норма. Карточки про него собирает
разная выдача: другой заголовок, другая тема радара, другие ссылки, другая оценка.
Это НЕ делает его другим событием.

same_event=true, если совпадают ГЛАВНЫЕ действующие лица и сам факт: кто, что сделал,
с чем. Различия в формулировке, теме радара, оценке и числе ссылок значения не имеют.
Две карточки-обзора одного и того же явления без своего конкретного события
(«перспективы добычи лития из пластовых вод в России», «дроны для инспекции
трубопроводов») — тоже true: это один сигнал, разложенный по разным темам.

same_event=false, если:
- это разные эпизоды одной истории (план и начало строительства; пилот и промышленный
  запуск; подписание и исполнение контракта);
- совпадает только компания или технология, а факты разные (два разных контракта одной
  компании; одна технология у разных компаний; разные месторождения);
- одна карточка — обзор явления, а другая — конкретное событие внутри него: обзор НЕ
  дубль события, даже если его упоминает;
- числа по одному параметру противоречат друг другу (объём, сумма, мощность, число
  единиц техники) — признак разных событий.

reason — одна короткая фраза по-русски: что за событие и почему решение такое.
Ответ строго по JSON Schema."""

SIGNAL_DUPLICATE_SCHEMA = {
    "name": "signal_duplicate_verdict",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["same_event", "reason"],
        "properties": {
            "same_event": {"type": "boolean"},
            "reason": {"type": "string"},
        },
    },
}

# Замер 18.09 на 70 сигналах прода: пересечение основ заголовка от 0,25 или общая
# компания дают 68 пар и находят все пять известных групп дублей; при 0,35 теряется
# половина обзоров по литию. Точность держит судья, порог отвечает только за охват.
PAIR_MIN_OVERLAP = 0.25
# Потолок расхода на прогон. Замер 19.09: 120 пар: $0,040 (gpt-5-mini), то есть
# $0,00033 за пару; 400 пар: около $0,13. При потолке 120 из 322 пар непроверенными
# остались как раз дубли: ZenaTech и FleetRabbit прошли новыми карточками. Ядро может
# передать свой потолок в снимке (dedup_max_pairs): без пересборки воркера. С сигналами ленты
# в снимке (04.11), 1000: замер прогона 51351-135 пар ленты на 24 находки, $0,00011 за пару на gpt-6-luna.
MAX_JUDGED_PAIRS = 1000
# Происхождение узла снимка: сигнал ленты (repository.SIGNAL_ORIGIN_FEED; модуль работает на NL без базы).
FEED_ORIGIN = "feed"


class DedupSetupError(ValueError):
    """Модель дедупа не задана: прогон радара падает с понятной причиной до поиска."""


def configured_model() -> str:
    """Модель судьи пар: явная настройка SIGNAL_DEDUP_MODEL, без отката на модель ленты (спека #128, история 48).

    До 03.11 дедуп молча шёл на OPENAI_MODEL (gpt-5-mini, её снимают 11.12.2026), а сбой пары считается «разными
    событиями»: прогон выглядел бы зелёным, а на радаре копились бы дубли (риск из плана переезда 18.10)."""
    if not app_config.SIGNAL_DEDUP_MODEL:
        raise DedupSetupError("SIGNAL_DEDUP_MODEL не задан: модель ленты дедуп радара не берёт")
    return app_config.SIGNAL_DEDUP_MODEL


# Положительные вердикты эксперта заказчика: те же, что питают подсказки поиска (signal_feedback), и
# «Оценка завышена» (03.10): сигнал годный, завышен только балл: подсказкой поиска не служит.
_POSITIVE_VERDICTS = {"strong_signal", "approved", "watch_later", "needs_better_source", "bad_translation", "overrated"}

# Модель пишет издателя или заглушку вместо компании: «не указаны в источниках»,
# «bigchallenges.ru (источник)», «kissflow (publisher)». Общий издатель: не общий участник.
_PLACEHOLDER_COMPANY = re.compile(r"не указ|упомин|источник|публикатор|publisher|публикует|публицист")


def wait_beating(pool: ThreadPoolExecutor, futures: Iterable[Future], beat: Callable[[], None]) -> None:
    """Ждать вызовы пула судьи пар, подавая признак продвижения аренды NL из основного
    потока, а не из рабочих.

    beat бросает StopRequested или LeaseLost, тогда ещё не начатые вызовы снимаются: выход
    из `with` ждёт пул целиком (shutdown(wait=True)) и иначе оплатил бы всю очередь для
    прогона, который уже отпущен. Начатые (не больше размера пула) доработают."""
    pending = set(futures)
    try:
        while pending:
            beat()
            _, pending = wait(pending, timeout=20, return_when=FIRST_COMPLETED)
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise


def title_stems(signal: dict[str, Any]) -> set[str]:
    return word_forms.title_stems(signal.get("title_ru") or signal.get("title") or "")


def title_variants(signal: dict[str, Any]) -> list[set[str]]:
    """Основы каждого заголовка карточки: русского, исходного и русских заголовков поиска (research_titles).

    Пара сравнивается по лучшему сочетанию, а не по одному заголовку: у находки на этапе поиска заголовок страницы
    (часто английский), у сигнала ленты: исходный и русский. Замер 04.11 на прогоне 51351: заголовок страницы против
    исходного заголовка статьи ловит 23 из 29 известных пар ленты и поиска, с русским заголовком поиска: 27."""
    texts = [signal.get("title_ru"), signal.get("title"), *(signal.get("research_titles") or [])]
    variants: list[set[str]] = []
    for text in texts:
        stems = word_forms.title_stems(text or "")
        if stems and stems not in variants:
            variants.append(stems)
    return variants


def _title_overlap(left: list[set[str]], right: list[set[str]]) -> float:
    return max((_overlap(a, b) for a in left for b in right), default=0.0)


def company_keys(signal: dict[str, Any]) -> set[str]:
    keys = set()
    for company in signal.get("companies") or []:
        raw = str(company).strip().lower()
        if not raw or _PLACEHOLDER_COMPANY.search(raw):
            continue
        key = re.sub(r"\(.*?\)", "", raw)
        key = re.sub(r"[«»\"'?]", "", key)
        # Модель пишет участников одной строкой: «EDF / MethaneSAT» против «MethaneSAT»
        # в соседней карточке: без разбора по «/» общей компании не видно, и пара
        # №107–№3 одного события не попала к судье (20.09). Черновая компания поиска, через запятую:
        # «Halliburton, Keystone AS» против «Halliburton; Keystone AS» (пара прогона 51351, 04.11).
        for part in re.split(r"\s*[/;,]\s*", key):
            # Форма собственности, не имя: «Keystone AS» это «Keystone», а «Inc.» после запятой: не компания.
            part = " ".join(word_forms.bare_words(part))
            if len(re.findall(r"[a-zа-яё]", part)) >= 2:
                keys.add(part)
    return keys


def url_keys(urls: list[str] | set[str]) -> set[str]:
    keys = set()
    for url in urls or []:
        value = re.sub(r"^https?://", "", str(url or "").strip().lower())
        value = value.split("#", 1)[0].split("?", 1)[0].rstrip("/").removeprefix("www.")
        if value:
            keys.add(value)
    return keys


def _overlap(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def _eligible(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Пару стоит судить, только если в ней есть что решать.

    Свежая карточка из этого прогона: всегда. Две сохранённые: только если одна из
    них свежая и не разобрана: иначе каждый прогон заново оплачивал бы одни и те же
    пары, а две разобранные экспертом сверялись бы вопреки его решению. Сигнал ленты: с находкой
    прогона и со свежей неразобранной карточкой радара (её и склеят с ним: сигнал ленты дублем
    не становится, assign_duplicates), но не с другим сигналом ленты: ленту с лентой склеивают
    перепечатки. Пару открывает свежесть карточки радара, а не сигнала ленты: у свежего сигнала
    ленты со старой карточкой радара дублем стала бы лента, а её прогон не прячет (04.11)."""
    if a["kind"] == "new" or b["kind"] == "new":
        return True
    feed = [node for node in (a, b) if node.get("origin") == FEED_ORIGIN]
    if len(feed) == 2:
        return False
    if feed:
        # Скрытая карточка радара (качество, отказ «Отбора») с лентой не сверяется: её склейка экран не изменит.
        radar = b if feed[0] is a else a
        return bool(radar["fresh"] and not radar["reviewed"] and not radar.get("hidden"))
    return any(node["fresh"] and not node["reviewed"] for node in (a, b))


def find_pairs(nodes: list[dict[str, Any]], *, min_overlap: float = PAIR_MIN_OVERLAP) -> list[tuple[int, int, float]]:
    """Пары-кандидаты: похожий заголовок, общая компания или общая ссылка.

    Порядок, для потолка: сначала пары со свежими карточками (решать надо сейчас),
    среди них: с общей компанией или ссылкой, потом по убыванию сходства заголовков.
    Общая компания, сильный признак даже при низком сходстве: английский заголовок
    против русского (ZenaTech, 19.09) даёт почти ноль общих основ. Сходство заголовков:
    лучшее из сочетаний заголовков пары (title_variants)."""
    features = [
        (title_variants(node["signal"]), company_keys(node["signal"]), url_keys(node.get("urls") or []))
        for node in nodes
    ]
    pairs = []
    for i, j in itertools.combinations(range(len(nodes)), 2):
        if not _eligible(nodes[i], nodes[j]):
            continue
        titles_i, companies_i, urls_i = features[i]
        titles_j, companies_j, urls_j = features[j]
        overlap = _title_overlap(titles_i, titles_j)
        shared = bool(companies_i & companies_j or urls_i & urls_j)
        if overlap >= min_overlap or shared:
            pairs.append((i, j, overlap, shared))
    pairs.sort(key=lambda pair: (
        0 if "new" in (nodes[pair[0]]["kind"], nodes[pair[1]]["kind"]) else 1,
        0 if pair[3] else 1,
        -pair[2],
    ))
    return [(i, j, overlap) for i, j, overlap, _shared in pairs]


def _hosts(urls: list[str] | set[str]) -> str:
    hosts = []
    for url in urls or []:
        host = urlsplit(str(url) if "://" in str(url) else f"https://{url}").netloc.lower().removeprefix("www.")
        if host and host not in hosts:
            hosts.append(host)
    return ", ".join(hosts[:5]) or "не указаны"


def pair_prompt(a: dict[str, Any], b: dict[str, Any]) -> str:
    """Две карточки для судьи. Исходный заголовок, рядом с русским, если они разные: у сигнала ленты и у находки
    поиска (заголовок страницы и русский заголовок модели поиска): оба."""
    blocks = []
    for label, node in (("A", a), ("B", b)):
        signal = node["signal"]
        title = str(signal.get("title_ru") or signal.get("title") or "")
        original = str(signal.get("title") or "")
        blocks.append("\n".join([
            f"Карточка {label}",
            f"заголовок: {title}",
            *([f"исходный заголовок: {original}"] if original and original != title else []),
            f"компании: {', '.join(str(c) for c in signal.get('companies') or []) or 'не указаны'}",
            f"тема радара: {signal.get('theme') or ''}",
            f"суть: {str(signal.get('summary') or '')[:600]}",
            f"источники: {_hosts(node.get('urls') or [])}",
        ]))
    return "\n\n".join(blocks)


def _rank(node: dict[str, Any]) -> tuple:
    """Кто остаётся главной карточкой группы.

    Разобранная экспертом (сначала одобренная): её он уже видел и оценил; затем уже
    сохранённая, и из них, появившаяся раньше: номер карточки эксперт использует как
    ссылку (19.09 вчерашняя №55 FleetRabbit ушла в сегодняшнюю №99: у той было на
    одну ссылку больше); и только потом свежая из прогона.

    Скрытая с экрана (качество, архив: `hidden` из снимка ядра): последней, если человек
    её не отклонял: иначе новая находка того же события ушла бы в неё дублем и пропала с
    экрана вместе с ней (02.10). Отклонённая остаётся главной: её повтор не возвращается.

    В одной ступени сигнал ленты: раньше карточки радара (04.11): одно событие: карточка ленты,
    радар её дополняет. Сигнал ленты дублем не становится (assign_duplicates), и если бы главной
    была более ранняя карточка радара, статья ленты, пришедшая после находки, осталась бы второй
    карточкой."""
    signal = node["signal"]
    day = ""
    verdict = node.get("verdict")
    if node["kind"] == "existing" and node.get("hidden") and (not verdict or verdict in _POSITIVE_VERDICTS):
        tier = 4
    elif node["kind"] == "existing" and node["reviewed"]:
        tier = 0 if verdict in _POSITIVE_VERDICTS else 1
    elif node["kind"] == "existing":
        tier = 2
        day = str(signal.get("first_seen_day") or "")
    else:
        tier = 3
    return (
        tier,
        0 if node.get("origin") == FEED_ORIGIN else 1,
        day,
        -float(signal.get("score") or 0),
        -int(signal.get("evidence_count") or 0),
        int(node.get("id") or 0),
        node.get("order", 0),
    )


def assign_duplicates(nodes: list[dict[str, Any]], edges: list[tuple[int, int, str]]) -> dict[int, tuple[int, str]]:
    """Звезда, а не цепочка: дублем становится только карточка, которую судья прямо
    сравнил с главной. Транзитивная склейка (A~B, B~C → A~C) собрала бы обзор лития
    и конкретный пилот в одну группу через третью карточку.

    Разобранная экспертом карточка не становится дублем никогда. Сигнал ленты, тоже: его
    прячут только перепечатки и человек; прогон радара мог бы спрятать свежую статью ленты в
    карточку радара прошлого месяца, которой в ленте этого месяца нет (04.11)."""
    neighbours: dict[int, dict[int, str]] = {index: {} for index in range(len(nodes))}
    for i, j, reason in edges:
        neighbours[i][j] = reason
        neighbours[j][i] = reason
    order = sorted(range(len(nodes)), key=lambda index: _rank(nodes[index]))
    position = {index: place for place, index in enumerate(order)}
    assigned: dict[int, tuple[int, str]] = {}
    for primary in order:
        if primary in assigned:
            continue
        for other in sorted(neighbours[primary], key=lambda index: position[index]):
            if other in assigned or position[other] < position[primary]:
                continue
            if nodes[other]["kind"] == "existing" and (nodes[other]["reviewed"]
                                                       or nodes[other].get("origin") == FEED_ORIGIN):
                continue
            assigned[other] = (primary, neighbours[primary][other])
    return assigned


def dedupe(
    nodes: list[dict[str, Any]],
    *,
    client_factory: Callable[[], Any],
    heartbeat: Callable[[], None] | None = None,
    max_pairs: int = MAX_JUDGED_PAIRS,
) -> dict[str, Any]:
    """Отобрать пары, спросить судью, собрать группы. В базу не ходит.

    Клиент создаётся, только если есть что судить."""
    beat = heartbeat or (lambda: None)
    pairs = find_pairs(nodes)
    judged = pairs[:max_pairs]
    edges: list[tuple[int, int, str]] = []
    stats = {"pairs": len(pairs), "judged": 0, "same": 0, "errors": 0,
             "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    # Сбой вызова ниже: «разные события» у пары, поэтому отсутствие модели проверяется до вызовов.
    model = configured_model() if judged else None
    client = client_factory() if judged else None

    def ask(i: int, j: int) -> Any:
        return client.complete_json(
            SIGNAL_DUPLICATE_INSTRUCTIONS,
            pair_prompt(nodes[i], nodes[j]),
            SIGNAL_DUPLICATE_SCHEMA,
            max_output_tokens=output_budget(700, app_config.SIGNAL_DEDUP_REASONING),
            model=model,
            reasoning_effort=app_config.SIGNAL_DEDUP_REASONING,
        )

    # Пары, параллельно (SIGNAL_DEDUP_CONCURRENCY): до 400 вызовов за прогон по одному шли
    # долго. Ответы разбираются в порядке пар, признак продвижения: из основного потока.
    responses: list[Any] = [None] * len(judged)
    failed: set[int] = set()
    workers = min(app_config.SIGNAL_DEDUP_CONCURRENCY, len(judged)) or 1
    if workers == 1:
        for index, (i, j, _overlap_value) in enumerate(judged):
            beat()
            try:
                responses[index] = ask(i, j)
            except Exception:  # noqa: BLE001 - сбой одной пары не должен ронять прогон: пара остаётся «разные»
                failed.add(index)
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="radar-dedup") as pool:
            futures = {pool.submit(contextvars.copy_context().run, ask, i, j): index
                       for index, (i, j, _overlap_value) in enumerate(judged)}
            wait_beating(pool, futures, beat)
        for future, index in futures.items():
            try:
                responses[index] = future.result()
            except Exception:  # noqa: BLE001 - пара остаётся «разные»
                failed.add(index)
    for index, (i, j, _overlap_value) in enumerate(judged):
        if index in failed or responses[index] is None:
            stats["errors"] += 1
            continue
        response = responses[index]
        stats["judged"] += 1
        stats["input_tokens"] += int(getattr(response, "input_tokens", 0) or 0)
        stats["output_tokens"] += int(getattr(response, "output_tokens", 0) or 0)
        try:
            stats["cost_usd"] += float(response.cost_usd)
        except Exception:  # noqa: BLE001 - цена неизвестной модели не повод терять решение
            pass
        data = response.data or {}
        if data.get("same_event") is True:
            stats["same"] += 1
            edges.append((i, j, str(data.get("reason") or "")[:300]))
    stats["cost_usd"] = round(stats["cost_usd"], 4)
    return {"assigned": assign_duplicates(nodes, edges), "stats": stats}
