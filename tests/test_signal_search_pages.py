"""Добор страниц находок радара (поручение владельца 04.11): при «Отборе» находка без текста издателя теряется, поэтому
после запроса с NL идут PDF, сбор на РФ (страницу качает ядро по просьбе воркера) и браузер NL — до дедупа прогона и
«Отбора» (порядок цикла Михаила 04.11). Шов 1 — контур целиком (tests/otbor_radar_support.py, докачка настоящая, сеть
подменена): задача радара → воркер NL → адрес ядра /api/external-worker/page → ядро. Шов 2 — запрос ядра
(publisher_page.download): только публичные адреса, переадресации проверяются, размер ограничен.
"""

import base64
import ipaddress

from fastapi.testclient import TestClient
import pytest

from oiltech_digest import api, external_worker, signal_research
from oiltech_digest.db import repository
from oiltech_digest.ingestion import playwright_parser, publisher_page, source_diagnostics
from oiltech_digest.ingestion.source_diagnostics import ProbeResult
from oiltech_digest.signal_research import PAGE_NOT_FETCHED
from tests.otbor_radar_support import PAGE_TEXT, event, run_radar
from tests.otbor_support import answer, decide
from tests.otbor_support import contour  # noqa: F401 — фикстура
from tests.signal_card_support import admin, api_get, user, worker  # noqa: F401 — фикстуры
from tests.test_document_parsing import make_pdf

PUBLIC_IP = "93.184.216.34"
RU_URL = "https://iadevon.example/news/oilservice/seismic-urals-komi-19341/"
RU_TITLE = "ЛУКОЙЛ-Пермь проведёт 3D-сейсморазведку на четырёх участках в Прикамье"
OTHER_URL = "https://neftegaz.example/news/lukoil-perm-seismic-3d"
OTHER_TITLE = "ЛУКОЙЛ-Пермь запланировала 3D-сейсморазведку на четырёх объектах"
PDF_URL = "https://public-inspection.federalregister.example/2026-22396.pdf"
PDF_TITLE = "PHMSA proposes updated federal safety standards for LNG facilities"
PDF_PAGES = ["The Pipeline and Hazardous Materials Safety Administration proposes to update the federal safety standards "
             "for liquefied natural gas facilities, including siting, design and fire protection requirements.",
             "Comments on the proposed rule are due within sixty days after publication in the Federal Register."]
SCAN_URL = "https://govinfo.example/content/pkg/FR-2026-11-03/scan.pdf"
SCAN_TITLE = "Federal Register issue scanned without a text layer for LNG"
JS_URL = "https://wam.example/en/article/adnoc-gas-etihadwe-gas-supply"
JS_TITLE = "ADNOC Gas and EtihadWE to strengthen gas supply for power generation"
WALL_URL = "https://ir.offshore.example/news-releases/third-quarter-2026-results"
WALL_TITLE = "Transocean reports third quarter 2026 results and backlog"


def html(title, text=PAGE_TEXT):
    paragraphs = "".join(f"<p>{part}.</p>" for part in text.split(". ") if part)
    return (f'<html><head><title>{title}</title><meta property="og:title" content="{title}"></head><body><article>'
            f"<h1>{title}</h1>{paragraphs}</article></body></html>").encode("utf-8")


SHELL = (b"<html><head><title>WAM</title></head><body><div>WAM - Emirates News Agency. This website uses cookies to "
         b"ensure you get the best experience on the website. If you continue to browse, then you agree.</div>"
         b"</body></html>")
BOT_CHECK = ("<html><head><title>Проверяем браузер</title></head><body><p>" + "Подождите, идёт проверка браузера "
             "перед переходом на сайт. Это займёт несколько секунд. " * 4 + "</p></body></html>").encode("utf-8")


class _CoreResponse:
    """Ответ сети ядра — как urllib3 без дочитывания (publisher_page._get)."""

    def __init__(self, status, body=b"", headers=None):
        self.status, self.body, self.headers = status, body, headers or {}

    def stream(self, size, decode_content=False):
        assert decode_content, "ядро читает страницу разжатой"
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]

    def release_conn(self):
        pass


