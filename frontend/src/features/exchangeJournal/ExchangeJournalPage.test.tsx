import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ExchangeJournal, ExchangeReadiness } from "../../api/exchangeJournal";
import { ExchangeJournalPage } from "./ExchangeJournalPage";

// Вкладка «Журнал обмена» (#198, спека #197), шов 3: экран на подставном API. Пакеты для потребителя выпуска (вид,
// номер, месяц, состав со ссылками на карточки витрины, собран или забран), дозагрузка с ответом сервера, готовность
// выпуска месяца и «Отправить как есть» (#199).

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const readiness: ExchangeReadiness = {
  month: "2026-11", closed: false, release: null, total: 3, ready: 2,
  not_ready: [{ signal_id: 812, kind: "article", id: 4501, title: "Контракт на ГРП в Западной Сибири", category: "business",
    missing: ["translation", "card"] }],
  as_is: { allowed: false,
    reason: "Выпуск за ноябрь 2026 ещё открыт: отправить его как есть можно после закрытия месяца (11-го числа следующего)." },
};

const october: ExchangeReadiness = {
  month: "2026-10", closed: true, release: null, total: 2, ready: 1,
  not_ready: [{ signal_id: 640, kind: "signal", id: 640, title: "Пилот роботизированной буровой", category: "technology",
    missing: ["scores"] }],
  as_is: { allowed: true, reason: null },
};

