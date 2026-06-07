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
        return Promise.resolve(jsonResponse({ ok: true, user: { id: 1, email: "user@example.com" } }));
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
      if (url === "/api/jobs/digest-export" && method === "POST") {
        return Promise.resolve(jsonResponse({ ok: true, job }));
      }
      if (url === "/api/jobs/17") {
        return Promise.resolve(jsonResponse(job));
      }
      if (url === "/api/jobs/17/download") {
        return Promise.resolve(downloadResponse());
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
    expect(screen.getAllByText("Directional drilling automation").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("1 сигналов")).toBeInTheDocument();

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
});
