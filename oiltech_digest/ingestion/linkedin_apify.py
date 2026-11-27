"""Сбор страниц LinkedIn через внешний сервис Apify.

Посты страницы компании и профиля эксперта отдают готовые модули сервиса (`ACTORS`); ядро зовёт их само, ключ
APIFY_TOKEN только в его окружении и только в заголовке запроса. Один запуск на страницу: у каждой страницы своя
граница, и сервис отдаёт только посты новее неё (`postedLimitDate` точен до секунды: пост за секунду до границы
приходит, за секунду после нет). Граница: поздняя из даты начала сбора источника и времени последнего виденного поста
плюс секунда; нет ни того, ни другого: без границы, последние LINKEDIN_APIFY_MAX_POSTS постов. Поэтому один пост не
оплачивается дважды, а известный адрес статьи второй раз не вставляется.

Деньги. Страница собирается не чаще раза в LINKEDIN_MIN_INTERVAL_HOURS: страница без новых постов тоже стоит денег.
Потолок запуска сервис держит сам (`maxTotalChargeUsd`), месячный считает шаг: сумма запусков стадии `linkedin_apify` в
учёте расхода с первого числа по Москве. Остатка месяца не хватает на запуск: сбор стоит до следующего месяца. Сумму
запуска сервис досчитывает с задержкой, причём счётчики событий обновляются раньше суммы; поэтому в учёт идёт большее
из суммы сервиса и счёта по ценам.

Сбой сервиса (ключ, лимит, упавший запуск): строка расхода с ошибкой, время сбора страницы не сдвигается (повтор
следующим циклом), остальные страницы идут дальше. Оплаченный запуск сдвигает границу всегда, по времени каждого
полученного поста, даже нечитаемого или не записанного: иначе следующий цикл оплатил бы те же посты снова. Посты идут
теми же рубежами, что посты Telegram: известный адрес, дата начала сбора, предфильтр, вставка.
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
# Цены сервиса на бесплатном тарифе, $: пост, запуск без постов, начало запуска. По ним счёт запуска: на бесплатном
# тарифе он равен сумме сервиса, а сумма приходит позже счётчиков событий.
POST_USD, EMPTY_USD, START_USD = 0.002, 0.001, 0.00005
_FINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"})
_PAGE_COUNTS = ("posts", "added", "known", "old", "prefilter", "duplicates", "unreadable", "post_errors", "images")


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
                 settle_timeout: float = 30.0, sleep: Callable[[float], None] = time.sleep,
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
            # Сначала маска, потом обрезка: иначе кусок ключа на границе обрезки остался бы открытым.
            return RunResult(False, cost_usd=cost, error=self._masked(f"{type(exc).__name__}: {exc}")[:200])

    def _masked(self, text: str) -> str:
        """Текст ошибки без ключа: исключение requests может процитировать заголовок запроса вместе с ним."""
        return text.replace(self._token, "***") if self._token else text

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
        """Итоговое списание запуска: большее из суммы сервиса и счёта по ценам (посты, запуск без постов, начало).
        Сервис досчитывает сумму после конца запуска, и счётчики событий у него обновляются раньше суммы: верные
        счётчики при отстающей сумме бывают у большинства запусков. Поэтому ждём, пока сумма не дорастёт до счёта,
        не дольше settle_timeout: страницы собираются подряд в одном шаге цикла планировщика."""
        deadline = self._clock() + self._settle_timeout
        while True:
            usage = float(run.get("usageTotalUsd") or 0)
            if posts is None:
                return usage
            expected = posts * POST_USD + (EMPTY_USD if posts == 0 else 0.0) + START_USD
            if usage >= expected - 1e-9 or self._clock() >= deadline:
                return max(usage, expected)
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


def boundary(source: dict[str, Any]) -> datetime | None:
    """С какого момента спрашивать посты страницы: поздняя из даты начала сбора и последнего виденного поста плюс
    секунда; нет ни того, ни другого: без границы."""
    bounds = [verdicts.old_cutoff(source, None)]
    seen = dates.as_utc(source.get("last_seen_published_at"))
    if seen is not None:
        bounds.append(seen + timedelta(seconds=1))
    known = [bound for bound in bounds if bound is not None]
    return max(known) if known else None


def due(source: dict[str, Any], now: datetime) -> bool:
    last = dates.as_utc(source.get("last_parsed_at"))
    return last is None or now - last >= timedelta(hours=config.LINKEDIN_MIN_INTERVAL_HOURS)


def run_input(page: linkedin_posts.Page, since: datetime | None) -> dict[str, Any]:
    request: dict[str, Any] = {"targetUrls": [page.url], "maxPosts": config.LINKEDIN_APIFY_MAX_POSTS,
                               "includeReposts": True, "includeQuotePosts": True, "scrapeReactions": False,
                               "scrapeComments": False}
    if since is not None:
        stamp = since.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        request["postedLimitDate"] = stamp.replace("+00:00", "Z")
    return request


def collect(*, source_id: int | None = None, dry_run: bool = False, now: datetime | None = None, client: Any = None,
            save_image: Callable[[str | None], str | None] = post_images.save) -> dict[str, Any]:
    """Собрать посты страниц LinkedIn, которым пора. dry_run: только план, без сервиса и без записи, флаг ему не
    нужен. Потолки расхода действуют всегда."""
    now = now or datetime.now(timezone.utc)
    stats: dict[str, Any] = {"pages": 0, "runs": 0, "failed": 0, "cost_usd": 0.0, "stopped": None, "plan": [],
                             **dict.fromkeys(_PAGE_COUNTS, 0)}
    spent = linkedin_store.spent_since(STAGE, month_start(now))
    stats["month_spend_usd"] = round(spent, 6)
    if not dry_run and not config.LINKEDIN_APIFY_ENABLED:
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
    ready = [source for source in sources if due(source, now)]
    ready.sort(key=lambda source: (dates.as_utc(source.get("last_parsed_at")) or datetime.min.replace(tzinfo=timezone.utc),
                                   int(source["id"])))
    client = client or ApifyClient(config.APIFY_TOKEN)
    for source in ready:
        page = linkedin_posts.page_from_url(source.get("url"))
        if page is None:
            logger.warning("LinkedIn #%s: адрес источника не страница LinkedIn", source["id"])
            stats["failed"] += 1
            continue
        remaining = config.LINKEDIN_APIFY_MONTH_MAX_USD - spent
        if remaining < POST_USD + START_USD:
            stats["stopped"] = (f"месячный потолок ${config.LINKEDIN_APIFY_MONTH_MAX_USD:.2f}: израсходовано ${spent:.2f},"
                                " сбор LinkedIn стоит до следующего месяца")
            logger.warning("LinkedIn: %s", stats["stopped"])
            break
        cap = min(config.LINKEDIN_APIFY_RUN_MAX_USD, remaining)
        request = run_input(page, boundary(source))
        stats["plan"].append({"source_id": int(source["id"]), "name": source.get("name"), "kind": page.kind,
                              "since": request.get("postedLimitDate"), "cap_usd": round(cap, 4)})
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
            logger.warning("LinkedIn #%s: %s", source["id"], result.error)
            stats["failed"] += 1
            continue
        try:
            page_stats = store_posts(source, result.items, save_image=save_image)
        except Exception as exc:  # noqa: BLE001 - сбой базы на одной странице не валит остальные
            logger.error("LinkedIn #%s: посты не записаны: %s", source["id"], type(exc).__name__)
            stats["failed"] += 1
            continue
        stats["pages"] += 1
        for key, value in page_stats.items():
            stats[key] += value
    stats["month_spend_usd"] = round(spent, 6)
    return stats


def store_posts(source: dict[str, Any], items: list[dict[str, Any]], *,
                save_image: Callable[[str | None], str | None] = post_images.save) -> dict[str, int]:
    """Посты одного оплаченного запуска в статьи источника. Пост, который не разобрался или не записался, считается и
    не держит остальные; граница страницы сдвигается по времени каждого полученного поста, время сбора ставится
    всегда."""
    counts = dict.fromkeys(_PAGE_COUNTS, 0)
    cutoff = verdicts.old_cutoff(source, None)
    newest_at: datetime | None = None
    newest_url: str | None = None
    for item in items:
        counts["posts"] += 1
        seen = linkedin_posts.seen_at(item)
        if seen is not None and (newest_at is None or seen > newest_at):
            newest_at = seen
            newest_url = linkedin_posts.post_url(item.get("linkedinUrl")) if isinstance(item, dict) else None
        try:
            outcome, image_saved = _store_post(source, item, cutoff, save_image)
        except Exception as exc:  # noqa: BLE001 - за запуск уже заплачено: один пост не держит остальные
            logger.error("LinkedIn #%s: пост не записан: %s", source["id"], type(exc).__name__)
            outcome, image_saved = "post_errors", False
        counts[outcome] += 1
        counts["images"] += int(image_saved)
    repository.touch_last_parsed(int(source["id"]))
    if newest_at is not None:
        repository.update_source_request_state(int(source["id"]), last_seen_article_url=newest_url,
                                               last_seen_published_at=newest_at)
    return counts


def _store_post(source: dict[str, Any], item: dict[str, Any], cutoff: datetime | None,
                save_image: Callable[[str | None], str | None]) -> tuple[str, bool]:
    """Рубежи одного поста и вставка: чем кончилось и сохранена ли картинка."""
    post = linkedin_posts.parse_post(item)
    if post is None:
        return "unreadable", False
    if repository.article_exists(post.url):
        return "known", False
    if cutoff is not None and post.published_at is not None and post.published_at < cutoff:
        return "old", False
    if not should_keep_article(post.title, post.text, source).keep:
        return "prefilter", False
    image_url = _image(post, save_image)
    inserted = repository.insert_article(post.record(int(source["id"]), image_url))
    return ("added" if inserted else "duplicates"), image_url is not None


def _image(post: linkedin_posts.Post, save_image: Callable[[str | None], str | None]) -> str | None:
    if not post.image_source:
        return None
    try:
        return save_image(post.image_source)
    except Exception as exc:  # noqa: BLE001 - без картинки статья всё равно нужна
        logger.info("LinkedIn: картинка поста не сохранена: %s", type(exc).__name__)
        return None
