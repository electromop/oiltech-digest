import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ExchangeJournal } from "../../api/exchangeJournal";
import { ExchangeJournalPage } from "./ExchangeJournalPage";

// Вкладка «Журнал обмена» (#198, спека #197), шов 3: экран на подставном API. Пакеты для потребителя выпуска (вид,
// номер, месяц, состав со ссылками на карточки витрины, собран или забран) и дозагрузка с ответом сервера.

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const journal: ExchangeJournal = {
  settings: {
    enabled: true, first_month: "2026-11", signing_keys: ["a1b2c3d4e5f60718"], consumer_keys: ["0f1e2d3c4b5a6978"],
    addresses: 1, token: true, problems: [], point_problems: [],
  },
  packages: [
    {
      number: 2, kind: "addition", month: "2026-09", built_at: "2026-11-19T06:00:00+00:00", built_by: "admin@example.test",
      first_taken_at: "2026-11-19T06:01:00+00:00", last_taken_at: "2026-11-19T06:02:00+00:00",
      notes: ["Дубль №304 ушёл корнем склейки №303"],
      cards: [{ signal_id: 303, title: "Пилот роботизированной буровой", category: "technology" }],
    },
    {
      number: 1, kind: "release", month: "2026-09", built_at: "2026-10-11T00:30:00+00:00", built_by: "планировщик",
      first_taken_at: null, last_taken_at: null, notes: [],
      cards: [{ signal_id: 501, title: "Контракт на бурение 40 скважин", category: "business" }],
    },
  ],
};

const fetchMock = vi.fn();

function serve(answer: (url: string, init?: RequestInit) => Response = () => json({})) {
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    if ((init?.method ?? "GET") === "POST") return Promise.resolve(answer(url, init));
    return Promise.resolve(json(journal));
  });
}

function renderPage() {
  render(<ExchangeJournalPage onUnauthorized={() => undefined} showToast={() => undefined} />);
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetchMock.mockReset();
});

describe("вкладка «Журнал обмена»", () => {
  it("показывает пакеты: вид, номер, месяц, собран или забран, состав со ссылками на карточки витрины", async () => {
    serve();
    renderPage();

    const release = await screen.findByRole("article", { name: "Выпуск №1 · сентябрь 2026" });
    expect(within(release).getByText("собран")).toBeInTheDocument();
    expect(within(release).getByText("Потребитель ещё не забирал")).toBeInTheDocument();
    expect(within(release).getByRole("link", { name: "№501 Контракт на бурение 40 скважин" }))
      .toHaveAttribute("href", "?screen=business-feed&q=%23501");
    const addition = screen.getByRole("article", { name: "Дозагрузка №2 · сентябрь 2026" });
    expect(within(addition).getByText("забран")).toBeInTheDocument();
    expect(within(addition).getByText(/^Забран 19\.11\.2026/)).toBeInTheDocument();
    expect(within(addition).getByRole("link", { name: "№303 Пилот роботизированной буровой" }))
      .toHaveAttribute("href", "?screen=signal-radar&q=%23303");
    // Что сборка сделала сверх состава, строками сервера.
    expect(within(addition).getByText("Дубль №304 ушёл корнем склейки №303")).toBeInTheDocument();
    expect(within(release).queryByLabelText("Сборка пакета №1")).not.toBeInTheDocument();
    // Новые сверху, как отдал сервер.
    expect(screen.getAllByRole("article").map((one) => one.getAttribute("aria-label")))
      .toEqual(["Дозагрузка №2 · сентябрь 2026", "Выпуск №1 · сентябрь 2026"]);
    expect(screen.getByText(/шаг включён, начиная с выпуска за ноябрь 2026/)).toBeInTheDocument();
  });

  it("дозагрузка уходит запросом и показывает ответ сервера: что ушло и что пропущено", async () => {
    serve(() => json({
      package: 3, sent: [700], skipped: [{ signal_id: 303, package: 2, month: "2026-09" }], moved: [],
      message: "Пакет №3: дозагрузка к выпуску за сентябрь 2026, карточек 1; №303 уже отправлен: пакет №2.",
    }));
    renderPage();
    await screen.findByRole("article", { name: "Выпуск №1 · сентябрь 2026" });

    fireEvent.change(screen.getByLabelText("Месяц выпуска"), { target: { value: "2026-09" } });
    fireEvent.change(screen.getByLabelText("Номера сигналов"), { target: { value: "700, 303" } });
    fireEvent.click(screen.getByRole("button", { name: "Собрать дозагрузку" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Пакет №3: дозагрузка к выпуску за сентябрь 2026");
    const [, init] = fetchMock.mock.calls.find(([, call]) => call?.method === "POST")!;
    expect(JSON.parse(String(init.body))).toEqual({ month: "2026-09", numbers: [700, 303] });
    // После дозагрузки журнал перечитан.
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => url === "/api/exchange-journal")).toHaveLength(2));
  });

  it("отказ сервера показывает список номеров, а неверный ввод не уходит запросом", async () => {
    serve(() => json({ detail: "Дозагрузка не собрана, нет таких сигналов: 999999; не действуют: 12 (в архиве)." }, 409));
    renderPage();
    await screen.findByRole("article", { name: "Выпуск №1 · сентябрь 2026" });

    fireEvent.change(screen.getByLabelText("Месяц выпуска"), { target: { value: "сентябрь" } });
    fireEvent.change(screen.getByLabelText("Номера сигналов"), { target: { value: "12" } });
    fireEvent.click(screen.getByRole("button", { name: "Собрать дозагрузку" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Месяц выпуска задаётся как ГГГГ-ММ");
    expect(fetchMock.mock.calls.some(([, call]) => call?.method === "POST")).toBe(false);

    fireEvent.change(screen.getByLabelText("Месяц выпуска"), { target: { value: "2026-09" } });
    fireEvent.change(screen.getByLabelText("Номера сигналов"), { target: { value: "999999 12" } });
    fireEvent.click(screen.getByRole("button", { name: "Собрать дозагрузку" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(
      "Дозагрузка не собрана, нет таких сигналов: 999999; не действуют: 12 (в архиве)."));
  });

  it("участнику без прав администратора вкладка отвечает отказом сервера", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(json({ detail: "Требуются права администратора" }, 403)));
    renderPage();

    expect(await screen.findByText("Требуются права администратора")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Собрать дозагрузку" })).not.toBeInTheDocument();
  });

  it("незаданные ключи и хэш токена видны предупреждениями", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(json({
      ...journal, packages: [],
      settings: { ...journal.settings, enabled: false, signing_keys: [], problems: ["нет ключа подписи платформы (EXCHANGE_SIGNING_KEYS)"],
        point_problems: ["нет хэша токена (EXCHANGE_TOKEN_HASH: SHA-256 токена в hex, 64 знака, не сам токен)"] },
    })));
    renderPage();

    const alerts = await screen.findAllByRole("alert");
    expect(alerts.map((one) => one.textContent)).toEqual([
      "Пакеты не соберутся: нет ключа подписи платформы (EXCHANGE_SIGNING_KEYS).",
      "Потребитель не заберёт пакеты: нет хэша токена (EXCHANGE_TOKEN_HASH: SHA-256 токена в hex, 64 знака, не сам токен).",
    ]);
    expect(screen.getByText("Пакетов для потребителя ещё нет.")).toBeInTheDocument();
    expect(screen.getByText(/шаг выключен/)).toBeInTheDocument();
  });
});
