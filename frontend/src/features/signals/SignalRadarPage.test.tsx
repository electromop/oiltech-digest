import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  getSignalSearchHealth,
  getSignalSummary,
  listSignals,
  type SignalQuery,
  type SignalSearchHealth,
} from "../../api/signals";
import type { Signal } from "../../api/types";
import { RADAR_PAGE_SIZE, SignalRadarPage } from "./SignalRadarPage";

const baseSignal: Signal = {
  id: 107,
  signal_key: "k107",
  title: "MethaneSAT lost contact",
  title_ru: "Потеря связи со спутником MethaneSAT",
  theme: "Экология",
  summary: "Спутник мониторинга метана перестал выходить на связь.",
  thesis: null,
  transferability: "Спутниковый мониторинг утечек",
  maturity: "watch",
  confidence: 0.7,
  score: 72,
  why_now: "Сбой единственного открытого спутника",
  why_not_noise: null,
  companies_json: ["EDF / MethaneSAT"],
  industries_json: [],
  evidence_count: 2,
  first_seen_at: null,
  last_seen_at: null,
  created_at: null,
  updated_at: null,
  evidence: [],
};

vi.mock("../../api/signals", () => ({
  listSignals: vi.fn(),
  getSignalSummary: vi.fn(),
  updateSignal: vi.fn(),
  createSignalFeedback: vi.fn(),
  getSignalSearchHealth: vi.fn(),
}));

const DEFAULT_CARDS: Signal[] = [
  { ...baseSignal, interest_score: 91.4, why_interesting: "Единственный открытый источник данных по метану" },
  { ...baseSignal, id: 3, signal_key: "k3", title_ru: "Карточка без ревью пачки" },
];

// Сервер радара в миниатюре: выборка, страница и числа над списком считаются «на сервере»
// по всем карточкам, а не по тем, что экран уже загрузил.
function matches(card: Signal, query: SignalQuery) {
  const q = (query.q || "").toLowerCase();
  return (
    (!query.theme || card.theme === query.theme)
    && (!query.maturity || card.maturity === query.maturity)
    && (!q || `${card.title_ru || ""} ${card.title}`.toLowerCase().includes(q))
  );
}

function serve(cards: Signal[]) {
  vi.mocked(listSignals).mockImplementation(async (query: SignalQuery = {}) => {
    const offset = query.offset ?? 0;
    return cards.filter((card) => matches(card, query)).slice(offset, offset + (query.limit ?? 100));
  });
  vi.mocked(getSignalSummary).mockImplementation(async (query: SignalQuery = {}) => {
    const topics = cards.filter((card) => card.theme_is_topic !== false).map((card) => card.theme);
    return {
      total: cards.length,
      matching: cards.filter((card) => matches(card, query)).length,
      themes: [...new Set(topics)].sort().map((theme) => ({ theme, count: topics.filter((item) => item === theme).length })),
    };
  });
}

function lastListQuery(): SignalQuery {
  return vi.mocked(listSignals).mock.lastCall?.[0] ?? {};
}

// Ежедневный прогон 27.09: крон 07:15 МСК = 04:15 UTC, Brave 402 во всех темах.
const failedEverywhere: SignalSearchHealth = {
  run_id: 31,
  job_id: 5120,
  run_at: "2026-09-27T04:15:02+00:00",
  status: "failed",
  signals: 0,
  topics: 13,
  failed: 13,
  first_error: "HTTP 402 Usage limit exceeded",
  http_status: 402,
  provider: "brave",
  cause: "http",
};

