import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

const article = {
  id: 101,
  title: "Directional drilling automation",
  url: "https://example.com/drilling",
  source: "World Oil",
  language: "en",
  date: "2026-06-07",
  published_at: "2026-06-07",
  collected: "2026-06-07",
  future_date: false,
  summary: "AI summary for drilling automation",
  tag: "Технологии / Бурение",
  score: 87,
  rating: "Высокая",
  status: "digest",
  digest: true,
  tag_confidence: 0.91,
  tag_rationale: "matched drilling",
  score_explanation: "strong relevance",
  raw_text_chars: 420,
  text_truncated: false,
  relevant: true,
  relevance_reason: "oilfield technology",
  score_items: [
    {
      name: "Технологическая значимость",
      weight: 100,
      final_score: 86.5,
      ai_score: 90,
      keyword_score: 80,
      rationale: "criterion rationale",
    },
  ],
};

const articleTwo = {
  ...article,
  id: 102,
  title: "Subsea intervention robotics",
  url: "https://example.com/subsea",
  source: "Offshore Engineer",
  date: "2026-06-08",
  published_at: "2026-06-08",
  collected: "2026-06-08",
  summary: "AI summary for subsea intervention robotics",
  tag: "Технологии / Подводные работы",
  score: 82,
  raw_text_chars: 510,
};

const articleThree = {
  ...article,
  id: 103,
  title: "Frac fleet electrification program",
  url: "https://example.com/frac-fleet",
  source: "JPT",
  date: "2026-06-09",
  published_at: "2026-06-09",
  collected: "2026-06-09",
  summary: "AI summary for frac fleet electrification",
  tag: "Технологии / ГРП",
  score: 79,
  raw_text_chars: 630,
};

const digestArticles = [article, articleTwo, articleThree];

const job = {
  id: 17,
  kind: "digest_export",
  queue: "playwright",
  execution_region: "ru",
  capability: "playwright",
  status: "ok",
  progress: 100,
  attempts: 1,
  max_attempts: 3,
  payload: { month: "2026-06" },
  result: { path: "/app/exports/digest.pdf", filename: "digest.pdf" },
  error: null,
  run_after: null,
  created_at: "2026-06-07T06:00:00Z",
  started_at: "2026-06-07T06:00:01Z",
  finished_at: "2026-06-07T06:00:03Z",
};

const maintenanceStatus = {
  retention: {
    stale_minutes: 60,
    background_job_days: 30,
    export_job_days: 14,
  },
  expired_sessions: 2,
  stale_running_jobs: 1,
  cleanup_candidates: {
    background_jobs: 5,
    export_jobs: 3,
  },
  external_queues: {
    totals: {
      queued: 4,
      running: 1,
      failed: 2,
      ok: 7,
      oldest_queued_at: "2026-06-07T06:00:00Z",
      last_heartbeat_at: "2026-06-07T06:05:00Z",
      expired_leases: 0,
    },
    queues: [
      {
        queue_name: "external-ai",
        queued: 4,
        running: 1,
        failed: 2,
        ok: 7,
        oldest_queued_at: "2026-06-07T06:00:00Z",
        last_heartbeat_at: "2026-06-07T06:05:00Z",
      },
    ],
  },
};

const maintenanceBenchmark = {
  iterations: 3,
  warn_ms: 800,
  params: {
    articles_limit: 200,
    source_limit: 150,
    jobs_limit: 100,
    month: null,
    digest_limit: 100,
    min_score: 0,
  },
  benchmarks: [
    { name: "articles_list", runs: 3, rows: 120, p50_ms: 12.5, p95_ms: 28.4, max_ms: 30.1, status: "ok" },
    { name: "source_health", runs: 3, rows: 60, p50_ms: 15.2, p95_ms: 812.3, max_ms: 820.0, status: "warn" },
  ],
  counts: {
    sources: 120,
    articles: 6000,
    article_cards: 5800,
    article_tags: 9200,
    article_scores: 5700,
    background_jobs: 84,
  },
  warnings: ["source_health"],
};

