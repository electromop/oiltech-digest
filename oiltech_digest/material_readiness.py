"""Готовность материала выпуска (#199, спека #197): правило в одном месте, отметка «в дайджест» как повод для прогона
конвейера, перепроверка готовности и состояние месяца для экрана «Дайджест», вкладки «Журнал обмена» и пакета выпуска.
Правило касается выпуска платформы вообще, не только потребителя.

Материал выпуска (статья ленты, находка радара, материал коллеги) готов, когда у сигнала его карточки есть суть и
русский заголовок (текст модели или правка людей), «Карточка» отработала, а баллы посчитаны там, где они положены:
второй вызов «Карточки» оценивает принятое «Отбором» категории «бизнес» или «технология». У «другого» баллов нет;
решённое до «Отбора» (история) не оценивается (решение владельца 22.11: «Старые материалы не оцениваем»), у него баллы
прежней цепочки, и готовность их не ждёт. Сигнал
карточки: у статьи её сигнал ленты, у дубля корень склейки (так карточка уходит потребителю, и только корень проходит
конвейер). У статьи без сигнала ленты (собрана до 01.08.2026) карточки нет.

Отметка «в дайджест» (db/marks) ставит материалу недостающие шаги без окна шагов планировщика, по полосе пересчётов:
суть и перевод (у статьи обработка ленты по номеру, у находки и материала «Суть и перевод»), основной вызов «Карточки»,
баллы. Ставится только шаг, которому уже есть из чего считать: «Карточка» после «Отбора» ждёт суть (порядок конвейера,
#132), баллы ждут «Карточку». Следующий шаг ставит after_step, когда ядро записало итог предыдущего (кто бы его ни
сделал: задача готовности или поток дня), пока отметка стоит. Порядок держит запись итога, а не очередь: полосы идут
параллельно, задачу могут отложить. Шаг, задача которого уже идёт, второй раз не ставится. Без отметки шаги
планировщика работают в своём окне, как раньше.

Перепроверка готовности (recheck_step, шаг планировщика каждого цикла): в последний день перед закрытием месяца (на
проде 10-е: месяц закрывается в ночь на 11-е) с часа READINESS_RECHECK_HOUR, а после закрытия каждый цикл, пока шаг
выпуска для потребителя не собрал пакет, ставит заново недостающие шаги неготовых материалов месяца. После закрытия
только месяцы этого шага (флаг и первый месяц, решение владельца 22.11): месяцы стартовой загрузки прогоняются по
номерам по его «да». Попыток шага на материал за сутки не больше суточного предела шагов конвейера (DAILY_ATTEMPTS их
хранилищ), по любому поводу.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from oiltech_digest import config, feed_window
from oiltech_digest.db import connection, exchange_store, material_readiness_store, repository
from oiltech_digest.db import signal_card_store, signal_scores_store, signal_summary_store

logger = logging.getLogger(__name__)

SUMMARY, TRANSLATION, CARD, SCORES = "summary", "translation", "card", "scores"
# Шаги готовности по порядку конвейера. «Гипотеза для Компании» войдёт сюда с её агентом (#163, отложен): черновик
# агента или решение человека, в том числе пустое; до тех пор гипотеза выпуск не держит.
STEPS = (SUMMARY, TRANSLATION, CARD, SCORES)
LABELS = {SUMMARY: "нет сути", TRANSLATION: "нет перевода заголовка", CARD: "нет карточки", SCORES: "нет баллов"}
# Повод задачи шага (trigger в задаче): отметка «в дайджест» и перепроверка готовности.
MARK, RECHECK = "digest_mark", "readiness"
# Суточный предел задач шага на материал (вид задачи → предел шага конвейера); у обработки ленты тот же, что у «Сути и
# перевода» сигналов.
DAILY_ATTEMPTS = {
    "process_articles": signal_summary_store.DAILY_ATTEMPTS,
    "signal_summary": signal_summary_store.DAILY_ATTEMPTS,
    "signal_card": signal_card_store.DAILY_ATTEMPTS,
    "signal_scores": signal_scores_store.DAILY_ATTEMPTS,
}
# Сколько неготовых материалов называть в одной строке (команда, отказ шага); остальные числом.
_NAMED = 20
# Ключ pg_advisory_xact_lock постановки шагов: проверка идущих задач и постановка идут по одной.
_LOCK = 19_911_199
# Стадия обработки ленты → колонка карточки статьи, где она лежит.
_ARTICLE_STAGES = {SUMMARY: "article_summary", TRANSLATION: "article_title"}
# Задачи шагов конвейера, после записи итога которых готовность ставит следующий шаг отмеченным материалам.
CHAIN_KINDS = frozenset({"process_articles", "signal_summary", "signal_card", "signal_scores"})


@dataclass(frozen=True)
class Material:
    """Материал выпуска: статья ленты (kind article) или сигнал (kind signal) с номером в выпуске; сигнал материала (у
    статьи её сигнал ленты, у статьи без него пусто), сигнал карточки (корень склейки) и чего не хватает до готовности.
    state: что правило прочитало (material_readiness_store), для постановки шагов."""

    kind: str
    id: int
    signal_id: int | None
    root: int | None
    missing: tuple[str, ...]
    state: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def ready(self) -> bool:
        return not self.missing

    def number(self) -> str:
        return number_label(self.root, self.id)


def number_label(signal_id: int | None, material_id: int) -> str:
    """Как материал назвать администратору: номер карточки, у статьи без сигнала ленты номер статьи."""
    return f"№{signal_id}" if signal_id is not None else f"статья №{material_id}"


def missing(signal: dict | None, article: dict | None = None) -> tuple[str, ...]:
    """Правило готовности материала, одно в коде: каких шагов (STEPS) не хватает. signal: сигнал карточки
    (material_readiness_store.signal_states); без сигнала (статья без сигнала ленты) article: её статья
    (material_readiness_store.article_states), и карточки у неё нет."""
    if signal is None:
        text = article or {}
        present = {SUMMARY: text.get("article_summary"), TRANSLATION: text.get("article_title"), CARD: False,
                   SCORES: True}
    else:
        present = {SUMMARY: signal["has_summary"], TRANSLATION: signal["has_title"], CARD: signal["has_card"],
                   SCORES: signal["has_scores"] or not signal["scores_due"]}
    return tuple(step for step in STEPS if not present[step])


def labels(steps: Sequence[str]) -> str:
    """Чего не хватает, словами: «нет перевода заголовка, нет карточки»."""
    return ", ".join(LABELS[step] for step in steps)


def lacking_text(lacking: Sequence[tuple[str, Sequence[str]]], total: int) -> str:
    """«не готовы 2 из 3: №12 (нет карточки), №15 (нет сути, нет баллов)»: неготовые (как назвать, чего не хватает)
    из total материалов; для отказа шага выпуска, ответа «как есть» и команд."""
    named = ", ".join(f"{number} ({labels(steps)})" for number, steps in lacking[:_NAMED])
    rest = f" и ещё {len(lacking) - _NAMED}" if len(lacking) > _NAMED else ""
    return f"не готовы {len(lacking)} из {total}: {named}{rest}"


def not_ready_text(found: Sequence[Material]) -> str:
    """Строка lacking_text по материалам."""
    return lacking_text([(one.number(), one.missing) for one in found if one.missing], len(found))


def materials(conn, article_ids: Sequence[int], signal_ids: Sequence[int]) -> list[Material]:
    """Материалы выпуска по номерам статей и сигналов (статьи первыми, в порядке номеров) с тем, чего им не хватает."""
    articles = [int(one) for one in article_ids]
    by_article = exchange_store.feed_signals(conn, articles)
    members = [("article", one, by_article.get(one)) for one in articles]
    members += [("signal", int(one), int(one)) for one in signal_ids]
    roots = exchange_store.roots(conn, [signal for _, _, signal in members if signal is not None])
    states = material_readiness_store.signal_states(conn, set(roots.values()))
    orphans = material_readiness_store.article_states(
        conn, [number for kind, number, signal in members if kind == "article" and signal is None])
    found = []
    for kind, number, signal in members:
        root = roots.get(signal) if signal is not None else None
        state = states.get(root) if root is not None else None
        article = orphans.get(number) if signal is None else None
        found.append(Material(kind, number, signal, root, missing(state, article), dict(state or article or {})))
    return found


def month_materials(conn, month: str) -> list[Material]:
    """Материалы выпуска месяца: состав, как у PDF, выгрузки и пакета выпуска (repository.issue_composition)."""
    composition = repository.issue_composition(month, conn=conn)
    return materials(conn, composition["article_ids"], composition["signal_ids"])


def month_summary(month: str) -> dict[str, int]:
    """Сколько материалов в выпуске месяца и сколько из них не готовы (верх экрана «Дайджест»)."""
    with connection.get_connection() as conn:
        found = month_materials(conn, month)
    return {"materials": len(found), "not_ready": sum(1 for one in found if one.missing)}


def attach(sections: list[dict], month: str | None) -> dict[str, int] | None:
    """Экран «Дайджест»: каждому материалу разделов выпуска (digest_materials.sections) поле readiness, чего ему не
    хватает до готовности; месяцу, сколько его материалов не готовы (состав выпуска, а не то, что оставили фильтры
    экрана). Без месяца числа нет."""
    shown = [material for section in sections for material in section["materials"]]
    found = lacking(
        [one["article_id"] for one in shown if one["item_type"] == "article" and one.get("article_id")],
        [one["signal_id"] for one in shown if one["item_type"] == "signal" and one.get("signal_id")],
    ) if shown else {}
    for material in shown:
        number = material["article_id"] if material["item_type"] == "article" else material["signal_id"]
        material["readiness"] = found.get((material["item_type"], number), [])
    return month_summary(month) if month else None


def lacking(article_ids: Sequence[int], signal_ids: Sequence[int]) -> dict[tuple[str, int], list[str]]:
    """Чего не хватает каждому материалу: («article» | «signal», номер) → шаги (экран «Дайджест»)."""
    with connection.get_connection() as conn:
        found = materials(conn, article_ids, signal_ids)
    return {(one.kind, one.id): list(one.missing) for one in found}


# --- Постановка недостающих шагов --------------------------------------------------------------------------------


@dataclass
class Enqueued:
    """Что поставлено: номера задач, номера материалов по шагам (у обработки ленты номера статей, у прочих шагов
    номера сигналов), статьи, чей сигнал ленты мост догнал без модели, и что не поставлено и почему."""

    jobs: list[int] = field(default_factory=list)
    steps: dict[str, list[int]] = field(default_factory=dict)
    resynced: list[int] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass
class _Plan:
    texts: dict[int, list[str]] = field(default_factory=dict)
    resync: list[int] = field(default_factory=list)
    summary: list[int] = field(default_factory=list)
    card: list[int] = field(default_factory=list)
    scores: list[int] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _text_step(one: Material, plan: _Plan) -> bool:
    """Суть и перевод: у статьи (и сигнала ленты) обработкой ленты по номеру, у находки и материала «Сутью и
    переводом»; у статьи с текстом, которого нет в сигнале, мостом без модели. True: текст будет сразу (мост
    переносит его до постановки следующих шагов)."""
    state = one.state
    if one.root is not None:
        article_id = state.get("article_id")
    elif one.kind == "article":
        article_id = one.id
    else:
        plan.skipped.append(f"{one.number()}: сигнала нет")
        return False
    if article_id is not None:
        if not state.get("article_accepted"):
            plan.skipped.append(f"{one.number()}: статья не принята, суть и перевод ей не пишутся")
            return False
        stages = [stage for stage, column in _ARTICLE_STAGES.items() if not state.get(column)]
        if stages:
            plan.texts[int(article_id)] = stages
            return False
        plan.resync.append(int(article_id))
        return True
    if state.get("summary_path") and signal_summary_store.missing(state):
        plan.summary.append(int(one.root))
    else:
        plan.skipped.append(f"{one.number()}: суть и перевод этому сигналу «Суть и перевод» не пишет")
    return False


def _plan(found: Sequence[Material]) -> _Plan:
    """Шаги, которым уже есть из чего считать: суть и перевод сразу; «Карточка» после «Отбора» после сути (решённому до
    «Отбора» суть не нужна, как у шага «Карточки»: signal_card_store._READY_SQL); баллы после «Карточки» и сути (их вход).
    Следующие поставит after_step, когда запишется итог предыдущего шага."""
    plan = _Plan()
    for one in found:
        has_summary = SUMMARY not in one.missing
        if not has_summary or TRANSLATION in one.missing:
            has_summary = _text_step(one, plan) or has_summary
        if one.root is None:
            continue
        if CARD in one.missing:
            if not one.state.get("card_path"):
                plan.skipped.append(f"{one.number()}: «Карточка» не берёт сигнал без принятого решения «Отбора»")
            elif has_summary or one.state.get("otbor_decision") is None:
                plan.card.append(int(one.root))
        elif SCORES in one.missing and has_summary:
            plan.scores.append(int(one.root))
    return plan


def _drop_taken(conn, plan: _Plan, *, capped: bool) -> None:
    """Убрать из плана шаги, задача которых идёт; с capped ещё и исчерпавшие суточный предел шага."""

    def keep(kind: str, ids: Sequence[int]) -> set[int]:
        taken = material_readiness_store.pending(conn, kind, ids)
        if capped:
            tries = material_readiness_store.attempts(conn, kind, ids)
            taken |= {one for one in ids if tries.get(one, 0) >= DAILY_ATTEMPTS[kind]}
        return set(ids) - taken

    texts = keep("process_articles", list(plan.texts))
    plan.texts = {one: stages for one, stages in plan.texts.items() if one in texts}
    for name, kind in (("summary", "signal_summary"), ("card", "signal_card"), ("scores", "signal_scores")):
        kept = keep(kind, getattr(plan, name))
        setattr(plan, name, sorted(set(getattr(plan, name)) & kept))


def enqueue_missing(found: Sequence[Material], *, trigger: str, capped: bool) -> Enqueued:
    """Поставить неготовым материалам недостающие шаги, которым уже есть из чего считать, по полосе пересчётов. trigger:
    повод в задаче (MARK, RECHECK); capped: не больше суточного предела шага (перепроверка и продолжение цепочки),
    отметка предела не считает."""
    done = Enqueued()
    todo = [one for one in found if one.missing]
    if not todo:
        return done
    plan = _plan(todo)
    with connection.get_connection() as conn:
        # Проверка идущих задач и постановка под одним замком: двойное нажатие «В дайджест» (два запроса сразу) иначе не
        # увидело бы задач друг друга и оплатило бы шаг дважды. Замок держит транзакция этого соединения; задачи ставятся
        # своими соединениями, и следующий, дождавшись замка, их уже видит.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
        _drop_taken(conn, plan, capped=capped)
        _create(plan, trigger=trigger, done=done)
    return done


def _create(plan: _Plan, *, trigger: str, done: Enqueued) -> None:
    """Задачи плана в полосу пересчётов: обработка ленты и «Суть и перевод», «Карточка», баллы."""
    from oiltech_digest import network_policy, signal_card, signal_scores, signal_summary
    from oiltech_digest.db import feed_signals
    from oiltech_digest.processing import external_ai, fulltext_recompute

    done.skipped += plan.skipped
    if plan.resync:
        feed_signals.sync_quietly(plan.resync)
        done.resynced = sorted(plan.resync)
    if plan.texts:
        route = network_policy.route_ai_bulk()
        if route.execution_region != "external":
            # Местный конвейер пометки only не знает: суть и перевод статье даёт обработка ленты дня.
            done.skipped.append("ИИ не во внешнем контуре: суть и перевод статьям даёт обработка ленты дня")
        else:
            groups: dict[tuple[str, ...], list[int]] = {}
            for article_id, stages in sorted(plan.texts.items()):
                groups.setdefault(tuple(stages), []).append(article_id)
            batch = fulltext_recompute.BATCH_SIZE
            for stages, ids in groups.items():
                for start in range(0, len(ids), batch):
                    chunk = ids[start:start + batch]
                    job = repository.create_background_job(
                        "process_articles",
                        {"article_ids": chunk, "limit": len(chunk), "offline": False, "only": list(stages),
                         external_ai.READINESS_FLAG: True, "trigger": trigger},
                        queue_name=route.queue_name, execution_region=route.execution_region,
                        capability=route.capability,
                    )
                    done.jobs.append(int(job["id"]))
            done.steps["process_articles"] = sorted(plan.texts)
    if plan.summary:
        done.jobs += [int(job["id"]) for job in signal_summary.enqueue(plan.summary, trigger=trigger, bulk=True)]
        done.steps["signal_summary"] = plan.summary
    if plan.card:
        try:
            done.jobs += [int(job["id"]) for job in signal_card.enqueue_cards(plan.card, trigger=trigger, bulk=True)]
            done.steps["signal_card"] = plan.card
        except signal_card.CardSetupError as error:
            done.skipped.append(f"«Карточка» не поставлена: {error}")
    if plan.scores:
        try:
            done.jobs += [int(job["id"]) for job in signal_scores.enqueue(plan.scores, trigger=trigger, bulk=True)]
            done.steps["signal_scores"] = plan.scores
        except signal_scores.ScoresSetupError as error:
            done.skipped.append(f"баллы не поставлены: {error}")


def after_digest_mark(*, article_id: int | None = None, signal_id: int | None = None) -> None:
    """Отметка «в дайджест» сама повод для прогона: материалу недостающие шаги без окна шагов планировщика и без
    суточного предела. Сбой постановки отметку не роняет: он в журнале приложения, шаги догонит перепроверка."""
    try:
        with connection.get_connection() as conn:
            found = materials(conn, [article_id] if article_id is not None else [],
                              [signal_id] if signal_id is not None else [])
        done = enqueue_missing(found, trigger=MARK, capped=False)
    except Exception:  # noqa: BLE001 - не дать отметить хуже, чем не поставить шаг
        logger.exception("готовность: шаги по отметке «в дайджест» не поставлены article_id=%s signal_id=%s",
                         article_id, signal_id)
        return
    if done.jobs or done.skipped:
        logger.info("готовность: отметка «в дайджест» article_id=%s signal_id=%s: задачи %s, шаги %s, не поставлено: %s",
                    article_id, signal_id, done.jobs, done.steps, done.skipped)


def after_step(job: dict[str, Any]) -> None:
    """Итог задачи шага конвейера записан (у внешней задачи в записи итога ядром, у местной после исполнения): её
    материалам, которые всё ещё отмечены «в дайджест», следующий шаг, которому теперь есть из чего считать. Повод
    задачи готовности наследуется, у потока дня это отметка. Не больше суточного предела шага: шаг, упавший у материала,
    по кругу не ставится, его догонит перепроверка. Задача ещё идёт (пишет итог), поэтому её собственный шаг второй раз
    не ставится. Сбой постановки запись итога не роняет."""
    if job.get("kind") not in CHAIN_KINDS:
        return
    payload = job.get("payload_json") or {}
    trigger = payload.get("trigger") if payload.get("trigger") in (MARK, RECHECK) else MARK
    try:
        with connection.get_connection() as conn:
            if job["kind"] == "process_articles":
                ids = payload.get("reserved_article_ids") or payload.get("article_ids") or []
                articles, _ = material_readiness_store.marked(conn, ids, [])
                found = materials(conn, sorted(articles), [])
            else:
                _, signals = material_readiness_store.marked(conn, [], payload.get("signal_ids") or [])
                found = materials(conn, [], sorted(signals))
        done = enqueue_missing(found, trigger=trigger, capped=True)
    except Exception:  # noqa: BLE001 - итог задачи уже записан, следующий шаг догонит перепроверка
        logger.exception("готовность: следующий шаг после задачи %s не поставлен", job.get("id"))
        return
    if done.jobs:
        logger.info("готовность: после задачи %s (%s) следующие шаги: задачи %s, шаги %s", job.get("id"),
                    job.get("kind"), done.jobs, done.steps)


# --- Перепроверка готовности -------------------------------------------------------------------------------------


def closing_month(moment: datetime) -> str | None:
    """Месяц, у которого сегодня последний день перед закрытием (FEED_ROLLOVER_DAY-го следующего месяца: на проде 10-е),
    если час по Москве не раньше READINESS_RECHECK_HOUR; иначе None."""
    local = moment.astimezone(feed_window.MSK)
    tomorrow = local.date() + timedelta(days=1)
    if tomorrow.day != config.FEED_ROLLOVER_DAY or local.hour < config.READINESS_RECHECK_HOUR:
        return None
    return feed_window.month_key((tomorrow.replace(day=1) - timedelta(days=1)).replace(day=1))


@dataclass
class Recheck:
    month: str
    materials: list[Material]
    enqueued: Enqueued


def recheck_step(moment: datetime, *, waiting: Sequence[str] = ()) -> list[Recheck]:
    """Шаг планировщика «перепроверка готовности»: месяц, который закрывается завтра (с часа из настройки), и закрытые
    месяцы, чей выпуск ещё ждёт сборки (waiting: их называет шаг выпуска для потребителя,
    exchange_packages.pending_release_months): неготовым материалам заново недостающие шаги, не больше суточного
    предела шага."""
    months = [closing] if (closing := closing_month(moment)) else []
    months += [one for one in waiting if one not in months]
    checked = []
    for month in months:
        with connection.get_connection() as conn:
            found = month_materials(conn, month)
        checked.append(Recheck(month, found, enqueue_missing(found, trigger=RECHECK, capped=True)))
    return checked


def describe(found: Sequence[Material], titles: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Неготовые материалы для вкладки «Журнал обмена»: номер карточки (по нему ссылка на витрину), вид и номер
    материала в выпуске, заголовок и категория карточки, чего не хватает."""
    return [{"signal_id": one.root, "kind": one.kind, "id": one.id,
             **titles.get(int(one.root) if one.root is not None else -1, {"title": None, "category": None}),
             "missing": list(one.missing)} for one in found if one.missing]