class Net:
    """Сеть теста: что отвечают запрос с NL, сеть ядра РФ и браузер NL по адресу; обращения — в calls. Ответ: байты —
    страница; число — статус отказа; None — сайт молчит; у ядра ("redirect", адрес) — переадресация; у браузера
    список — ответы попыток по очереди."""

    def __init__(self, monkeypatch, *, nl=None, core=None, browser=None, resolve=None):
        self.nl, self.core, self.browser = nl or {}, core or {}, browser or {}
        self.resolve = resolve or {}
        self.calls = []
        self.status = None
        monkeypatch.setattr(source_diagnostics, "probe_url", self._probe)
        monkeypatch.setattr(publisher_page, "_resolve", self._resolve)
        monkeypatch.setattr(publisher_page, "_get", self._core_get)
        monkeypatch.setattr(playwright_parser, "fetch_rendered", self._render)
        monkeypatch.setattr(playwright_parser, "last_fetch_status", lambda: self.status)

    def _resolve(self, host, port):
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            return self.resolve.get(host, [PUBLIC_IP])

    def _probe(self, url, timeout=20):
        self.calls.append(("nl", url))
        answer = self.nl.get(url)
        if isinstance(answer, bytes):
            return ProbeResult(url=url, status=200), answer
        if isinstance(answer, int):
            return ProbeResult(url=url, status=answer), None
        return ProbeResult(url=url, status="ERR", error="ConnectTimeout: молчит"), None

    def _core_get(self, target, *, timeout):
        url = f"{target.scheme}://{target.host_header}{target.path}"
        self.calls.append(("core", url, target.ip))
        answer = self.core.get(url)
        if isinstance(answer, bytes):
            return _CoreResponse(200, answer, {"Content-Type": "text/html"})
        if isinstance(answer, int):
            return _CoreResponse(answer)
        if isinstance(answer, tuple):
            return _CoreResponse(302, headers={"Location": answer[1]})
        raise TimeoutError("ядро не дождалось ответа")

    def _render(self, url, timeout_ms=30_000, wait_until="domcontentloaded", settle_ms=3500):
        self.calls.append(("browser", url, wait_until))
        answer = self.browser.get(url)
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, bytes):
            self.status = "ok:200"
            return answer
        self.status = f"blocked:{answer}" if isinstance(answer, int) else "error:TimeoutError"
        return None

    def steps(self, url):
        return [call[0] for call in self.calls if call[1] == url]


def findings():
    """Заголовки находок, которые ждут «Отбора»."""
    with repository.get_connection() as conn:
        return sorted(row[0] for row in conn.execute("SELECT title FROM signals WHERE selector = 'otbor'"))


def evidence(url):
    """Ссылка находки, как её записало ядро: служебные поля докачки и текст страницы."""
    with repository.get_connection() as conn:
        payload, fact = conn.execute("SELECT raw_payload_json, extracted_fact FROM signal_evidence "
                                     "WHERE source_url = %s", (url,)).fetchone()
    return payload, fact


def fulltext():
    """Счётчики докачки темы в итоге последнего прогона радара, как его записало ядро."""
    with repository.get_connection() as conn:
        applied = conn.execute("SELECT result_json FROM background_jobs WHERE kind = 'signal_discovery' "
                               "ORDER BY id DESC LIMIT 1").fetchone()[0]["applied"]
    [topic] = applied["topics"]
    return topic["research"]["fulltext"]


def sections(admin):
    return {card["title"]: card["section_reason"] for card in api_get("/api/signals", admin, section="all")}


# --- Шов 1: контур целиком ------------------------------------------------------------------------------------


def test_pdf_the_old_fetch_skipped_reaches_otbor_as_the_text_of_its_first_pages(user, contour, monkeypatch):
    """PDF прежняя докачка отбрасывала по расширению (federalregister и govinfo 04.11): при «Отборе» это текст
    издателя. Скан без текстового слоя ни ядро (тот же файл), ни браузер (Chromium PDF скачивает) не вытянут."""
    net = Net(monkeypatch, nl={PDF_URL: make_pdf(PDF_PAGES), SCAN_URL: make_pdf([""])})

    run_radar(monkeypatch, [event(PDF_TITLE, PDF_URL), event(SCAN_TITLE, SCAN_URL)], None)

    assert findings() == [PDF_TITLE]
    payload, fact = evidence(PDF_URL)
    assert (payload["full_text_fetcher"], payload["full_text_pdf"]) == ("request", True)
    assert PDF_PAGES[0] in fact and PDF_PAGES[1] in fact
    assert net.steps(SCAN_URL) == ["nl"]
    assert fulltext()["pdf"] == 1