const digestBranding = {
  header: {
    brand_text: "ГАЗПРОМ НЕФТЬ",
    brand_suffix: "ЭНЕРГИЯ В ЛЮДЯХ",
    department_text: "БЛОК РАЗВИТИЯ БИЗНЕСА",
  },
  hero: {
    badge: "НОВОСТИ",
    headline: "НЕФТЕСЕРВИСНЫЙ ДАЙДЖЕСТ",
    subtitle: "Технологии, рынок и возможности для бизнеса",
    image_url: "",
  },
  issue: {
    title_template: "Нефтесервисный дайджест",
    title_template_with_month: "Нефтесервисный дайджест · {month}",
    period_label_all: "за всё время",
    preheader: "Ключевые новости и обзоры нефтесервисного рынка",
    intro_template:
      "Уважаемые коллеги! Представляем ключевые новости и обзоры нефтесервисного рынка, которые помогают отслеживать технологические тренды, рыночную динамику и возможности для развития бизнеса.",
    intro_template_with_month:
      "Уважаемые коллеги! Представляем ключевые новости и обзоры за {month}, которые помогают отслеживать технологические тренды, рыночную динамику и возможности для развития нефтесервисного бизнеса.",
    highlights_title: "Главное за период",
    news_title: "Новости",
    read_more_label: "ЧИТАТЬ ДАЛЕЕ",
    empty_summary_text: "Суть ещё не сформирована.",
    preview_empty_text: "В текущей выборке нет сигналов для превью.",
  },
  footer: {
    contact_text: "При возникновении вопросов обращайтесь в Блок развития бизнеса",
    contact_email: "Rodionov.VVL@gazprom-neft.ru",
    note: "Внутренняя корпоративная рассылка",
    socials: [{ label: "VK", accent: "#0077ff", text: "VK" }],
  },
  highlights: {
    analytics_source_keywords: ["rystad"],
    analytics_category_keywords: ["аналит"],
    business_category_keywords: ["контракт"],
    cards: [
      { metric: "total", icon: "doc", prefix: "", suffix: "", noun_one: "новость", noun_few: "новости", noun_many: "новостей" },
      { metric: "analytics", icon: "chart", prefix: "аналитических", suffix: "", noun_one: "материал", noun_few: "материала", noun_many: "материалов" },
      { metric: "business", icon: "people", prefix: "", suffix: "для бизнеса", noun_one: "возможность", noun_few: "возможности", noun_many: "возможностей" },
    ],
  },
};

const digestContent = {
  month: "2026-06",
  title: "Нефтесервисный дайджест · 2026-06",
  issue: {
    preheader: digestBranding.issue.preheader,
    intro: "Тестовый intro",
    news_title: digestBranding.issue.news_title,
    read_more_label: digestBranding.issue.read_more_label,
    empty_summary_text: digestBranding.issue.empty_summary_text,
    preview_empty_text: digestBranding.issue.preview_empty_text,
  },
  hero: digestBranding.hero,
  news: [
    {
      article_id: article.id,
      category: article.tag,
      title: article.title,
      summary: article.summary,
      url: article.url,
      score: article.score,
    },
    {
      article_id: articleTwo.id,
      category: articleTwo.tag,
      title: articleTwo.title,
      summary: articleTwo.summary,
      url: articleTwo.url,
      score: articleTwo.score,
    },
    {
      article_id: articleThree.id,
      category: articleThree.tag,
      title: articleThree.title,
      summary: articleThree.summary,
      url: articleThree.url,
      score: articleThree.score,
    },
  ],
  footer: digestBranding.footer,
};

const digestEmailHtml = "";

function jsonResponse(payload: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
}

function downloadResponse() {
  return new Response(new Blob(["PDF"], { type: "application/pdf" }), {
    status: 200,
    headers: {
      "Content-Type": "application/pdf",
      "Content-Disposition": 'attachment; filename="digest.pdf"',
    },
  });
}