const journal: ExchangeJournal = {
  readiness,
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

function serve(answer: (url: string, init?: RequestInit) => Response = () => json({}),
  months: Record<string, ExchangeReadiness> = { "2026-10": october }) {
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    if ((init?.method ?? "GET") === "POST") return Promise.resolve(answer(url, init));
    if (url.startsWith("/api/exchange-journal/readiness")) {
      const month = new URL(url, "http://test").searchParams.get("month") ?? "";
      return Promise.resolve(json(months[month] ?? { ...readiness, month, total: 0, ready: 0, not_ready: [] }));
    }
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

  it("готовность выпуска месяца: сколько готово, неготовые со ссылками на карточки и чего им не хватает", async () => {
    serve();
    renderPage();

    const block = await screen.findByRole("region", { name: "Готовность выпуска" });
    expect(within(block).getByText("Выпуск за ноябрь 2026: готово 2 из 3.")).toBeInTheDocument();
    expect(within(block).getByRole("link", { name: "№812 Контракт на ГРП в Западной Сибири" }))
      .toHaveAttribute("href", "?screen=business-feed&q=%23812");
    expect(within(block).getByText("нет перевода заголовка, нет карточки")).toBeInTheDocument();
    // Месяц открыт: отправить как есть нельзя, причина словами сервера.
    expect(within(block).getByRole("button", { name: "Отправить как есть" })).toBeDisabled();
    expect(within(block).getByText(/ещё открыт: отправить его как есть можно после закрытия месяца/)).toBeInTheDocument();
  });

  it("«Отправить как есть» закрытого месяца спрашивает подтверждение и уходит запросом с месяцем", async () => {
    serve(() => json({
      package: 4, month: "2026-10", sent: [640, 641], not_ready: [{ signal_id: 640, missing: ["scores"] }],
      message: "Пакет №4: выпуск за октябрь 2026, карточек 2; отправлен как есть, не готовы 1 из 2: №640 (нет баллов).",
    }));
    renderPage();
    const block = await screen.findByRole("region", { name: "Готовность выпуска" });

    fireEvent.change(within(block).getByLabelText("Месяц"), { target: { value: "2026-10" } });
    fireEvent.click(within(block).getByRole("button", { name: "Показать" }));
    expect(await within(block).findByText("Выпуск за октябрь 2026: готово 1 из 2.")).toBeInTheDocument();
    expect(within(block).getByText("нет баллов")).toBeInTheDocument();

    fireEvent.click(within(block).getByRole("button", { name: "Отправить как есть" }));
    const dialog = await screen.findByRole("dialog", { name: "Отправить выпуск как есть" });
    expect(within(dialog).getByText(/с тем, что есть \(не готово: 1 из 2\)/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, call]) => call?.method === "POST")).toBe(false);
    fireEvent.click(within(dialog).getByRole("button", { name: "Отправить" }));

    expect(await within(block).findByRole("status")).toHaveTextContent("Пакет №4: выпуск за октябрь 2026, карточек 2");
    const [url, init] = fetchMock.mock.calls.find(([, call]) => call?.method === "POST")!;
    expect(url).toBe("/api/exchange-journal/releases");
    expect(JSON.parse(String(init.body))).toEqual({ month: "2026-10" });
    // После отправки журнал перечитан.
    await waitFor(() => expect(fetchMock.mock.calls.filter(([one]) => one === "/api/exchange-journal")).toHaveLength(2));
  });

  it("отказ «Отправить как есть» показывает причину сервера", async () => {
    serve(() => json({ detail: "Выпуск за октябрь 2026 уже собран: пакет №4." }, 409));
    renderPage();
    const block = await screen.findByRole("region", { name: "Готовность выпуска" });
    fireEvent.change(within(block).getByLabelText("Месяц"), { target: { value: "2026-10" } });
    fireEvent.click(within(block).getByRole("button", { name: "Показать" }));
    await within(block).findByText("Выпуск за октябрь 2026: готово 1 из 2.");

    fireEvent.click(within(block).getByRole("button", { name: "Отправить как есть" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Отправить выпуск как есть" }))
      .getByRole("button", { name: "Отправить" }));
    expect(await within(block).findByRole("alert")).toHaveTextContent("Выпуск за октябрь 2026 уже собран: пакет №4.");
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


describe("вкладка «Журнал обмена»: обновление, квитанции и отклики (#200)", () => {
  const withExchange: ExchangeJournal = {
    ...journal,
    settings: { ...journal.settings, decryption_keys: ["1122334455667788"], consumer_signing_keys: [],
      reaction_problems: ["нет открытого ключа подписи потребителя (EXCHANGE_CONSUMER_SIGNING_KEYS)"] },
    packages: [
      {
        number: 3, kind: "update", month: null, built_at: "2026-11-21T21:30:00+00:00", built_by: "планировщик",
        first_taken_at: "2026-11-21T21:31:00+00:00", last_taken_at: "2026-11-21T21:31:00+00:00", overdue: false,
        receipt: { outcome: "accepted", message: "Принято: пакет №3, обновление. Склеено 1, снято 1.",
          at: "2026-11-21T21:31:05+00:00" },
        notes: ["№304 ушёл склейкой в №303", "№290 снят: в архиве"], cards: [],
      },
      { ...journal.packages[0], receipt: { outcome: "rejected", message: "Отклонено: подпись не сходится.",
        at: "2026-11-19T06:01:10+00:00" } },
      { ...journal.packages[1], overdue: true },
    ],
    refusals: [{ kind: "reactions", number: 5, reason: "Отклонено: подпись не сходится.", attempts: 12,
      first_at: "2026-11-21T08:00:00+00:00", last_at: "2026-11-21T08:11:00+00:00" }],
    reaction_packages: [{ number: 4, source: "Тестовый потребитель", signer: "abcdefabcdefabcd", reactions: 7,
      answer: "Принято: пакет откликов №4, откликов 7.", received_at: "2026-11-21T07:00:00+00:00",
      last_received_at: "2026-11-21T07:01:00+00:00", repeats: 1 }],
    updates: [{ trigger: "schedule", package_number: 3, author: "планировщик",
      note: "пакет №3: обновление, карточек 0, склеек 1, снятий 1", started_at: "2026-11-21T21:30:00+00:00" }],
    sent_states: [{ signal_id: 304, state: "merged", state_label: "ушёл склейкой в №303 пакетом №3", merged_into: 303,
      state_package: 3, state_at: "2026-11-21T21:30:00+00:00", title: "Пилот роботизированной буровой",
      category: "technology" }],
  };

  function serveExchange(answer: (url: string, init?: RequestInit) => Response = () => json({})) {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "POST") return Promise.resolve(answer(url, init));
      if (url.startsWith("/api/exchange-journal/readiness")) return Promise.resolve(json(readiness));
      return Promise.resolve(json(withExchange));
    });
  }

  it("пакет показывает квитанцию потребителя, отклонённый выделен с ответом, незабранный сутки помечен", async () => {
    serveExchange();
    renderPage();

    const update = await screen.findByRole("article", { name: "Обновление №3" });
    expect(within(update).getByText("принят")).toBeInTheDocument();
    expect(within(update).getByLabelText("Квитанция на пакет №3")).toHaveTextContent("Склеено 1, снято 1.");
    expect(within(update).getByText("Карточек нет: только склейки и снятия.")).toBeInTheDocument();
    expect(within(update).getByText("№304 ушёл склейкой в №303")).toBeInTheDocument();
    const rejected = screen.getByRole("article", { name: "Дозагрузка №2 · сентябрь 2026" });
    expect(rejected).toHaveClass("journalEntryRefused");
    expect(within(rejected).getByText("отклонён")).toBeInTheDocument();
    expect(within(rejected).getByRole("alert")).toHaveTextContent("Отклонено: подпись не сходится.");
    const release = screen.getByRole("article", { name: "Выпуск №1 · сентябрь 2026" });
    expect(within(release).getByText("не забран больше суток")).toBeInTheDocument();
    expect(screen.getByText(/Отклики не примутся: нет открытого ключа подписи потребителя/)).toBeInTheDocument();
  });

  it("пакеты откликов с ответом платформы и отказы точки с причиной и попытками", async () => {
    serveExchange();
    renderPage();

    const panel = await screen.findByRole("region", { name: "Пакеты откликов" });
    expect(within(panel).getByText(/Пакет откликов №4 · Тестовый потребитель · откликов 7/)).toHaveTextContent(
      "Принято: пакет откликов №4, откликов 7.");
    expect(within(panel).getByLabelText("Отказы точки обмена")).toHaveTextContent(
      "Пакет откликов №5: Отклонено: подпись не сходится. Попыток 12");
  });

  it("«Собрать обновление сейчас» уходит запросом, показывает ответ и перечитывает журнал", async () => {
    serveExchange(() => json({ package: 6, message: "Пакет №6: обновление, карточек 2, склеек 0, снятий 0." }));
    renderPage();
    const panel = await screen.findByRole("region", { name: "Обновление" });
    expect(within(panel).getByText(/шаг раз в сутки: пакет №3: обновление/)).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "№304 Пилот роботизированной буровой" }))
      .toHaveAttribute("href", "?screen=signal-radar&q=%23304");
    expect(within(panel).getByText("ушёл склейкой в №303 пакетом №3")).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole("button", { name: "Собрать обновление сейчас" }));

    expect(await within(panel).findByRole("status")).toHaveTextContent("Пакет №6: обновление, карточек 2");
    const [url] = fetchMock.mock.calls.find(([, call]) => call?.method === "POST")!;
    expect(url).toBe("/api/exchange-journal/updates");
    await waitFor(() => expect(fetchMock.mock.calls.filter(([one]) => one === "/api/exchange-journal")).toHaveLength(2));
  });

  it("фильтры пакетов по виду и состоянию", async () => {
    serveExchange();
    renderPage();
    await screen.findByRole("article", { name: "Обновление №3" });
    const names = () => screen.getAllByRole("article").map((one) => one.getAttribute("aria-label"));

    fireEvent.change(screen.getByLabelText("Вид"), { target: { value: "update" } });
    expect(names()).toEqual(["Обновление №3"]);
    fireEvent.change(screen.getByLabelText("Вид"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Состояние"), { target: { value: "rejected" } });
    expect(names()).toEqual(["Дозагрузка №2 · сентябрь 2026"]);
    fireEvent.change(screen.getByLabelText("Состояние"), { target: { value: "built" } });
    expect(names()).toEqual(["Выпуск №1 · сентябрь 2026"]);
    fireEvent.change(screen.getByLabelText("Вид"), { target: { value: "update" } });
    expect(screen.queryAllByRole("article")).toEqual([]);
    expect(screen.getByText("Пакетов такого вида и состояния нет.")).toBeInTheDocument();
  });

  it("отказ сборки обновления показан у кнопки", async () => {
    serveExchange(() => json({ detail: "Пакет не собран: нет ключа подписи платформы (EXCHANGE_SIGNING_KEYS)." }, 409));
    renderPage();
    const panel = await screen.findByRole("region", { name: "Обновление" });

    fireEvent.click(within(panel).getByRole("button", { name: "Собрать обновление сейчас" }));

    expect(await within(panel).findByRole("alert")).toHaveTextContent("нет ключа подписи платформы");
  });
});