def test_page_the_worker_could_not_open_is_fetched_by_the_core_and_passes_the_run_dedup(user, contour, monkeypatch):
    """Сбор на РФ (iadevon.ru 04.11: с NL молчит, с ядра РФ открылась): страницу качает ядро, и она проходит дедуп
    прогона вместе с другой ссылкой того же события — «Отбор» решает одну находку с двумя ссылками."""
    ru_text = PAGE_TEXT.replace("ADNOC Drilling", "ЛУКОЙЛ-Пермь")
    net = Net(monkeypatch, nl={OTHER_URL: html(OTHER_TITLE)}, core={RU_URL: html(RU_TITLE, ru_text)})

    run_radar(monkeypatch, [event(RU_TITLE, RU_URL), event(OTHER_TITLE, OTHER_URL)], None, same_event=True)

    payload, fact = evidence(RU_URL)
    assert payload["full_text_fetcher"] == "core" and "ЛУКОЙЛ-Пермь will drill" in fact
    # Ядро соединилось с адресом, который само проверило; браузер не понадобился.
    assert net.steps(RU_URL) == ["nl", "core"] and ("core", RU_URL, PUBLIC_IP) in net.calls
    assert fulltext()["via"] == {"core": 1, "browser": 0}
    model = decide(monkeypatch, {RU_TITLE: answer(), OTHER_TITLE: answer()})
    [call] = model.calls
    links = call["input"].split("Ссылки находки:", 1)[1].split("Похожие сигналы", 1)[0]
    assert RU_TITLE in links and OTHER_TITLE in links


def test_page_drawn_by_script_opens_in_the_browser_on_the_second_wait(user, contour, monkeypatch):
    """wam.ae 04.11: запросу 20 знаков, ядру — молчание, браузер к готовности разметки отдал оболочку «WAM», к полной
    загрузке — новость. Находка уходит в «Отбор» с текстом второй попытки."""
    net = Net(monkeypatch, nl={JS_URL: b"<html><head><title>WAM</title></head><body>WAM</body></html>"},
              browser={JS_URL: [SHELL, html(JS_TITLE)]})

    run_radar(monkeypatch, [event(JS_TITLE, JS_URL)], None)

    assert findings() == [JS_TITLE]
    payload, _ = evidence(JS_URL)
    assert payload["full_text_fetcher"] == "browser"
    assert net.steps(JS_URL) == ["nl", "core", "browser", "browser"]
    assert [call[2] for call in net.calls if call[0] == "browser"] == ["domcontentloaded", "load"]
    assert fulltext()["via"] == {"core": 0, "browser": 1}


def test_bot_check_page_is_not_the_publisher_text_and_the_core_brings_the_page(user, contour, monkeypatch):
    """scientica.ru 04.11 отдала NL «Проверяем браузер», и находка ушла в «Отклонённые» как страница-список. Проверка
    браузера — не текст издателя: страницу приносит следующий шаг."""
    net = Net(monkeypatch, nl={RU_URL: BOT_CHECK}, core={RU_URL: html(RU_TITLE)})

    run_radar(monkeypatch, [event(RU_TITLE, RU_URL)], None)

    assert findings() == [RU_TITLE]
    assert net.steps(RU_URL) == ["nl", "core"]
    assert evidence(RU_URL)[0]["full_text_fetcher"] == "core"


def test_page_behind_a_wall_for_robots_stays_a_search_drop_after_every_step(user, admin, contour, monkeypatch):
    """Сайты для инвесторов на Akamai 04.11: запросу молчат, ядру тоже, браузеру отвечают 403. Находка остаётся в
    «Отклонённых → Поиск», как раньше, а счётчики докачки показывают, что пробовали все шаги."""
    net = Net(monkeypatch, browser={WALL_URL: 403})

    run_radar(monkeypatch, [event(WALL_TITLE, WALL_URL)], None)

    assert findings() == []
    assert sections(admin) == {WALL_TITLE: f"Поиск → Текст страницы не получен: {PAGE_NOT_FETCHED}"}
    assert net.steps(WALL_URL) == ["nl", "core", "browser"]
    assert fulltext() == {"attempted": 1, "fetched": 0, "too_short": 0, "failed": 1, "skipped_budget": 0,
                          "via": {"core": 0, "browser": 0}, "pdf": 0}


