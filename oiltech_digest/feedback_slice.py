"""Недельный срез ОС (#189, спека #187): раз в неделю по настройке (понедельник, 09:00 МСК) и по кнопке «собрать
сейчас» разбор ОС берёт отметки с прошлого среза.

Окно среза: с конца окна прошлого выполненного среза (у первого: со строки seed, выкат #188) по сейчас; срез по
расписанию не идёт дважды за неделю, упавший или отложенный своё окно не закрывает: его отметки возьмёт следующий.
Перед постановкой разбора сверяется потолок расходов контура в месяц: расход стадий контура с начала месяца не меньше
потолка, и срез откладывает разбор до следующего месяца, а администратор видит предупреждение. В первый день среза в
месяце (первый понедельник) в журнале появляется задача: случайные отклонённые «Отбором» публикации по темам на
просмотр эксперту. Каждый шаг заодно переводит в «устарело» предложения без решения дольше срока жизни.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from oiltech_digest import feedback_journal, feedback_review, feedback_settings
from oiltech_digest.db import feedback_store, repository
from oiltech_digest.db.feedback_store import PENDING, SAMPLE_TASK
from oiltech_digest.feed_window import MSK

# Стадии расхода контура ОС под потолком: разбор ОС и автопроверка правил эксперта (#190).
STAGES = (feedback_review.STAGE, "rule_check")
SLICE_AUTHOR = "Недельный срез"
_SAMPLE_POOL_DAYS = 31


class SliceRefused(Exception):
    """Срез не запущен: причина словами для ответа администратору (409)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def scheduler_step(now: datetime | None = None) -> dict[str, Any]:
    """Шаг планировщика: «устарело» у предложений без решения; в день и после часа среза по настройке: срез этой
    недели, если его ещё не было; в первый такой день месяца: задача выборки отклонённого."""
    from oiltech_digest import rule_check

    now = now or _now()
    settings = feedback_settings.current()
    result: dict[str, Any] = {"expired": feedback_journal.expire_stale()}
    local = now.astimezone(MSK)
    due = _due(local, settings)
    if due:
        week = local.date() - timedelta(days=local.isoweekday() - 1)
        if not feedback_store.scheduled_this_week(week):
            result["slice"] = run(trigger="schedule", author=None, now=now, week_start=week, settings=settings)
        if local.day <= 7:
            result["sample"] = sample_task(now, settings)
    # Автопроверки правил эксперта (#190): каждый цикл: предложения без проверки; в первый срез месяца: проверка
    # всего набора правил каждого агента против голой инструкции.
    result["checks"] = rule_check.scheduler_step(now, monthly=due and local.day <= 7)
    return result


def collect_now(*, author: str | None) -> dict[str, Any]:
    """Кнопка «собрать сейчас» у администратора: срез вне расписания, если кнопка включена настройкой."""
    settings = feedback_settings.current()
    if not settings["manual_slice"]:
        raise SliceRefused("кнопка «собрать сейчас» выключена в настройках контура")
    return run(trigger="manual", author=author, now=_now(), settings=settings)


def run(*, trigger: str, author: str | None, now: datetime, settings: dict[str, Any],
        week_start: date | None = None) -> dict[str, Any]:
    """Срез: окно с прошлого выполненного среза по часам базы, сигналы с новыми отметками, потолок расходов, задача
    разбора ОС. now: часы расписания (день недели, месяц потолка)."""
    running = feedback_store.running_slice()
    if running is not None:
        if trigger == "manual":
            raise SliceRefused(f"срез №{running['id']} ещё идёт")
        return {"skipped": f"срез №{running['id']} ещё идёт"}
    days = int(settings["similar_window_days"])
    window_to = feedback_store.db_now()
    window_from = feedback_store.last_done_window_end() or window_to - timedelta(days=days)
    signal_ids = feedback_store.signals_with_new_marks(window_from, window_to)
    common = {"trigger": trigger, "window_from": window_from, "window_to": window_to, "week_start": week_start,
              "author": author}
    if not signal_ids:
        slice_id = feedback_store.create_slice(status="done", note="новых отметок нет", **common)
        return {"slice_id": slice_id, "status": "done", "signals": 0}
    spent, ceiling = month_spend(now), float(settings["monthly_ceiling_usd"])
    if spent >= ceiling:
        note = (f"потолок расходов контура: ${spent:.2f} из ${ceiling:.2f} в этом месяце; разбор отложен до "
                "следующего месяца")
        slice_id = feedback_store.create_slice(status="postponed", signals=len(signal_ids), note=note, **common)
        return {"slice_id": slice_id, "status": "postponed", "signals": len(signal_ids), "note": note}
    slice_id = feedback_store.create_slice(status="running", signals=len(signal_ids), **common)
    if slice_id is None:
        return {"skipped": "срез этой недели уже есть"}
    try:
        job = feedback_review.enqueue(slice_id, signal_ids, since=window_to - timedelta(days=days), until=window_to,
                                      model=str(settings["review_model"]), window_from=window_from)
    except Exception as exc:
        # Срез без задачи не держит следующие: не выполнен, его окно возьмёт следующий.
        feedback_store.finish_slice(slice_id, status="failed", note=f"задача разбора ОС не поставлена: {exc}"[:500])
        raise
    feedback_store.attach_job(slice_id, int(job["id"]))
    return {"slice_id": slice_id, "status": "running", "signals": len(signal_ids), "job_id": int(job["id"])}