const RUN = "Прогон 27.09 в 07:15:";
// Каждая причина — по-русски; поиск, который не вызывался, «не выполнен», а не «не ответил».
const CAUSES: Array<[string, Partial<SignalSearchHealth>, string]> = [
  [
    "нет ключа",
    { cause: "not_configured", http_status: null, first_error: "BRAVE_SEARCH_API_KEY is empty" },
    `${RUN} поиск не выполнен в 13 из 13 тем (поиск не настроен — нет ключа). Новых сигналов нет.`,
  ],
  [
    "провайдер не подключён",
    { cause: "not_configured", provider: "none", http_status: null, first_error: "search provider is not connected yet" },
    `${RUN} поиск не выполнен в 13 из 13 тем (провайдер поиска не подключён). Новых сигналов нет.`,
  ],
  [
    "неизвестный провайдер — текст сервера",
    {
      cause: "unsupported_provider",
      provider: "brvae",
      http_status: null,
      first_error: "unsupported SOURCE_DISCOVERY_SEARCH_PROVIDER=brvae",
    },
    `${RUN} поиск не выполнен в 13 из 13 тем (unsupported SOURCE_DISCOVERY_SEARCH_PROVIDER=brvae). Новых сигналов нет.`,
  ],
  [
    "429",
    { http_status: 429, first_error: "HTTP 429 Request rate limit exceeded for plan" },
    `${RUN} поиск не ответил в 13 из 13 тем (HTTP 429 — превышен лимит запросов к поиску). Новых сигналов нет.`,
  ],
  [
    "5xx",
    { http_status: 503, first_error: "HTTP 503 Service Unavailable" },
    `${RUN} поиск не ответил в 13 из 13 тем (HTTP 503 — сервис поиска недоступен). Новых сигналов нет.`,
  ],
  [
    "таймаут или обрыв соединения",
    {
      cause: "network",
      http_status: null,
      first_error: "HTTPSConnectionPool(host='api.search.brave.com', port=443): Read timed out. (read timeout=20)",
    },
    `${RUN} поиск не ответил в 13 из 13 тем (поиск не ответил вовремя). Новых сигналов нет.`,
  ],
  [
    "TLS или прокси",
    {
      cause: "connection",
      http_status: null,
      first_error: "HTTPSConnectionPool(host='api.search.brave.com', port=443): Max retries exceeded (SSLError)",
    },
    `${RUN} поиск не ответил в 13 из 13 тем (нет соединения с поиском). Новых сигналов нет.`,
  ],
  [
    "прочая ошибка HTTP — текст сервера",
    { http_status: 404, first_error: "HTTP 404 Not Found" },
    `${RUN} поиск не ответил в 13 из 13 тем (HTTP 404 Not Found). Новых сигналов нет.`,
  ],
  [
    "402 не у Brave — текст сервера",
    { provider: "serpapi", first_error: "HTTP 402 Payment Required" },
    `${RUN} поиск не ответил в 13 из 13 тем (HTTP 402 Payment Required). Новых сигналов нет.`,
  ],
  [
    "прочая ошибка без кода — текст сервера",
    { cause: "other", http_status: null, first_error: "Expecting value: line 1 column 1 (char 0)" },
    `${RUN} поиск не ответил в 13 из 13 тем (Expecting value: line 1 column 1 (char 0)). Новых сигналов нет.`,
  ],
];

function renderRadar(isAdmin: boolean) {
  render(<SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} isAdmin={isAdmin} />);
}

