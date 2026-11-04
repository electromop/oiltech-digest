import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Article, ArchiveMonth, DigestContentItem } from "../../api/types";
import { DigestPage } from "./DigestPage";

// Какой выпуск открыт по умолчанию (решение владельца 29.09: сентябрьский выпуск собирают
// сами пользователи). Раньше экран открывался на «Все месяцы»: превью и выгрузка брали
// отметки за всё время, включая архив, а «Сохранить draft» клал черновик в месяц по часам
// браузера (UTC) — 1–4 октября это октябрь, хотя собирается сентябрьский выпуск.
// С пересмотра 23-24.10 выбора выпуска на экране нет: собирается выпуск первого открытого месяца окна, прошлые —
// только во вкладке «Архив выпусков» и только на просмотр; предпросмотр — сразу PDF.

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

    expect(await screen.findByText(/Выпуск за сентябрь 2026/)).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Выпуск" })).not.toBeInTheDocument();
    // Пока очередь не собрана, статья видна в «Отмечено, но не в выпуске» — ждём её в самой очереди.
    await waitFor(() => expect(screen.getByText("Сентябрьская статья").closest("li")).not.toBeNull());
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

    const user = userEvent.setup();
    render(<DigestPage onUnauthorized={() => undefined} showToast={() => undefined} />);

    expect(await screen.findByText(/Выпуск за октябрь 2026/)).toBeInTheDocument();
    await waitFor(() => expect(requested()).toContain("/api/monthly-digests/2026-10"));
    await user.click(screen.getByRole("button", { name: /Архив выпусков/ }));
    expect(screen.getByRole("rowheader", { name: "сентябрь 2026" })).toBeInTheDocument();
  });

  it("архив выпусков: прошлые выпуски списком, «Открыть» — выпуск на просмотр в той же вкладке, без правки", async () => {
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
    expect(screen.getByText(/Прошлый выпуск за сентябрь 2026/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Архив выпусков/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("button", { name: "Сохранить черновик" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Сентябрьская статья")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Из дайджеста" })).not.toBeInTheDocument();
    await waitFor(() => expect(requested().some((url) => url.startsWith("/api/articles") && url.includes("month=2026-09"))).toBe(true));

    // Назад — к списку выпусков; «Сборка выпуска» — снова текущий выпуск.
    await user.click(screen.getByRole("button", { name: "← Все выпуски" }));
    expect(screen.getByRole("table")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Сборка выпуска" }));
    expect(await screen.findByText(/Выпуск за октябрь 2026/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Сохранить черновик" })).toBeInTheDocument();
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

  it("сигналы радара — в том же списке после статей, с плашкой; снимаются из дайджеста", async () => {
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

    // Отдельного блока нет (решение владельца 24.10): одна очередь, сигнал — после статьи, различает плашка.
    expect(screen.queryByRole("region", { name: "Сигналы радара в выпуске" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Цифровой двойник скважины").closest("li")).not.toBeNull());
    const rows = screen.getAllByRole("listitem").filter((row) => row.classList.contains("digestPickRow"));
    expect(rows.map((row) => row.querySelector("strong")?.textContent)).toEqual(["Сентябрьская статья", "Цифровой двойник скважины"]);
    const radar = rows[1];
    expect(within(radar).getByText("Технология")).toHaveClass("kindBadge", "technology");
    expect(within(radar).getByText("Оператор испытал цифрового двойника.")).toBeInTheDocument();
    expect(within(radar).queryByRole("button", { name: "Поднять материал выше" })).not.toBeInTheDocument();
    // В выпуске два материала: статья очереди и сигнал радара.
    expect(screen.getByText(/2 материала в выпуске/)).toBeInTheDocument();

    await user.click(within(radar).getByRole("button", { name: "Из дайджеста" }));

    await waitFor(() => {
      const patch = fetchMock.mock.calls.find(([input, init]) => String(input) === "/api/signals/77" && (init as RequestInit | undefined)?.method === "PATCH");
      expect(patch).toBeDefined();
      expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({ selected_for_digest: false });
    });
  });

  it("у материала без балла в очереди «без баллов», а не «0», как на витринах", async () => {
    openMonths = ["2026-09"];
    archive = [];
    marked = [{ ...september, score: 0 }];
    news = [
      { item_type: "article", article_id: 901, category: "Бурение", title: september.title, summary: "", url: september.url },
      { item_type: "signal", signal_id: 77, article_id: null, category: "Цифровизация", title: "Находка без баллов",
        summary: "Суть находки.", url: "https://radar.example/77", score: 0 },
      { item_type: "signal", signal_id: 78, article_id: null, category: "Цифровизация", title: "Оценённая находка",
        summary: "Суть находки.", url: "https://radar.example/78", score: 81 },
    ];
    render(<DigestPage onUnauthorized={() => undefined} showToast={() => undefined} />);

    await waitFor(() => expect(screen.getByText("Оценённая находка").closest("li")).not.toBeNull());
    const rows = screen.getAllByRole("listitem").filter((row) => row.classList.contains("digestPickRow"));
    for (const row of rows.slice(0, 2)) {
      expect(within(row).getByText("без баллов")).toBeInTheDocument();
      expect(within(row).queryByText("0")).not.toBeInTheDocument();
    }
    expect(within(rows[2]).getByText("81").closest(".scoreBadge")).not.toBeNull();
    expect(within(rows[2]).queryByText("без баллов")).not.toBeInTheDocument();
  });

  describe("предпросмотр — сразу PDF", () => {
    let created = 0;
    beforeEach(() => {
      created = 0;
      Object.defineProperty(window.URL, "createObjectURL", { configurable: true, value: vi.fn(() => `blob:pdf-${++created}`) });
      Object.defineProperty(window.URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    });

    it("переход к списку архива и обратно PDF не пересобирает; прошлый выпуск — целиком, без фильтров (ревью 24.10)", async () => {
      openMonths = ["2026-10"];
      archive = [{ month: "2026-09", articles: 120, digest: 1 }];
      marked = [september];
      const user = userEvent.setup();
      render(<DigestPage onUnauthorized={() => undefined} showToast={() => undefined} />);

      await waitFor(() => expect(pdfRequests()).toHaveLength(1));
      await user.clear(screen.getByRole("spinbutton", { name: "Балл от" }));
      await user.type(screen.getByRole("spinbutton", { name: "Балл от" }), "60");

      await user.click(screen.getByRole("button", { name: /Архив выпусков/ }));
      await user.click(screen.getByRole("button", { name: "Сборка выпуска" }));
      expect(pdfRequests()).toHaveLength(1);

      await user.click(screen.getByRole("button", { name: /Архив выпусков/ }));
      const row = screen.getByRole("rowheader", { name: "сентябрь 2026" }).closest("tr") as HTMLElement;
      await user.click(within(row).getByRole("button", { name: "Открыть" }));

      // Один запрос PDF на открытие, выпуск целиком: фильтр «Балл от 60» к прошлому выпуску не применяется и не виден.
      await waitFor(() => expect(pdfRequests()).toHaveLength(2));
      expect(pdfRequests()[1]).toContain("month=2026-09");
      expect(pdfRequests()[1]).toContain("min_score=0");
      expect(screen.queryByRole("spinbutton", { name: "Балл от" })).not.toBeInTheDocument();
    });

    it("собирается при открытии выпуска; порядок черновика PDF не задаёт (#161), сохранённый черновик — собран заново", async () => {
      openMonths = ["2026-09"];
      archive = [];
      marked = [september, septemberTwo];
      const user = userEvent.setup();
      render(<DigestPage onUnauthorized={() => undefined} showToast={() => undefined} />);

      const frame = await screen.findByTitle("Предпросмотр выпуска в PDF");
      await waitFor(() => expect(frame).toHaveAttribute("src", "blob:pdf-1#navpanes=0&view=FitH"));
      // Один PDF на открытие — за выпуск по умолчанию, а не сначала «за все месяцы».
      expect(pdfRequests()).toHaveLength(1);
      expect(pdfRequests()[0]).toContain("month=2026-09");

      // Порядок в PDF считается сам: перестановка в очереди меняет письмо и DOCX, а PDF устаревшим не помечает.
      await user.click((await screen.findAllByRole("button", { name: "Опустить материал ниже" }))[0]);
      expect(screen.getByText(/В PDF порядок считается сам: раздел, рубрика, балл/)).toBeInTheDocument();
      expect(screen.queryByText(/Состав выпуска изменён, но не сохранён/)).not.toBeInTheDocument();
      // «Из выпуска» меняет состав черновика, а с ним и PDF после сохранения: до сохранения PDF помечен устаревшим.
      await user.click(screen.getAllByRole("button", { name: "Из выпуска" })[0]);
      expect(await screen.findByText(/Состав выпуска изменён, но не сохранён/)).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Сохранить черновик" }));

      await waitFor(() => expect(pdfRequests()).toHaveLength(2));
      await waitFor(() => expect(screen.getByTitle("Предпросмотр выпуска в PDF")).toHaveAttribute("src", "blob:pdf-2#navpanes=0&view=FitH"));
      expect(screen.queryByText(/Состав выпуска изменён, но не сохранён/)).not.toBeInTheDocument();
    });

    it("«Обновить PDF» собирает заново мимо памяти предпросмотра, открытие выпуска — через неё (LOW-2 ревью PR #155)", async () => {
      openMonths = ["2026-09"];
      archive = [];
      marked = [september];
      const user = userEvent.setup();
      render(<DigestPage onUnauthorized={() => undefined} showToast={() => undefined} />);

      const frame = await screen.findByTitle("Предпросмотр выпуска в PDF");
      await waitFor(() => expect(frame).toHaveAttribute("src", "blob:pdf-1#navpanes=0&view=FitH"));
      expect(pdfRequests()[0]).not.toContain("fresh");

      await user.click(screen.getByRole("button", { name: "Обновить PDF" }));
      await waitFor(() => expect(pdfRequests()).toHaveLength(2));
      expect(pdfRequests()[1]).toContain("fresh=1");
      await waitFor(() => expect(screen.getByTitle("Предпросмотр выпуска в PDF")).toHaveAttribute("src", "blob:pdf-2#navpanes=0&view=FitH"));
    });
  });
});