def overview(*, is_admin: bool, now: datetime | None = None) -> dict[str, Any]:
    """Состояние контура для вкладки: последние срезы, следующий по расписанию, расход и потолок, предупреждение."""
    now = now or _now()
    settings = feedback_settings.current()
    slices = feedback_store.slices(limit=5)
    spent, ceiling = month_spend(now), float(settings["monthly_ceiling_usd"])
    last = slices[0] if slices else None
    warning = None
    if spent >= ceiling or (last is not None and last["status"] == "postponed"):
        warning = (f"Потолок расходов контура ОС: ${spent:.2f} из ${ceiling:.2f} в этом месяце. Разбор ОС отложен до "
                   "следующего месяца: поднимите потолок в настройках или дождитесь нового месяца.")
    return {
        "slices": [_slice(row) for row in slices],
        "running": any(row["status"] == "running" for row in slices),
        "next_slice_at": next_slice_at(now, settings).isoformat(),
        "month_spend_usd": round(spent, 2), "ceiling_usd": ceiling,
        "warning": warning if is_admin else None,
        "can_collect": is_admin and bool(settings["manual_slice"]),
        "can_configure": is_admin,
    }


def month_spend(now: datetime) -> float:
    local = now.astimezone(MSK)
    return feedback_store.month_spend(STAGES, datetime.combine(local.date().replace(day=1), time(), MSK))


def next_slice_at(now: datetime, settings: dict[str, Any]) -> datetime:
    """Ближайший срез по расписанию (МСК): день и время из настроек. Час этой недели прошёл: через неделю, если срез
    этой недели уже был или его день кончился; иначе он пойдёт на ближайшем цикле планировщика."""
    local = now.astimezone(MSK)
    hour, minute = (int(part) for part in str(settings["slice_time"]).split(":"))
    week = local.date() - timedelta(days=local.isoweekday() - 1)
    moment = datetime.combine(week + timedelta(days=int(settings["slice_weekday"]) - 1), time(hour, minute), MSK)
    if moment > local:
        return moment
    if feedback_store.scheduled_this_week(week) or local.date() != moment.date():
        return moment + timedelta(days=7)
    return local


def sample_task(now: datetime, settings: dict[str, Any]) -> dict[str, Any] | None:
    """Задача выборки на просмотр эксперту: случайные отклонённые «Отбором» за месяц публикации, поровну по темам. Раз
    в месяц; у публикации ленты и материала темы нет (до «Карточки» они не дошли): тема по ключам."""
    from oiltech_digest import signal_themes
    from oiltech_digest.signal_card_pack import _round_robin

    size = int(settings["rejected_sample_size"])
    local = now.astimezone(MSK)
    month = local.strftime("%Y-%m")
    if size <= 0 or feedback_store.task_in_month(SAMPLE_TASK, month):
        return None
    pool = feedback_store.rejected_by_otbor(now - timedelta(days=_SAMPLE_POOL_DAYS), seed=month)
    tags = repository.list_enabled_tags()
    themes = set(signal_themes.names(tags))
    items = []
    for row in pool:
        theme = row["theme"] if row["theme"] in themes else next(iter(signal_themes.candidates(
            f"{row['original_title'] or ''} {row['summary'] or ''}", tags)), "без темы")
        items.append({**row, "theme": theme})
    chosen = _round_robin(items, size, int(local.strftime("%Y%m")))
    publications = [{"signal_id": int(row["id"]), "title": row["title"], "url": row["url"], "theme": row["theme"],
                     "reason": row["verdict_reason"] or row["reject_reason"], "origin": row["origin"]}
                    for row in chosen]
    if not publications:
        return None
    action_id = feedback_store.create_action(
        SAMPLE_TASK, PENDING, agents=["otbor"], author=SLICE_AUTHOR,
        details={"publications": publications, "month": month, "pool": len(pool)})
    return {"action_id": action_id, "publications": len(publications), "pool": len(pool)}


def _due(local: datetime, settings: dict[str, Any]) -> bool:
    hour, minute = (int(part) for part in str(settings["slice_time"]).split(":"))
    return local.isoweekday() == int(settings["slice_weekday"]) and (local.hour, local.minute) >= (hour, minute)


def _slice(row: dict[str, Any]) -> dict[str, Any]:
    return {key: (value.isoformat() if hasattr(value, "isoformat") else value) for key, value in row.items()
            if key in ("id", "trigger", "status", "window_from", "window_to", "signals", "proposals", "note",
                       "author", "created_at", "finished_at")}