# --- Шов 2: запрос ядра и порядок шагов -----------------------------------------------------------------------


def test_core_does_not_fetch_an_inner_address_and_does_not_connect_to_it(monkeypatch):
    net = Net(monkeypatch, resolve={"metadata.example": ["169.254.169.254"], "mixed.example": [PUBLIC_IP, "10.0.0.7"]})

    for url in ("http://metadata.example/latest/meta-data/", "https://mixed.example/news",
                "http://127.0.0.1:8000/api/health", "file:///etc/passwd", "https://user:secret@site.example/a"):
        page = publisher_page.download(url)
        assert page["ok"] is False and page["error"].startswith("ядро не качает"), url
    assert net.calls == []


def test_core_checks_every_redirect_and_follows_only_public_ones(monkeypatch):
    start, public, inner = "https://news.example/a", "https://www.news.example/a", "http://metadata.example/latest/"
    net = Net(monkeypatch, core={start: ("redirect", public), public: html(RU_TITLE)},
              resolve={"metadata.example": ["169.254.169.254"]})

    page = publisher_page.download(start)
    assert page["ok"] is True and page["final_url"] == public and RU_TITLE.encode() in page["content"]

    net.core[public] = ("redirect", inner)
    page = publisher_page.download(start)
    assert page["ok"] is False and "внутренний адрес 169.254.169.254" in page["error"]
    assert [call[1] for call in net.calls] == [start, public, start, public]


def test_core_stops_reading_a_page_over_the_size_limit(monkeypatch):
    monkeypatch.setattr(publisher_page.config, "SIGNAL_PAGE_MAX_BYTES", 1000)
    Net(monkeypatch, core={"https://big.example/report": b"x" * 5000})

    page = publisher_page.download("https://big.example/report")

    assert page["ok"] is False and "больше" in page["error"]


def test_core_page_address_answers_only_the_worker(monkeypatch):
    monkeypatch.setattr(api.config, "EXTERNAL_WORKER_TOKEN_HASH", api._sha256_hex("secret"))
    Net(monkeypatch, core={RU_URL: html(RU_TITLE)})
    client = TestClient(api.app)

    assert client.post("/api/external-worker/page", json={"url": RU_URL}).status_code == 401
    answer = client.post("/api/external-worker/page", json={"url": RU_URL},
                         headers={"Authorization": "Bearer secret"}).json()

    assert answer["ok"] is True and RU_TITLE.encode() in base64.b64decode(answer["content_b64"])


def test_worker_on_a_core_without_the_page_address_goes_on_without_it(monkeypatch):
    class _OldCore:
        headers = {}

        def post(self, url, **kwargs):
            return type("Response", (), {"status_code": 404})()

    client = external_worker.ExternalWorkerClient(core_api_url="http://core.example", token="secret", worker_id="nl",
                                                  queues=["external-agents"], capabilities=["openai"])
    client.session = _OldCore()

    answer = client.fetch_page(RU_URL)

    assert answer["ok"] is False and "до 04.11" in answer["error"]


@pytest.mark.parametrize("browser_enabled", [True, False])
def test_page_with_a_list_title_is_kept_when_no_step_brings_a_better_one(monkeypatch, browser_enabled):
    """aer.ca 04.11: «Bulletin 2026-45» — настоящая страница с коротким заголовком. Шаги дальше её не улучшат; итог —
    она сама, и её, как раньше, отсеет правило страницы-списка. Браузер выключается настройкой NL."""
    url, title = "https://www.aer.example/bulletins/bulletin-2026-45", "Bulletin 2026-45"
    monkeypatch.setattr(publisher_page.config, "SIGNAL_PAGE_BROWSER_ENABLED", browser_enabled)
    net = Net(monkeypatch, nl={url: html(title)}, browser={url: [html(title), html(title)]})

    page = publisher_page.read(url, "AER bulletin on new well licensing rules", timeout=8,
                               weak_title=signal_research.is_list_page)

    assert (page["fetcher"], page["title"]) == ("request", title)
    assert net.steps(url) == (["nl", "browser", "browser"] if browser_enabled else ["nl"])
