import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Article, ArchiveMonth } from "../../api/types";
import { DigestPage } from "./DigestPage";

// Какой выпуск открыт по умолчанию (решение владельца 29.09: сентябрьский выпуск собирают
// сами пользователи). Раньше экран открывался на «Все месяцы»: превью и выгрузка брали
// отметки за всё время, включая архив, а «Сохранить draft» клал черновик в месяц по часам
// браузера (UTC) — 1–4 октября это октябрь, хотя собирается сентябрьский выпуск.

const baseArticle: Article = {
  id: 0,
  title: "",
  url: "https://news.example.org/0",
  source: "World Oil",
  tag: "Бурение",
  summary: "Суть материала.",
  score: 70,
  rating: "Средняя",
  status: "digest",
  language: "ru",
  date: null,
  collected: null,
  raw_text_chars: 900,
  text_truncated: false,
  relevant: true,
  relevance_reason: null,
  digest: true,
};

const september: Article = { ...baseArticle, id: 901, title: "Сентябрьская статья", date: "2026-09-20", collected: "2026-09-20" };
const october: Article = { ...baseArticle, id: 902, title: "Октябрьская статья", date: "2026-10-02", collected: "2026-10-02" };

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("экран выпуска: какой месяц открыт по умолчанию", () => {
  const fetchMock = vi.fn();
  let openMonths: string[] = [];
  let archive: ArchiveMonth[] = [];
  let marked: Article[] = [];

  beforeEach(() => {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url === "/api/feed-window") {
        return Promise.resolve(jsonResponse({ months: openMonths, month: null, read_only: false, rollover_day: 5, archive }));
      }
      if (url.startsWith("/api/articles")) {
        return Promise.resolve(jsonResponse(marked));
      }
      if (url === "/api/digest-branding") {
        return Promise.resolve(jsonResponse({ header: {}, hero: {}, issue: {}, footer: { socials: [] }, highlights: {} }));
      }
      if (url.startsWith("/api/digest-content")) {
        return Promise.resolve(jsonResponse({ month: null, title: "Нефтесервисный дайджест", news: [] }));
      }
      if (url.startsWith("/api/digest-email")) {
        return Promise.resolve(new Response("", { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } }));
      }
      if (url.startsWith("/api/monthly-digests/") && method === "PUT") {
        const month = decodeURIComponent(url.split("/api/monthly-digests/")[1]);
        return Promise.resolve(jsonResponse({ id: 1, month, title: "", status: "draft", items: 1 }));
      }
      if (url.startsWith("/api/monthly-digests/")) {
        return Promise.resolve(jsonResponse({ detail: "Digest not found" }, { status: 404 }));
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  const requested = () => fetchMock.mock.calls.map(([input]) => String(input));

  it("1–4 числа открыт выпуск прошлого месяца: очередь, превью и черновик — сентябрьские", async () => {
    openMonths = ["2026-09", "2026-10"];
    archive = [];
    marked = [september, october];
    const user = userEvent.setup();
    render(<DigestPage onUnauthorized={() => undefined} showToast={() => undefined} />);

    const monthSelect = await screen.findByDisplayValue("2026-09");
    expect(within(monthSelect).getByRole("option", { name: "2026-10" })).toBeInTheDocument();
    expect(await screen.findByText("Сентябрьская статья")).toBeInTheDocument();
    expect(screen.queryByText("Октябрьская статья")).not.toBeInTheDocument();
    await waitFor(() => expect(requested()).toContain("/api/monthly-digests/2026-09"));
    await waitFor(() =>
      expect(requested().some((url) => url.startsWith("/api/digest-content?month=2026-09&"))).toBe(true),
    );

    await user.click(screen.getByRole("button", { name: /^Сохранить/ }));

    await waitFor(() => {
      const put = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
      expect(put).toBeDefined();
      expect(String(put?.[0])).toBe("/api/monthly-digests/2026-09");
      expect(JSON.parse(String((put?.[1] as RequestInit).body)).items.map((item: { article_id: number }) => item.article_id)).toEqual([901]);
    });
  });

  it("с 5-го открыт текущий месяц — даже пустой, а прошлый выпуск остаётся в списке архивом", async () => {
    openMonths = ["2026-10"];
    archive = [{ month: "2026-09", articles: 120, digest: 1 }];
    marked = [];
    render(<DigestPage onUnauthorized={() => undefined} showToast={() => undefined} />);

    const monthSelect = await screen.findByDisplayValue("2026-10");
    expect(within(monthSelect).getByRole("option", { name: "2026-09 · архив" })).toBeInTheDocument();
    await waitFor(() => expect(requested()).toContain("/api/monthly-digests/2026-10"));
  });
});