describe("App smoke", () => {
  const fetchMock = vi.fn();
  const openMock = vi.fn();
  const createObjectURLMock = vi.fn(() => "blob:test");
  const revokeObjectURLMock = vi.fn();
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";

      if (url === "/api/auth/me") {
        return Promise.resolve(jsonResponse({ detail: "Not authenticated" }, { status: 401 }));
      }
      if (url === "/api/auth/login" && method === "POST") {
        return Promise.resolve(jsonResponse({ ok: true, user: { id: 1, email: "user@example.com", role: "admin" } }));
      }
      if (url.startsWith("/api/articles")) {
        return Promise.resolve(jsonResponse(digestArticles));
      }
      if (url === "/api/stats") {
        return Promise.resolve(
          jsonResponse({
            total_articles: 3,
            with_summary: 3,
            processed_articles: 3,
            selected_for_digest: 3,
            avg_score: 82,
            sources: 3,
            // Счётчики по статусам с сервера (по всей базе), НАМЕРЕННО расходятся с
            // загруженными фикстурами (все 3 — 'digest'): так тест отличает серверную
            // привязку от клиентского фолбэка `?? articles.filter(...)`, который дал бы 0.
            status_counts: { new: 5, review: 2, digest: 1, archive: 3, noise: 999, duplicate: 7 },
          }),
        );
      }
      if (url === "/api/digest-branding") {
        return Promise.resolve(jsonResponse(method === "PUT" ? { ok: true, branding: digestBranding } : digestBranding));
      }
      if (url.startsWith("/api/digest-content")) {
        return Promise.resolve(jsonResponse(digestContent));
      }
      if (url.startsWith("/api/digest-email")) {
        return Promise.resolve(new Response(digestEmailHtml, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } }));
      }
      if (url.startsWith("/api/monthly-digests/") && method === "PUT") {
        const monthValue = decodeURIComponent(url.split("/api/monthly-digests/")[1] || "2026-06");
        return Promise.resolve(jsonResponse({ id: 17, month: monthValue, title: `Нефтесервисный дайджест · ${monthValue}`, status: "draft", items: 1 }));
      }
      if (url.startsWith("/api/monthly-digests/")) {
        const monthValue = decodeURIComponent(url.split("/api/monthly-digests/")[1] || "2026-06");
        return Promise.resolve(jsonResponse({ id: 17, month: monthValue, title: `Нефтесервисный дайджест · ${monthValue}`, status: "draft", items: [] }));
      }
      if (url === "/api/jobs/digest-export" && method === "POST") {
        return Promise.resolve(jsonResponse({ ok: true, job }));
      }
      if (url === "/api/jobs/17") {
        return Promise.resolve(jsonResponse(job));
      }
      if (url === "/api/jobs/17/download") {
        return Promise.resolve(downloadResponse());
      }
      if (url === "/api/maintenance/status") {
        return Promise.resolve(jsonResponse(maintenanceStatus));
      }
      if (url === "/api/maintenance/cleanup" && method === "POST") {
        return Promise.resolve(
          jsonResponse({
            ok: true,
            result: {
              expired_sessions: 2,
              background_jobs: 5,
              background_job_days: 30,
              export_jobs: 3,
              export_job_days: 14,
            },
          }),
        );
      }
      if (url === "/api/maintenance/benchmark?iterations=3") {
        return Promise.resolve(jsonResponse(maintenanceBenchmark));
      }
      if (url.startsWith("/api/jobs")) {
        return Promise.resolve(jsonResponse([job]));
      }

      return Promise.resolve(jsonResponse({ ok: true }));
    });

    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("open", openMock);
    Object.defineProperty(window.URL, "createObjectURL", { configurable: true, value: createObjectURLMock });
    Object.defineProperty(window.URL, "revokeObjectURL", { configurable: true, value: revokeObjectURLMock });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    fetchMock.mockReset();
    openMock.mockReset();
    createObjectURLMock.mockClear();
    revokeObjectURLMock.mockClear();
  });

  it("logs in, navigates and expands a signal", async () => {
    const user = userEvent.setup();
    const { container } = render(<App />);

    expect(await screen.findByRole("heading", { name: "Вход в админ-панель" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Зарегистрироваться" }));
    expect(screen.getByRole("heading", { name: "Регистрация" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Войти" }));

    await user.type(screen.getByPlaceholderText("you@example.com"), "user@example.com");
    await user.type(screen.getByPlaceholderText("Не короче 8 символов"), "12345678");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    expect(await screen.findByRole("heading", { name: "Сигналы" })).toBeInTheDocument();
    const requestedUrls = fetchMock.mock.calls.map(([input]) => String(input));
    expect(requestedUrls).toContain("/api/articles?limit=2000&min_score=50&max_score=100&sort=score_desc");
    expect(screen.getByRole("heading", { name: "Каталог сигналов" })).toBeInTheDocument();
    const catalogBadge = container.querySelector(".panelHeader .badge");
    expect(catalogBadge).not.toBeNull();
    expect(catalogBadge?.textContent ?? "").toMatch(/сигнал|Выборка по всей базе|Обновляем выборку по всей базе/);
    // #9: группы-теги свёрнуты по умолчанию — сначала раскрываем группу, потом сам сигнал.
    await user.click(screen.getByRole("button", { name: /Раскрыть группу/ }));
    expect(screen.getAllByText("Directional drilling automation").length).toBeGreaterThanOrEqual(1);

    await user.click(screen.getAllByRole("button", { name: "Раскрыть сигнал" })[0]);
    expect(screen.getAllByRole("button", { name: "Свернуть сигнал" }).length).toBeGreaterThanOrEqual(1);

    await user.click(screen.getByRole("button", { name: "Свернуть сайдбар" }));
    expect(document.querySelector(".shell.sidebarCollapsed")).toBeInTheDocument();

    const mobileNav = document.querySelector(".mobileNav");
    expect(mobileNav).not.toBeNull();
    expect(within(mobileNav as HTMLElement).getByRole("button", { name: "Сигналы" })).toBeInTheDocument();
    expect(within(mobileNav as HTMLElement).queryByRole("button", { name: "Фоновые задачи" })).not.toBeInTheDocument();
  });

  it("opens hidden jobs page from query string without adding it to navigation", async () => {
    window.history.replaceState(null, "", "/?screen=jobs");

    const user = userEvent.setup();
    render(<App />);

    await user.type(await screen.findByPlaceholderText("you@example.com"), "user@example.com");
    await user.type(screen.getByPlaceholderText("Не короче 8 символов"), "12345678");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    expect(await screen.findByRole("heading", { name: "Фоновые задачи" })).toBeInTheDocument();
    expect(screen.getByText("#17")).toBeInTheDocument();
    expect(screen.getByText("digest_export")).toBeInTheDocument();
    expect(screen.getAllByText("playwright").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByRole("button", { name: "Фоновые задачи" })).not.toBeInTheDocument();
  });

  it("opens hidden maintenance page from query string without adding it to navigation", async () => {
    window.history.replaceState(null, "", "/?screen=maintenance");

    const user = userEvent.setup();
    render(<App />);

    await user.type(await screen.findByPlaceholderText("you@example.com"), "user@example.com");
    await user.type(screen.getByPlaceholderText("Не короче 8 символов"), "12345678");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    expect(await screen.findByRole("heading", { name: "Обслуживание сервиса" })).toBeInTheDocument();
    expect(screen.getByText("Истекшие сессии")).toBeInTheDocument();
    expect(screen.getByText("Фоновые задачи к очистке")).toBeInTheDocument();
    expect(screen.getByText("Внешний контур")).toBeInTheDocument();
    expect(screen.getByText("external-ai")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Обслуживание сервиса" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Запустить замер" }));

    expect(await screen.findByText("articles_list")).toBeInTheDocument();
    expect(screen.getAllByText("source_health").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("warn").length).toBeGreaterThanOrEqual(1);
  });

  // Плитки статусов должны показывать серверные счётчики (status_counts по всей базе),
  // а не клиентский подсчёт по загруженной странице. Мок /api/stats отдаёт noise=999 и
  // duplicate=7, тогда как среди 3 загруженных статей нет ни одной 'noise'/'duplicate' —
  // клиентский фолбэк дал бы 0. Значит ненулевые числа доказывают привязку к серверу.
  it("плитки статусов берут счётчики с сервера, а не считают по загруженной странице", async () => {
    const user = userEvent.setup();
    const { container } = render(<App />);

    await user.type(await screen.findByPlaceholderText("you@example.com"), "user@example.com");
    await user.type(screen.getByPlaceholderText("Не короче 8 символов"), "12345678");
    await user.click(screen.getByRole("button", { name: "Войти" }));
    expect(await screen.findByRole("heading", { name: "Сигналы" })).toBeInTheDocument();

    const tileValue = (label: string) => {
      const cards = Array.from(container.querySelectorAll(".statCardReact"));
      const card = cards.find((el) => el.querySelector(".metaText")?.textContent === label);
      return card?.querySelector(".statValueReact")?.textContent ?? null;
    };

    await waitFor(() => expect(tileValue("Шум")).toBe("999"));
    expect(tileValue("Дубликаты")).toBe("7");
    expect(tileValue("Новые")).toBe("5");
    expect(tileValue("На проверке")).toBe("2");
  });

  // Регресс на бесконечный цикл переподгрузки ленты.
  // activeServerQuery был объектным литералом, пересоздаваемым на КАЖДОМ рендере, и стоял
  // в deps эффекта серверного поиска. Эффект вызывал setServerResults/setSearching → рендер →
  // новая идентичность объекта → эффект снова → fetch каждые ~400мс, бесконечно.
  // Взводился при ЛЮБОМ активном фильтре (поиск/тег/статус/источник/язык/score/сорт/«Со статусом»).
  it("не перезапрашивает ленту в цикле, пока активен фильтр", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.type(await screen.findByPlaceholderText("you@example.com"), "user@example.com");
    await user.type(screen.getByPlaceholderText("Не короче 8 символов"), "12345678");
    await user.click(screen.getByRole("button", { name: "Войти" }));
    expect(await screen.findByRole("heading", { name: "Сигналы" })).toBeInTheDocument();

    const articleCalls = () =>
      fetchMock.mock.calls.filter(([input]) => String(input).startsWith("/api/articles?")).length;

    // Взводим серверный фильтр — ровно то, что делает аналитик.
    await user.type(screen.getByPlaceholderText("Поиск по всей базе: название, текст, суть"), "бурение");

    // Ждём, пока debounce (400мс) отработает и выборка придёт.
    await waitFor(() => expect(articleCalls()).toBeGreaterThan(1));
    const settled = articleCalls();

    // Даём пройти времени, которого хватило бы на несколько циклов debounce (400мс).
    // На багованном коде здесь набегает поток новых запросов; на исправленном — ни одного.
    await new Promise((resolve) => setTimeout(resolve, 1500));

    expect(articleCalls()).toBe(settled);
  });

  // Второй фронт того же бага: нестабильный activeServerQuery стоял и в deps 40с-эффекта,
  // поэтому setInterval(40с) пересоздавался на КАЖДОМ рендере и «живое» автообновление при
  // активном фильтре не доживало до срабатывания. Ждать 40с в тесте не нужно — достаточно
  // убедиться, что таймер заводится один раз и не пересоздаётся бесконечно.
  it("не пересоздаёт 40с-таймер автообновления на каждом рендере", async () => {
    const setIntervalSpy = vi.spyOn(window, "setInterval");
    const user = userEvent.setup();
    render(<App />);

    await user.type(await screen.findByPlaceholderText("you@example.com"), "user@example.com");
    await user.type(screen.getByPlaceholderText("Не короче 8 символов"), "12345678");
    await user.click(screen.getByRole("button", { name: "Войти" }));
    expect(await screen.findByRole("heading", { name: "Сигналы" })).toBeInTheDocument();

    const autoRefreshTimers = () =>
      setIntervalSpy.mock.calls.filter(([, ms]) => ms === 40000).length;

    await user.type(screen.getByPlaceholderText("Поиск по всей базе: название, текст, суть"), "бурение");
    await waitFor(() => expect(autoRefreshTimers()).toBeGreaterThan(0));

    // Даём выборке полностью улечься: debounce (400мс) + ответ + сброс флага searching.
    // До этого момента таймер пересоздаётся ЗАКОННО (меняется фильтр — меняются deps).
    await new Promise((resolve) => setTimeout(resolve, 1200));
    const settled = autoRefreshTimers();

    // А вот в ТИШИНЕ (пользователь ничего не трогает) таймер пересоздаваться не должен.
    // На багованном коде цикл рендеров плодил новый setInterval снова и снова, из-за чего
    // 40с-автообновление не доживало до срабатывания.
    await new Promise((resolve) => setTimeout(resolve, 1500));

    expect(autoRefreshTimers()).toBe(settled);
  });

});
