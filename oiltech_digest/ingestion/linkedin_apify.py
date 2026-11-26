"""Сбор страниц LinkedIn через внешний сервис Apify.

Посты страницы компании и профиля эксперта отдают готовые модули сервиса (`ACTORS`); ядро зовёт их само, ключ
APIFY_TOKEN только в его окружении и только в заголовке запроса. Один запуск на страницу: у каждой страницы своя
граница, и сервис отдаёт только посты новее неё (`postedLimitDate` точен до секунды: пост за секунду до границы
приходит, за секунду после нет). Граница:
поздняя из даты начала сбора источника и времени последнего виденного поста плюс секунда, поэтому один пост не
оплачивается дважды, а известный адрес статьи второй раз не вставляется.

Деньги. Страница собирается не чаще раза в LINKEDIN_MIN_INTERVAL_HOURS: страница без новых постов тоже стоит денег.
Потолок запуска сервис держит сам (`maxTotalChargeUsd`), месячный считает шаг: сумма запусков стадии `linkedin_apify` в
учёте расхода с первого числа по Москве. Остатка месяца не хватает на запуск: сбор стоит до следующего месяца. Списание
сервис досчитывает с задержкой (десятки секунд после конца запуска), поэтому клиент ждёт итоговое списание, а не
берёт первое.

Сбой сервиса (ключ, лимит, упавший запуск): строка расхода с ошибкой, время сбора страницы не сдвигается (повтор
следующим циклом), остальные страницы идут дальше. Посты идут теми же рубежами, что посты Telegram: известный адрес,
дата начала сбора, предфильтр, вставка.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as day_start, timedelta, timezone
import logging
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

from oiltech_digest import config
from oiltech_digest.db import linkedin_store, repository
from oiltech_digest.ingestion import dates, linkedin_posts, post_images, verdicts
from oiltech_digest.ingestion.relevance_filter import should_keep_article

logger = logging.getLogger(__name__)

API = "https://api.apify.com/v2"
ACTORS = {
    linkedin_posts.COMPANY: "harvestapi~linkedin-company-posts",
    linkedin_posts.PROFILE: "harvestapi~linkedin-profile-posts",
}
STAGE = "linkedin_apify"
MSK = ZoneInfo("Europe/Moscow")
# Цены сервиса на бесплатном тарифе, $: пост, запуск без постов, начало запуска. Только для оценки, если сервис не
# успел досчитать списание; в учёт идёт сумма сервиса.
POST_USD, EMPTY_USD, START_USD = 0.002, 0.001, 0.00005
_FINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"})
_FIRST_DAYS = 14  # граница страницы без даты начала сбора и без виденных постов


class ApifyError(RuntimeError):
    """Сервис ответил не так: код ошибки, не JSON, нет нужных полей."""


@dataclass(frozen=True)
class RunResult:
    ok: bool
    items: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    error: str | None = None


class ApifyClient:
    """Запуск модуля сервиса, его посты и итоговое списание."""

    def __init__(self, token: str, *, session: Any = None, poll_seconds: float = 5.0, run_timeout: float = 300.0,
                 settle_timeout: float = 90.0, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._token = token
        self._session = session or requests.Session()
        self._poll, self._run_timeout, self._settle_timeout = poll_seconds, run_timeout, settle_timeout
        self._sleep, self._clock = sleep, clock

    def run(self, actor: str, run_input: dict[str, Any], max_charge_usd: float) -> RunResult:
        run_id = None
        try:
            run = self._call("POST", f"/acts/{actor}/runs", params={"maxTotalChargeUsd": f"{max_charge_usd:.4f}"},
                             json=run_input)
            run_id = run.get("id") if isinstance(run, dict) else None
            if not run_id:
                raise ApifyError("сервис не дал номер запуска")
            run = self._finish(run_id, run)
            if run.get("status") != "SUCCEEDED":
                return RunResult(False, cost_usd=self._cost(run_id, run, None),
                                 error=f"запуск сервиса кончился статусом {run.get('status')}")
            items = self._call("GET", f"/datasets/{run['defaultDatasetId']}/items",
                               params={"format": "json", "clean": "true"})
            if not isinstance(items, list):
                raise ApifyError("сервис отдал не список постов")
            posts = [item for item in items if isinstance(item, dict)]
            return RunResult(True, posts, self._cost(run_id, run, len(posts)))
        except (requests.RequestException, ApifyError, ValueError, KeyError, TypeError) as exc:
            cost = 0.0
            if run_id:
                try:
                    cost = self._cost(run_id, self._call("GET", f"/actor-runs/{run_id}"), None)
                except (requests.RequestException, ApifyError, ValueError, KeyError, TypeError):
                    cost = START_USD
            return RunResult(False, cost_usd=cost, error=f"{type(exc).__name__}: {str(exc)[:200]}")

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._session.request(method, API + path, headers={"Authorization": f"Bearer {self._token}"},
                                         timeout=60, **kwargs)
        if response.status_code >= 400:
            raise ApifyError(f"{method} {path}: HTTP {response.status_code} {_error_type(response)}".strip())
        body = response.json()
        return body["data"] if isinstance(body, dict) and "data" in body else body

    def _finish(self, run_id: str, run: dict[str, Any]) -> dict[str, Any]:
        """Дождаться конца запуска; не кончился за run_timeout: остановить его."""
        deadline = self._clock() + self._run_timeout
        while run.get("status") not in _FINAL:
            if self._clock() >= deadline:
                self._call("POST", f"/actor-runs/{run_id}/abort")
                return {**run, "status": "TIMED-OUT"}
            self._sleep(self._poll)
            run = self._call("GET", f"/actor-runs/{run_id}")
        return run

    def _cost(self, run_id: str, run: dict[str, Any], posts: int | None) -> float:
        """Итоговое списание запуска. Сервис досчитывает его после конца запуска: ждём, пока счёт постов (у пустого
        запуска: «запуск без постов») не сойдётся с полученными постами; не сошёлся за settle_timeout: большее из
        списания и оценки по ценам."""
        deadline = self._clock() + self._settle_timeout
        while True:
            events = run.get("chargedEventCounts") or {}
            usage = float(run.get("usageTotalUsd") or 0)
            if posts is None:
                return usage
            settled = int(events.get("post") or 0) >= posts and (posts > 0 or int(events.get("no-result") or 0) >= 1)
            if settled:
                return usage
            if self._clock() >= deadline:
                return max(usage, posts * POST_USD + (EMPTY_USD if posts == 0 else 0.0) + START_USD)
            self._sleep(self._poll)
            run = self._call("GET", f"/actor-runs/{run_id}")


def _error_type(response: Any) -> str:
    try:
        error = response.json().get("error") or {}
    except ValueError:
        return ""
    return str(error.get("type") or "")[:80] if isinstance(error, dict) else ""


def month_start(now: datetime) -> datetime:
    """Первое число месяца по Москве: с него считается месячный потолок."""
    return datetime.combine(now.astimezone(MSK).date().replace(day=1), day_start(), MSK)


def boundary(source: dict[str, Any], now: datetime) -> datetime:
    """С какого момента спрашивать посты страницы: поздняя из даты начала сбора и последнего виденного поста плюс
    секунда; нет ни того, ни другого: две недели назад."""
    bounds = [verdicts.old_cutoff(source, None)]
    seen = dates.as_utc(source.get("last_seen_published_at"))
    if seen is not None:
        bounds.append(seen + timedelta(seconds=1))
    known = [bound for bound in bounds if bound is not None]
    return max(known) if known else now - timedelta(days=_FIRST_DAYS)


def due(source: dict[str, Any], now: datetime) -> bool:
    last = dates.as_utc(source.get("last_parsed_at"))
    return last is None or now - last >= timedelta(hours=config.LINKEDIN_MIN_INTERVAL_HOURS)


def run_input(page: linkedin_posts.Page, since: datetime) -> dict[str, Any]:
    return {
        "targetUrls": [page.url],
        "maxPosts": config.LINKEDIN_APIFY_MAX_POSTS,
        "postedLimitDate": since.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "includeReposts": True,
        "includeQuotePosts": True,
        "scrapeReactions": False,
        "scrapeComments": False,
    }


def collect(*, source_id: int | None = None, force: bool = False, dry_run: bool = False,
            now: datetime | None = None, client: Any = None,
            save_image: Callable[[str | None], str | None] = post_images.save) -> dict[str, Any]:
    """Собрать посты страниц LinkedIn, которым пора. force: без флага и без интервала (ручной запуск), потолки
    расхода действуют всегда. dry_run: только план, без сервиса и без записи."""
    now = now or datetime.now(timezone.utc)
    stats: dict[str, Any] = {"pages": 0, "runs": 0, "posts": 0, "added": 0, "known": 0, "old": 0, "prefilter": 0,
                             "duplicates": 0, "images": 0, "failed": 0, "cost_usd": 0.0, "stopped": None, "plan": []}
    spent = linkedin_store.spent_since(STAGE, month_start(now))
    stats["month_spend_usd"] = round(spent, 6)
    if not (force or dry_run) and not config.LINKEDIN_APIFY_ENABLED:
        stats["stopped"] = "сбор LinkedIn выключен (LINKEDIN_APIFY_ENABLED=0)"
        return stats
    if not dry_run and client is None and not config.APIFY_TOKEN:
        stats["stopped"] = "нет ключа сервиса (APIFY_TOKEN)"
        return stats
    sources = linkedin_store.active_sources()
    if source_id is not None:
        sources = [source for source in sources if int(source["id"]) == int(source_id)]
        if not sources:
            stats["stopped"] = f"источник {source_id} не страница LinkedIn, выключен или в архиве"
            return stats
    ready = [source for source in sources if force or due(source, now)]
    ready.sort(key=lambda source: (dates.as_utc(source.get("last_parsed_at")) or datetime.min.replace(tzinfo=timezone.utc),
                                   int(source["id"])))
    client = client or ApifyClient(config.APIFY_TOKEN)
    for source in ready:
        page = linkedin_posts.page_from_url(source.get("url"))
        if page is None:
            logger.warning("LinkedIn %s: адрес %r не страница LinkedIn", source.get("name"), source.get("url"))
            stats["failed"] += 1
            continue
        remaining = config.LINKEDIN_APIFY_MONTH_MAX_USD - spent
        if remaining < POST_USD + START_USD:
            stats["stopped"] = (f"месячный потолок ${config.LINKEDIN_APIFY_MONTH_MAX_USD:.2f}: израсходовано ${spent:.2f},"
                                " сбор LinkedIn стоит до следующего месяца")
            logger.warning("LinkedIn: %s", stats["stopped"])
            break
        cap = min(config.LINKEDIN_APIFY_RUN_MAX_USD, remaining)
        request = run_input(page, boundary(source, now))
        stats["plan"].append({"source_id": int(source["id"]), "name": source.get("name"), "kind": page.kind,
                              "since": request["postedLimitDate"], "cap_usd": round(cap, 4)})
        if dry_run:
            continue
        actor = ACTORS[page.kind]
        result = client.run(actor, request, cap)
        linkedin_store.record_run(stage=STAGE, model=actor.replace("~", "/"), cost_usd=result.cost_usd, ok=result.ok,
                                  error=result.error)
        spent += result.cost_usd
        stats["runs"] += 1
        stats["cost_usd"] = round(stats["cost_usd"] + result.cost_usd, 6)
        if not result.ok:
            logger.warning("LinkedIn %s: %s", source.get("name"), result.error)
            stats["failed"] += 1
            continue
        try:
            page_stats = store_posts(source, result.items, save_image=save_image)
        except Exception as exc:  # noqa: BLE001 - сбой записи одной страницы не валит остальные
            logger.exception("LinkedIn %s: посты не записаны: %s", source.get("name"), exc)
            stats["failed"] += 1
            continue
        stats["pages"] += 1
        for key, value in page_stats.items():
            stats[key] += value
    stats["month_spend_usd"] = round(spent, 6)
    return stats


def store_posts(source: dict[str, Any], items: list[dict[str, Any]], *,
                save_image: Callable[[str | None], str | None] = post_images.save) -> dict[str, int]:
    """Посты одного запуска в статьи источника; затем время сбора и последний виденный пост страницы."""
    counts = {"posts": 0, "added": 0, "known": 0, "old": 0, "prefilter": 0, "duplicates": 0, "images": 0}
    cutoff = verdicts.old_cutoff(source, None)
    newest: linkedin_posts.Post | None = None
    for item in items:
        post = linkedin_posts.parse_post(item)
        if post is None:
            continue
        counts["posts"] += 1
        if post.seen_at is not None and (newest is None or newest.seen_at is None or post.seen_at > newest.seen_at):
            newest = post
        if repository.article_exists(post.url):
            counts["known"] += 1
            continue
        if cutoff is not None and post.published_at is not None and post.published_at < cutoff:
            counts["old"] += 1
            continue
        if not should_keep_article(post.title, post.text, source).keep:
            counts["prefilter"] += 1
            continue
        image_url = _image(post, save_image)
        counts["images"] += int(image_url is not None)
        if repository.insert_article(post.record(int(source["id"]), image_url)):
            counts["added"] += 1
        else:
            counts["duplicates"] += 1
    repository.touch_last_parsed(int(source["id"]))
    if newest is not None:
        repository.update_source_request_state(int(source["id"]), last_seen_article_url=newest.url,
                                               last_seen_published_at=newest.seen_at)
    return counts


def _image(post: linkedin_posts.Post, save_image: Callable[[str | None], str | None]) -> str | None:
    if not post.image_source:
        return None
    try:
        return save_image(post.image_source)
    except Exception as exc:  # noqa: BLE001 - без картинки статья всё равно нужна
        logger.info("картинка поста %s не сохранена: %s", post.url, exc)
        return None
