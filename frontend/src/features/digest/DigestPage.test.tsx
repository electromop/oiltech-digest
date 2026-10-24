import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Article, ArchiveMonth, DigestContentItem } from "../../api/types";
import { DigestPage } from "./DigestPage";

// Какой выпуск открыт по умолчанию (решение владельца 29.09: сентябрьский выпуск собирают
// сами пользователи). Раньше экран открывался на «Все месяцы»: превью и выгрузка брали
// отметки за всё время, включая архив, а «Сохранить draft» клал черновик в месяц по часам
// браузера (UTC) — 1–4 октября это октябрь, хотя собирается сентябрьский выпуск.
// С пересмотра 23.10 выпуск — всегда месяц: «Все месяцы» в списке нет, предпросмотр — сразу PDF,
// прошлые выпуски — во вкладке «Архив выпусков».

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
const septemberTwo: Article = { ...baseArticle, id: 903, title: "Вторая сентябрьская", date: "2026-09-22", collected: "2026-09-22" };
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
  let news: DigestContentItem[] = [];

  beforeEach(() => {
    news = [];
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url === "/api/feed-window") {
        return Promise.resolve(jsonResponse({ months: openMonths, month: null, read_only: false, rollover_day: 5, archive }));
      }
      if (url.startsWith("/api/articles")) {
        return Promise.resolve(jsonResponse(marked));
      }
      if (url.startsWith("/api/digest-content")) {
        return Promise.resolve(jsonResponse({ month: null, title: "Нефтесервисный дайджест", news }));
      }
      if (url.startsWith("/api/digest-preview.pdf")) {
        return Promise.resolve(
          new Response(new Blob(["%PDF-1.7"], { type: "application/pdf" }), {
            status: 200,
            headers: { "Content-Type": "application/pdf", "Content-Disposition": 'inline; filename="digest-preview.pdf"' },
          }),
        );
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
  const pdfRequests = () => requested().filter((url) => url.startsWith("/api/digest-preview.pdf?"));

  it("1–4 числа открыт выпуск прошлого месяца: очередь, превью и черновик — сентябрьские", async () => {
    openMonths = ["2026-09", "2026-10"];
    archive = [];
    marked = [september, october];
    const user = userEvent.setup();
    render(<DigestPage onUnauthorized={() => undefined} showToast={() => undefined} />);

    const monthSelect = await screen.findByDisplayValue("сентябрь 2026");
    expect(within(monthSelect).getByRole("option", { name: "октябрь 2026" })).toBeInTheDocument();
    expect(within(monthSelect).queryByRole("option", { name: "Все месяцы" })).not.toBeInTheDocument();
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

    const monthSelect = await screen.findByDisplayValue("октябрь 2026");
    expect(within(monthSelect).getByRole("option", { name: "сентябрь 2026 · архив" })).toBeInTheDocument();
    await waitFor(() => expect(requested()).toContain("/api/monthly-digests/2026-10"));
  });

  it("архив выпусков: прошлые выпуски списком, «Открыть» — выпуск на просмотр, без правки", async () => {
    openMonths = ["2026-10"];
    archive = [
      { month: "2026-08", articles: 300, digest: 4 },
      { month: "2026-09", articles: 120, digest: 1 },
      { month: "2026-07", articles: 90, digest: 0 },
    ];
    marked = [september];
    const user = userEvent.setup();
    render(<DigestPage onUnauthorized={() => undefined} showToast={() => undefined} />);

    await user.click(await screen.findByRole("button", { name: /Архив выпусков/ }));

    // Выпуски с отметками, новые сверху; месяц без выбранного — не выпуск.
    const rows = within(screen.getByRole("table")).getAllByRole("rowheader").map((cell) => cell.textContent);
    expect(rows).toEqual(["сентябрь 2026", "август 2026"]);

    const september2026 = screen.getByRole("rowheader", { name: "сентябрь 2026" }).closest("tr") as HTMLElement;
    await user.click(within(september2026).getByRole("button", { name: "Открыть" }));

    expect(await screen.findByText(/Выпуск за сентябрь 2026 в архиве/)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Выпуск" })).toHaveValue("2026-09");
    expect(screen.queryByRole("button", { name: "Сохранить черновик" })).not.toBeInTheDocument();
    expect(await screen.findByText("Сентябрьская статья")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Из дайджеста" })).not.toBeInTheDocument();
  });

  it("плашка «Технология / Бизнес» у материала — из состава выпуска на сервере", async () => {
    openMonths = ["2026-09"];
    archive = [];
    marked = [september, septemberTwo];
    news = [
      { article_id: 901, category: "Бурение", title: september.title, summary: "", url: september.url, kind: "technology", kind_label: "Технология" },
      { article_id: 903, category: "Бурение", title: septemberTwo.title, summary: "", url: septemberTwo.url, kind: "business", kind_label: "Бизнес" },
    ];
    render(<DigestPage onUnauthorized={() => undefined} showToast={() => undefined} />);

    // Пока очередь не собрана, материалы видны в «Отмечено, но не в выпуске» — ждём их в самой очереди.
    await waitFor(() => expect(screen.getByText("Вторая сентябрьская").closest("li")).not.toBeNull());
    const first = screen.getByText("Сентябрьская статья").closest("li") as HTMLElement;
    const second = screen.getByText("Вторая сентябрьская").closest("li") as HTMLElement;
    expect(within(first).getByText("Технология")).toHaveClass("kindBadge", "technology");
    expect(within(second).getByText("Бизнес")).toHaveClass("kindBadge", "business");
  });

  it("сигналы радара в выпуске видны в составе с плашкой и снимаются из дайджеста", async () => {
    openMonths = ["2026-09"];
    archive = [];
    marked = [september];
    news = [
      { item_type: "article", article_id: 901, category: "Бурение", title: september.title, summary: "", url: september.url, kind: "business", kind_label: "Бизнес" },
      { item_type: "signal", signal_id: 77, article_id: null, category: "Цифровизация", title: "Цифровой двойник скважины",
        summary: "Оператор испытал цифрового двойника.", url: "https://radar.example/77", score: 81, kind: "technology", kind_label: "Технология" },
    ];
    const user = userEvent.setup();
    render(<DigestPage onUnauthorized={() => undefined} showToast={() => undefined} />);

    const radar = await screen.findByRole("region", { name: "Сигналы радара в выпуске" });
    expect(within(radar).getByText("Технология")).toHaveClass("kindBadge", "technology");
    expect(within(radar).getByText("Оператор испытал цифрового двойника.")).toBeInTheDocument();
    // В выпуске два материала: статья очереди и сигнал радара.
    expect(screen.getByText(/2 материала в выпуске/)).toBeInTheDocument();

    await user.click(within(radar).getByRole("button", { name: "Из дайджеста" }));

    await waitFor(() => {
      const patch = fetchMock.mock.calls.find(([input, init]) => String(input) === "/api/signals/77" && (init as RequestInit | undefined)?.method === "PATCH");
      expect(patch).toBeDefined();
      expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({ selected_for_digest: false });
    });
  });

  describe("предпросмотр — сразу PDF", () => {
    let created = 0;
    beforeEach(() => {
      created = 0;
      Object.defineProperty(window.URL, "createObjectURL", { configurable: true, value: vi.fn(() => `blob:pdf-${++created}`) });
      Object.defineProperty(window.URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    });

    it("собирается при открытии выпуска; порядок поменяли — PDF прежний, пока черновик не сохранён; сохранили — собран заново", async () => {
      openMonths = ["2026-09"];
      archive = [];
      marked = [september, septemberTwo];
      const user = userEvent.setup();
      render(<DigestPage onUnauthorized={() => undefined} showToast={() => undefined} />);

      const frame = await screen.findByTitle("Предпросмотр выпуска в PDF");
      await waitFor(() => expect(frame).toHaveAttribute("src", "blob:pdf-1"));
      // Один PDF на открытие — за выпуск по умолчанию, а не сначала «за все месяцы».
      expect(pdfRequests()).toHaveLength(1);
      expect(pdfRequests()[0]).toContain("month=2026-09");

      await user.click((await screen.findAllByRole("button", { name: "Опустить материал ниже" }))[0]);
      expect(await screen.findByText(/Порядок изменён, но не сохранён/)).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Сохранить черновик" }));

      await waitFor(() => expect(pdfRequests()).toHaveLength(2));
      await waitFor(() => expect(screen.getByTitle("Предпросмотр выпуска в PDF")).toHaveAttribute("src", "blob:pdf-2"));
      expect(screen.queryByText(/Порядок изменён, но не сохранён/)).not.toBeInTheDocument();
    });
  });
});
