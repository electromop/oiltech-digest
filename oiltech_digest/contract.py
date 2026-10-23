"""Договор ядра (РФ) и воркеров зарубежного контура (NL).

Стороны выкатываются порознь: ядро — скриптом на РФ, воркеры — владельцем на NL, по
одному. Здесь две вещи, которые им нужно понимать одинаково.

1. Кто на той стороне. 18.09 и 21.09 воркер старой сборки падал на итоге новой формы, а
   «пересобран ли NL» узнавали по косвенному полю. Теперь воркер при каждом запросе шлёт
   сборку (git SHA) и номер контракта; ядро помнит их по контейнеру, а сторож поднимает
   тревогу contract_mismatch у живого потребителя с другим номером.

   CONTRACT повышается при любой несовместимой правке протокола: формы payload и итога,
   эндпоинтов /api/external-worker/*, нового вида задачи. Порядок выката — ядро (обратно
   совместимо), потом NL.
   История: 1 — сборка и контракт в заголовках, возврат задачи на остановке (release);
   2 — вид задачи signal_card, агент «Карточка сигнала» (#96): сборка NL контракта 1 отказывает
   ему ошибкой «Unsupported external job kind», поэтому шаг планировщика включается флагом
   SIGNAL_CARD_ENABLED только после пересборки NL;
   3 — вид задачи admission_notify, код допуска в Telegram (#113) в очереди external-notify: её
   слушает воркер сбора, отправку включает флаг ADMISSION_NOTIFY_ENABLED после пересборки NL;
   4 — вид задачи otbor, агент «Отбор» (#130): решение «брать или нет» у статей ленты; шаг
   планировщика включается флагом OTBOR_ENABLED только после пересборки NL.
   Совместимые добавки номер не меняют — старая сторона их молча пропускает: 28.09 — поле
   result у fail (снимок сделанного у зависшего пакета) и only в payload process_articles;
   29.09 (сессия G, ADR 0002) — profile у критериев в payload и profile + criteria_snapshot в
   балле итога. Итог старой сборки NL без них ядро пишет, построив снимок само по id
   подпунктов; 29.09 (#79) — признак incident_without_solution и ai_score_raw подпунктов в
   итоге оценки: ядро их не читает, балл пишет как раньше; 17.10 — last_seen_day у карточек
   снимка радара: старая сборка NL берёт известное поиску по fresh, новая без поля — по
   first_seen_day.

2. Что осталось задаче, возвращённой на остановке. Пересборка NL останавливает воркер
   посреди задачи (SIGTERM). Раньше такая задача висела до конца аренды (600 с), а
   оплаченная часть ИИ-пакета выбрасывалась и оплачивалась заново (ADR 0001). Теперь воркер
   возвращает её сам (release) с тем, что успел, а ядро решает здесь, что осталось сделать.
"""

from __future__ import annotations

from typing import Any

CONTRACT = 4
HEADER_BUILD = "X-Worker-Build"
HEADER_CONTRACT = "X-Worker-Contract"


def consumer_of(worker_id: str | None) -> str:
    """Потребитель — контейнер NL, а не поток: полоса сбора держит nl-fetch-1#1..#3."""
    return str(worker_id or "").split("#", 1)[0].strip() or "unknown"


def parse_contract(value: str | None) -> int | None:
    try:
        return int(str(value).strip()) if value is not None else None
    except ValueError:
        return None

# Пакет «N необработанных статей» без явного N (так его ставит /api/jobs/process).
PROCESS_LIMIT_DEFAULT = 5

# Пакеты по статьям: итог — по строке на статью, её можно применить без остальных.
_ARTICLE_BATCHES = frozenset({"process_articles", "recheck_relevance", "translate_titles"})
# Виды, чей частичный итог ядро принимает. Сбор ИИ не зовёт — повторить его дёшево;
# итог документа — одна карточка на весь файл, половина карточки не нужна никому. Карточки
# сигналов (#96) — по строке на сигнал, как статьи; решения «Отбора» (#130) — по строке на публикацию.
PARTIAL_KINDS = _ARTICLE_BATCHES | {"reprint_review", "signal_card", "otbor"}


def accepts_partial(kind: str | None, result: dict[str, Any] | None,
                    payload: dict[str, Any] | None = None) -> bool:
    """Пробный прогон (dry_run) частичного итога не принимает: его результат — отчёт целиком
    (recheck-dry-show, вердикты судьи), по частям он не складывается, и сделанная часть
    пропала бы из отчёта (ревью 23.09). Такую задачу остановка возвращает целиком."""
    return (
        str(kind or "") in PARTIAL_KINDS
        and bool(result)
        and bool((result or {}).get("partial"))
        and not (payload or {}).get("dry_run")
    )


def without_reservation(payload: dict[str, Any]) -> dict[str, Any]:
    """Резерв статей держится за выданной задачей; вернувшаяся в очередь его отпускает,
    при следующей выдаче он считается заново."""
    return {key: value for key, value in (payload or {}).items() if key != "reserved_article_ids"}


def done_count(result: dict[str, Any] | None) -> int:
    """Сколько строк сделано в частичном итоге: статей пакета, пар судьи, карточек сигналов или решений
    «Отбора»."""
    result = result or {}
    return len(result.get("articles") or result.get("verdicts") or result.get("cards")
               or result.get("decisions") or [])


def remaining_after_partial(kind: str | None, payload: dict[str, Any],
                            result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Что осталось задаче после частичного итога; None — ничего, задача выполнена.

    Сделанное вычитается, чтобы следующая выдача не звала модель за уже записанное:
    - явный список статей — без обработанных (в том числе с ошибкой: полный прогон тоже
      не повторил бы их внутри задачи);
    - пакет «N необработанных статей» — N уменьшается на сделанное, а выборка при выдаче
      сама обходит записанные;
    - пары судьи перепечаток — без рассуженных;
    - сигналы карточек — без тех, на которые модель уже ответила (в том числе ответом не по схеме:
      вызов оплачен);
    - статьи «Отбора» — без тех, о которых воркер вернул строку (ответ или сбой модели).
    """
    rest = without_reservation(payload)
    if not accepts_partial(kind, result, payload):
        return rest
    kind = str(kind)
    if kind == "signal_card":
        answered = {str(card.get("signal_id")) for card in result.get("cards") or [] if isinstance(card, dict)}
        left = [signal_id for signal_id in rest.get("signal_ids") or [] if str(signal_id) not in answered]
        return {**rest, "signal_ids": left} if left else None
    if kind == "otbor":
        answered = {str(item.get("ref")) for item in result.get("decisions") or [] if isinstance(item, dict)}
        left = [article_id for article_id in rest.get("article_ids") or [] if f"article:{article_id}" not in answered]
        return {**rest, "article_ids": left} if left else None
    if kind in _ARTICLE_BATCHES:
        done = {int(item["article_id"]) for item in result.get("articles") or []}
        article_ids = [int(item) for item in rest.get("article_ids") or []]
        if article_ids:
            left = [article_id for article_id in article_ids if article_id not in done]
            return {**rest, "article_ids": left} if left else None
        if kind == "process_articles":
            left_limit = int(rest.get("limit") or PROCESS_LIMIT_DEFAULT) - len(done)
            return {**rest, "limit": left_limit} if left_limit > 0 else None
        return rest
    judged = {(int(item["a_id"]), int(item["b_id"])) for item in result.get("verdicts") or []}
    pairs = [pair for pair in rest.get("pairs") or [] if (int(pair["a_id"]), int(pair["b_id"])) not in judged]
    return {**rest, "pairs": pairs} if pairs else None