describe("SignalRadarPage", () => {
  beforeEach(() => {
    vi.mocked(getSignalSearchHealth).mockReset();
    vi.mocked(listSignals).mockReset();
    vi.mocked(getSignalSummary).mockReset();
    serve(DEFAULT_CARDS);
  });

  it("показывает «почему интересно» только там, где ревью пачки его дало", async () => {
    render(<SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} />);

    expect(await screen.findByText("Единственный открытый источник данных по метану")).toBeInTheDocument();
    expect(screen.getAllByText(/Почему интересно/)).toHaveLength(1);
    expect(screen.getByText(/Почему интересно · 91/)).toBeInTheDocument();
  });

  it("админ видит плашку: поиск не ответил во всех темах", async () => {
    vi.mocked(getSignalSearchHealth).mockResolvedValue({ search_health: failedEverywhere });

    renderRadar(true);

    expect(
      await screen.findByText(
        "Прогон 27.09 в 07:15: поиск не ответил в 13 из 13 тем (HTTP 402 — исчерпан месячный лимит поиска Brave). Новых сигналов нет.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveClass("archiveNotice");
  });

  it.each(CAUSES)("причина по-русски: %s", async (_cause, patch, text) => {
    vi.mocked(getSignalSearchHealth).mockResolvedValue({ search_health: { ...failedEverywhere, ...patch } });

    renderRadar(true);

    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it("админ видит плашку и при частичном сбое", async () => {
    vi.mocked(getSignalSearchHealth).mockResolvedValue({
      search_health: { ...failedEverywhere, status: "ok", failed: 3, signals: 4 },
    });

    renderRadar(true);

    expect(
      await screen.findByText(
        "Прогон 27.09 в 07:15: поиск не ответил в 3 из 13 тем (HTTP 402 — исчерпан месячный лимит поиска Brave).",
      ),
    ).toBeInTheDocument();
  });

  it("обычный пользователь плашку не видит и здоровье поиска не запрашивает", async () => {
    vi.mocked(getSignalSearchHealth).mockResolvedValue({ search_health: failedEverywhere });

    renderRadar(false);

    expect(await screen.findByText("Карточка без ревью пачки")).toBeInTheDocument();
    expect(getSignalSearchHealth).not.toHaveBeenCalled();
    expect(screen.queryByText(/поиск не ответил/)).not.toBeInTheDocument();
  });

  it("после успешного прогона плашки нет", async () => {
    vi.mocked(getSignalSearchHealth).mockResolvedValue({
      search_health: { ...failedEverywhere, status: "ok", failed: 0, signals: 9, first_error: null, http_status: null },
    });

    renderRadar(true);

    expect(await screen.findByText("Карточка без ревью пачки")).toBeInTheDocument();
    expect(getSignalSearchHealth).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/поиск не ответил/)).not.toBeInTheDocument();
  });

  describe("экран для коллег заказчика", () => {
    const drilling = {
      ...baseSignal,
      id: 21,
      signal_key: "k21",
      theme: "Бурение",
      title_ru: "Роботизированная буровая установка",
      score: 80,
      first_seen_at: "2026-09-25T09:00:00Z",
      evidence: [
        { id: 1, signal_id: 21, article_id: null, source_url: "https://worldoil.com/a", title: "Robotic rig",
          title_ru: null, publisher: "worldoil.com", published_at: null } as never,
      ],
    };
    const ecology = { ...baseSignal, id: 22, signal_key: "k22", theme: "Экология", title_ru: "Спутник MethaneSAT", score: 60 };

    beforeEach(() => {
      serve([drilling, ecology]);
    });

    it("сигналы — блоками по темам, как бизнес-сигналы: свёрнуты, раскрываются", async () => {
      renderRadar(false);

      expect(await screen.findByRole("button", { name: "Раскрыть группу Бурение" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Раскрыть группу Экология" })).toBeInTheDocument();
      expect(screen.queryByText("Роботизированная буровая установка")).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Развернуть всё" }));

      expect(screen.getByText("Роботизированная буровая установка")).toBeInTheDocument();
      expect(screen.getByText("Спутник MethaneSAT")).toBeInTheDocument();
    });

    it("обычный пользователь оставляет ОС, в оценках есть «Не тот блок»", async () => {
      renderRadar(false);
      fireEvent.click(await screen.findByRole("button", { name: "Раскрыть группу Бурение" }));

      fireEvent.click(screen.getAllByRole("button", { name: "Обратная связь" })[0]);

      expect(
        screen.getByRole("option", { name: "Не тот блок — бизнес-сигнал, а не технология" }),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Сохранить" })).toBeInTheDocument();
    });

    it("ранние карточки со свободной темой — одним блоком последним, не в фильтре тем", async () => {
      serve([
        drilling,
        { ...ecology, id: 31, signal_key: "k31", theme: "HSE/бурение", theme_is_topic: false, title_ru: "Ранняя 1" },
        { ...ecology, id: 32, signal_key: "k32", theme: "R&D / добыча лития", theme_is_topic: false, title_ru: "Ранняя 2" },
      ]);
      renderRadar(false);

      const early = await screen.findByRole("button", { name: "Раскрыть группу Ранние карточки — тема вне 13 тематик" });
      const groups = screen.getAllByRole("button", { name: /^Раскрыть группу/ });
      expect(groups[groups.length - 1]).toBe(early);
      expect(await screen.findByRole("option", { name: "Бурение" })).toBeInTheDocument();
      expect(screen.queryByRole("option", { name: "HSE/бурение" })).not.toBeInTheDocument();
    });

    it("блок, раскрытый поиском, всё равно сворачивается кнопкой", async () => {
      renderRadar(false);
      await screen.findByRole("button", { name: "Раскрыть группу Бурение" });

      fireEvent.change(screen.getByRole("textbox", { name: /^Поиск/ }), { target: { value: "буровая" } });
      expect(screen.getByText("Роботизированная буровая установка")).toBeInTheDocument();
      await waitFor(() => expect(lastListQuery().q).toBe("буровая"));

      fireEvent.click(await screen.findByRole("button", { name: "Свернуть группу Бурение" }));
      expect(screen.queryByText("Роботизированная буровая установка")).not.toBeInTheDocument();
    });

    it("поиск — на сервере по всему радару, с задержкой как у ленты", async () => {
      const deep = { ...drilling, id: 151, signal_key: "k151", title_ru: "Сейсморазведка с дронов", score: 12 };
      serve([drilling, ecology, deep]);
      vi.mocked(listSignals).mockImplementationOnce(async () => [drilling, ecology]);
      renderRadar(false);
      await screen.findByRole("button", { name: "Раскрыть группу Бурение" });

      fireEvent.change(screen.getByRole("textbox", { name: /^Поиск/ }), { target: { value: "дронов" } });
      // Запрос уходит не на каждую букву: сразу после ввода нового запроса ещё нет.
      expect(listSignals).toHaveBeenCalledTimes(1);

      expect(await screen.findByText("Сейсморазведка с дронов")).toBeInTheDocument();
      expect(lastListQuery()).toMatchObject({ q: "дронов", offset: 0 });
      expect(screen.queryByText("Роботизированная буровая установка")).not.toBeInTheDocument();
    });

    it("выбрал тему — видны только её карточки, без «Обновить»", async () => {
      renderRadar(false);
      await screen.findByRole("button", { name: "Раскрыть группу Экология" });

      fireEvent.change(await screen.findByRole("combobox", { name: /^Тема/ }), { target: { value: "Бурение" } });

      await waitFor(() =>
        expect(screen.queryByRole("button", { name: /группу Экология/ })).not.toBeInTheDocument(),
      );
      // Блок выбранной темы раскрыт сам.
      expect(screen.getByText("Роботизированная буровая установка")).toBeInTheDocument();
      expect(lastListQuery()).toMatchObject({ theme: "Бурение" });
      // Список тем не сжимается до выбранной: он — со всего радара, а не с выборки.
      expect(screen.getByRole("option", { name: "Экология" })).toBeInTheDocument();
    });

    it("зрелость тоже применяется сразу", async () => {
      serve([drilling, { ...ecology, maturity: "proven" }]);
      renderRadar(false);
      await screen.findByRole("button", { name: "Раскрыть группу Бурение" });

      fireEvent.change(screen.getByRole("combobox", { name: /^Зрелость/ }), { target: { value: "proven" } });

      await waitFor(() =>
        expect(screen.queryByRole("button", { name: /группу Бурение/ })).not.toBeInTheDocument(),
      );
      expect(lastListQuery()).toMatchObject({ maturity: "proven" });
    });

    it("«Показать ещё» догружает следующую страницу с сервера", async () => {
      const many = Array.from({ length: RADAR_PAGE_SIZE + 1 }, (_, index) => ({
        ...drilling,
        id: 1000 + index,
        signal_key: `k${1000 + index}`,
        title_ru: `Сигнал номер ${index + 1}`,
      }));
      serve(many);
      renderRadar(false);

      fireEvent.click(await screen.findByRole("button", { name: "Показать ещё 1 (осталось 1)" }));

      expect(await screen.findByText(`Сигнал номер ${RADAR_PAGE_SIZE + 1}`)).toBeInTheDocument();
      expect(lastListQuery()).toMatchObject({ offset: RADAR_PAGE_SIZE });
      expect(screen.queryByRole("button", { name: /Показать ещё/ })).not.toBeInTheDocument();
    });

    it("вместо строки издателей — число ссылок и дата поступления", async () => {
      const { container } = render(
        <SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} />,
      );
      fireEvent.click(await screen.findByRole("button", { name: "Раскрыть группу Бурение" }));

      expect(screen.getByText("Поступил: 25.09.2026")).toBeInTheDocument();
      expect(container.querySelector(".signalPublisherChip")).toBeNull();
    });
  });
});
