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
        return Promise.resolve(jsonResponse([article]));
      }
      if (url === "/api/stats") {
        return Promise.resolve(
          jsonResponse({
            total_articles: 1,
            with_summary: 1,
            processed_articles: 1,
            selected_for_digest: 1,
            avg_score: 87,
            sources: 1,
          }),
        );
      }
      if (url === "/api/digest-branding") {
        return Promise.resolve(jsonResponse(method === "PUT" ? { ok: true, branding: digestBranding } : digestBranding));
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

  it("logs in, navigates, expands a signal and downloads digest without opening a blank tab", async () => {
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Вход в админку" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Зарегистрироваться" }));
    expect(screen.getByRole("heading", { name: "Регистрация" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Войти" }));

    await user.type(screen.getByPlaceholderText("you@example.com"), "user@example.com");
    await user.type(screen.getByPlaceholderText("Не короче 8 символов"), "12345678");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    expect(await screen.findByRole("heading", { name: "Сигналы" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Каталог сигналов" })).toBeInTheDocument();
    expect(screen.getByText("1 сигналов")).toBeInTheDocument();

    // #9: группы-теги свёрнуты по умолчанию — сначала раскрываем группу, потом сам сигнал.
    await user.click(screen.getByRole("button", { name: /Раскрыть группу/ }));
    expect(screen.getAllByText("Directional drilling automation").length).toBeGreaterThanOrEqual(1);

    await user.click(screen.getByRole("button", { name: "Раскрыть сигнал" }));
    expect(screen.getByText("AI summary for drilling automation")).toBeInTheDocument();
    expect(screen.getByText("criterion rationale")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Свернуть сайдбар" }));
    expect(document.querySelector(".shell.sidebarCollapsed")).toBeInTheDocument();

    const desktopNav = screen.getAllByRole("button", { name: "Месячный дайджест" })[0];
    await user.click(desktopNav);
    expect(await screen.findByRole("heading", { name: "Месячный дайджест" })).toBeInTheDocument();
    expect(screen.getAllByText("Directional drilling automation").length).toBeGreaterThanOrEqual(1);

    await user.click(screen.getByRole("button", { name: "PDF" }));

    await waitFor(() => {
      expect(createObjectURLMock).toHaveBeenCalledTimes(1);
    });
    expect(openMock).not.toHaveBeenCalled();
    expect(revokeObjectURLMock).toHaveBeenCalledWith("blob:test");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/jobs/digest-export",
      expect.objectContaining({ method: "POST", credentials: "same-origin" }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/jobs/17/download",
      expect.objectContaining({ credentials: "same-origin" }),
    );

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
});
