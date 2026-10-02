import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  createSignalFeedback,
  getSignalSearchHealth,
  getSignalSummary,
  listSignals,
  updateSignal,
  type SignalQuery,
  type SignalSearchHealth,
  type SignalSummary,
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
  const arrival = (card.first_seen_at || "").slice(0, 10);
  return (
    (!query.theme || card.theme === query.theme)
    && (!query.maturity || card.maturity === query.maturity)
    && (!q || `${card.title_ru || ""} ${card.title}`.toLowerCase().includes(q))
    && (query.minScore == null || card.score >= query.minScore)
    && (query.maxScore == null || card.score <= query.maxScore)
    && (!query.since || arrival >= query.since)
    && (!query.until || arrival <= query.until)
  );
}

function ordered(cards: Signal[], sort: SignalQuery["sort"]) {
  const seen = (card: Signal) => card.first_seen_at || "";
  if (sort === "date_desc") return [...cards].sort((a, b) => seen(b).localeCompare(seen(a)));
  if (sort === "score_asc") return [...cards].sort((a, b) => a.score - b.score);
  return [...cards].sort((a, b) => b.score - a.score);
}

type Tiles = Omit<SignalSummary, "matching" | "themes">;

function serve(cards: Signal[], tiles: Partial<Tiles> = {}) {
  // Своя копия: «В дайджест» и отзывы меняют её, а не общие заготовки карточек; ответы —
  // снимки на момент ответа, как у настоящего сервера.
  const state = cards.map((card) => ({ ...card }));
  vi.mocked(listSignals).mockImplementation(async (query: SignalQuery = {}) => {
    const offset = query.offset ?? 0;
    return ordered(state.filter((card) => matches(card, query)), query.sort)
      .slice(offset, offset + (query.limit ?? 100))
      .map((card) => ({ ...card }));
  });
  vi.mocked(getSignalSummary).mockImplementation(async (query: SignalQuery = {}) => {
    const topics = state.filter((card) => card.theme_is_topic !== false).map((card) => card.theme);
    return {
      total: state.length,
      new_7d: 0,
      in_digest: state.filter((card) => card.selected_for_digest).length,
      with_feedback: state.filter((card) => Number(card.feedback_count || 0) > 0).length,
      merged: state.reduce((sum, card) => sum + Number(card.merged_count || 0), 0),
      ...tiles,
      matching: state.filter((card) => matches(card, query)).length,
      themes: [...new Set(topics)].sort().map((theme) => ({ theme, count: topics.filter((item) => item === theme).length })),
    };
  });
  vi.mocked(updateSignal).mockImplementation(async (signalId, patch) => {
    const card = state.find((item) => item.id === signalId);
    if (card && patch.selected_for_digest != null) card.selected_for_digest = patch.selected_for_digest;
    return { ok: true };
  });
  vi.mocked(createSignalFeedback).mockImplementation(async (payload) => {
    const card = state.find((item) => item.id === payload.signal_id);
    if (card) card.feedback_count = Number(card.feedback_count || 0) + 1;
    return { ok: true, event_id: 1, memory_ids: [], memories: 0 };
  });
}

// Ответ, который придёт, когда тест скажет: выборка «в пути».
function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function tileValue(label: string) {
  return screen.getByText(label).closest(".statCardReact")?.querySelector(".statValueReact")?.textContent;
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
    vi.mocked(createSignalFeedback).mockReset();
    vi.mocked(updateSignal).mockReset();
    serve(DEFAULT_CARDS);
  });

  it("показывает «почему интересно» только там, где ревью пачки его дало", async () => {
    render(<SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} />);
    await screen.findByText("Карточка без ревью пачки");
    for (const button of screen.getAllByRole("button", { name: "Раскрыть сигнал" })) fireEvent.click(button);

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
      fireEvent.click(screen.getByRole("button", { name: "Раскрыть сигнал" }));

      fireEvent.click(screen.getByRole("button", { name: "Обратная связь" }));

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

    it("поиск — на сервере по всему радару", async () => {
      const deep = { ...drilling, id: 151, signal_key: "k151", title_ru: "Сейсморазведка с дронов", score: 12 };
      serve([drilling, ecology, deep]);
      vi.mocked(listSignals).mockImplementationOnce(async () => [drilling, ecology]);
      renderRadar(false);
      await screen.findByRole("button", { name: "Раскрыть группу Бурение" });

      // Не длиннее, чем принимает API (q ≤ 200): иначе 422 с сырым текстом ошибки.
      expect(screen.getByRole("textbox", { name: /^Поиск/ })).toHaveAttribute("maxlength", "200");
      fireEvent.change(screen.getByRole("textbox", { name: /^Поиск/ }), { target: { value: "дронов" } });

      expect(await screen.findByText("Сейсморазведка с дронов")).toBeInTheDocument();
      expect(lastListQuery()).toMatchObject({ q: "дронов", offset: 0 });
      expect(screen.queryByText("Роботизированная буровая установка")).not.toBeInTheDocument();
    });

    it("запрос уходит через 400 мс после последней буквы — как у ленты", async () => {
      vi.useFakeTimers();
      try {
        renderRadar(false);
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(listSignals).toHaveBeenCalledTimes(1);
        const input = screen.getByRole("textbox", { name: /^Поиск/ });

        fireEvent.change(input, { target: { value: "бур" } });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(300);
        });
        fireEvent.change(input, { target: { value: "буровая" } });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(399);
        });
        expect(listSignals).toHaveBeenCalledTimes(1);

        await act(async () => {
          await vi.advanceTimersByTimeAsync(1);
        });
        // Одна выборка на всё слово, а не на каждую букву.
        expect(listSignals).toHaveBeenCalledTimes(2);
        expect(lastListQuery()).toMatchObject({ q: "буровая" });
      } finally {
        vi.useRealTimers();
      }
    });

    it("фильтр сменили, пока сохранялся отзыв «дубль», — список перечитан по новому фильтру", async () => {
      type SaveResult = Awaited<ReturnType<typeof createSignalFeedback>>;
      let finishSave: (result: SaveResult) => void = () => undefined;
      vi.mocked(createSignalFeedback).mockImplementation(
        () => new Promise<SaveResult>((resolve) => {
          finishSave = resolve;
        }),
      );
      renderRadar(false);
      fireEvent.click(await screen.findByRole("button", { name: "Раскрыть группу Бурение" }));
      fireEvent.click(screen.getByRole("button", { name: "Раскрыть сигнал" }));
      fireEvent.click(screen.getByRole("button", { name: "Обратная связь" }));
      fireEvent.change(screen.getByLabelText("ID дубля"), { target: { value: "22" } });
      fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

      fireEvent.change(screen.getByRole("combobox", { name: /^Тема/ }), { target: { value: "Экология" } });
      await waitFor(() => expect(lastListQuery()).toMatchObject({ theme: "Экология" }));
      const callsBefore = vi.mocked(listSignals).mock.calls.length;

      await act(async () => {
        finishSave({ ok: true, event_id: 9, memory_ids: [], memories: 0, merged: true });
      });

      await waitFor(() => expect(vi.mocked(listSignals).mock.calls.length).toBeGreaterThan(callsBefore));
      expect(lastListQuery()).toMatchObject({ theme: "Экология" });
      await waitFor(() =>
        expect(screen.queryByRole("button", { name: /группу Бурение/ })).not.toBeInTheDocument(),
      );
      expect(screen.getByRole("combobox", { name: /^Тема/ })).toHaveValue("Экология");
    });

    it("после склейки дубля догруженное «Показать ещё» не пропадает", async () => {
      serve(Array.from({ length: RADAR_PAGE_SIZE + 1 }, (_, index) => ({
        ...drilling,
        id: 1000 + index,
        signal_key: `k${1000 + index}`,
        title_ru: `Сигнал номер ${index + 1}`,
      })));
      vi.mocked(createSignalFeedback).mockResolvedValue({ ok: true, event_id: 9, memory_ids: [], memories: 0, merged: true });
      renderRadar(false);
      fireEvent.click(await screen.findByRole("button", { name: "Показать ещё 1 (осталось 1)" }));
      await screen.findByText(`Сигнал номер ${RADAR_PAGE_SIZE + 1}`);

      fireEvent.click(screen.getAllByRole("button", { name: "Раскрыть сигнал" })[0]);
      fireEvent.click(screen.getByRole("button", { name: "Обратная связь" }));
      fireEvent.change(screen.getByLabelText("ID дубля"), { target: { value: "1001" } });
      fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

      await waitFor(() => expect(lastListQuery()).toMatchObject({ offset: 0, limit: RADAR_PAGE_SIZE + 1 }));
      expect(await screen.findByText(`Сигнал номер ${RADAR_PAGE_SIZE + 1}`)).toBeInTheDocument();
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

    it("во время новой выборки «Показать ещё» недоступна и не догружает к старому списку", async () => {
      serve(Array.from({ length: RADAR_PAGE_SIZE + 1 }, (_, index) => ({
        ...drilling,
        id: 1000 + index,
        signal_key: `k${1000 + index}`,
        title_ru: `Сигнал номер ${index + 1}`,
      })));
      renderRadar(false);
      const more = await screen.findByRole("button", { name: "Показать ещё 1 (осталось 1)" });

      fireEvent.change(screen.getByRole("textbox", { name: /^Поиск/ }), { target: { value: "номер 10" } });

      expect(more).toBeDisabled();
      fireEvent.click(more);
      expect(vi.mocked(listSignals).mock.calls.some(([query]) => query?.offset === RADAR_PAGE_SIZE)).toBe(false);
      await waitFor(() => expect(lastListQuery()).toMatchObject({ q: "номер 10", offset: 0 }));
    });

    it("«В дайджест», нажатое во время новой выборки, не откатывается её ответом", async () => {
      serve([drilling]);
      renderRadar(false);
      await screen.findByRole("button", { name: "В дайджест" });
      const stale = deferred<Signal[]>();
      vi.mocked(listSignals).mockImplementationOnce(() => stale.promise);
      fireEvent.change(screen.getByRole("combobox", { name: /^Зрелость/ }), { target: { value: "watch" } });
      await waitFor(() => expect(lastListQuery()).toMatchObject({ maturity: "watch" }));

      fireEvent.click(screen.getByRole("button", { name: "В дайджест" }));
      expect(await screen.findByRole("button", { name: "Убрать" })).toBeInTheDocument();
      // Ответ выборки, собранный до отметки, приходит последним.
      await act(async () => {
        stale.resolve([{ ...drilling }]);
      });

      expect(screen.getByRole("button", { name: "Убрать" })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "В дайджест" })).not.toBeInTheDocument();
    });

    it("отзыв, сохранённый во время новой выборки, не откатывается её ответом", async () => {
      serve([drilling]);
      renderRadar(false);
      fireEvent.click(await screen.findByRole("button", { name: "Раскрыть сигнал" }));
      fireEvent.click(screen.getByRole("button", { name: "Обратная связь" }));
      fireEvent.change(screen.getByLabelText("Оценка сигнала"), { target: { value: "approved" } });
      const stale = deferred<Signal[]>();
      vi.mocked(listSignals).mockImplementationOnce(() => stale.promise);
      fireEvent.change(screen.getByRole("combobox", { name: /^Зрелость/ }), { target: { value: "watch" } });
      await waitFor(() => expect(lastListQuery()).toMatchObject({ maturity: "watch" }));

      fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
      expect(await screen.findByText("Обратная связь: 1")).toBeInTheDocument();
      await act(async () => {
        stale.resolve([{ ...drilling }]);
      });

      expect(screen.getByText("Обратная связь: 1")).toBeInTheDocument();
    });

    it("«Сначала новые» — сортировка с сервера, блоки идут в её порядке", async () => {
      const fresh = { ...ecology, first_seen_at: "2026-09-27T09:00:00Z" };
      serve([drilling, fresh]);
      renderRadar(false);
      await screen.findByRole("button", { name: "Раскрыть группу Экология" });
      const groupNames = () =>
        screen.getAllByRole("button", { name: /^Раскрыть группу/ }).map((item) => item.getAttribute("aria-label"));
      expect(groupNames()).toEqual(["Раскрыть группу Бурение", "Раскрыть группу Экология"]);

      fireEvent.change(screen.getByRole("combobox", { name: /^Сортировка/ }), { target: { value: "date_desc" } });

      await waitFor(() => expect(groupNames()).toEqual(["Раскрыть группу Экология", "Раскрыть группу Бурение"]));
      expect(lastListQuery()).toMatchObject({ sort: "date_desc" });
    });

    it("над радаром — плитки, как у «Бизнес-сигналов»: числа с сервера, поиск их не меняет", async () => {
      serve([drilling, ecology], { total: 76, new_7d: 18, in_digest: 3, with_feedback: 12, merged: 5 });
      const { container } = render(
        <SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} />,
      );
      await screen.findByText("Всего сигналов");

      const tiles = {
        "Всего сигналов": "76",
        "Новые за 7 дней": "18",
        "В дайджесте": "3",
        "С обратной связью": "12",
        "Объединено дублей": "5",
      };
      for (const [label, value] of Object.entries(tiles)) expect(tileValue(label)).toBe(value);
      // Строки-сводки «N сигналов · N в дайджесте · обратная связь: N» больше нет.
      expect(container.querySelector(".signalRadarHeaderStats")).toBeNull();

      fireEvent.change(screen.getByRole("textbox", { name: /^Поиск/ }), { target: { value: "буровая" } });
      await waitFor(() => expect(lastListQuery().q).toBe("буровая"));
      expect(await screen.findByText("1 из 76 сигналов")).toBeInTheDocument();
      expect(tileValue("Всего сигналов")).toBe("76");
    });

    it("выбор в дайджест обновляет плитку «В дайджесте»", async () => {
      vi.mocked(updateSignal).mockResolvedValue({ ok: true });
      let inDigest = 0;
      serve([drilling]);
      const counts = vi.mocked(getSignalSummary).getMockImplementation()!;
      vi.mocked(getSignalSummary).mockImplementation(async (query) => ({ ...(await counts(query)), in_digest: inDigest }));
      renderRadar(false);
      await screen.findByText("В дайджесте");
      expect(tileValue("В дайджесте")).toBe("0");

      inDigest = 1;
      fireEvent.click(await screen.findByRole("button", { name: "В дайджест" }));

      await waitFor(() => expect(tileValue("В дайджесте")).toBe("1"));
      expect(updateSignal).toHaveBeenCalledWith(21, { selected_for_digest: true });
    });

    it("в шапке панели — «N из M сигналов» по всему радару", async () => {
      renderRadar(false);
      expect(await screen.findByText("2 из 2 сигналов")).toBeInTheDocument();

      fireEvent.change(await screen.findByRole("combobox", { name: /^Тема/ }), { target: { value: "Экология" } });

      expect(await screen.findByText("1 из 2 сигналов")).toBeInTheDocument();
    });

    it("расширенные фильтры: балл и период «Поступил» уходят на сервер, «Сбросить» возвращает всё", async () => {
      const fresh = { ...ecology, first_seen_at: "2026-09-27T09:00:00Z" };
      serve([drilling, fresh]);
      renderRadar(false);
      await screen.findByRole("button", { name: "Раскрыть группу Экология" });

      fireEvent.change(screen.getByRole("combobox", { name: /^Сортировка/ }), { target: { value: "score_asc" } });
      fireEvent.click(screen.getByRole("button", { name: "Расширенные фильтры" }));
      fireEvent.change(screen.getByRole("spinbutton", { name: "Балл от" }), { target: { value: "70" } });
      await waitFor(() => expect(lastListQuery()).toMatchObject({ minScore: 70, sort: "score_asc" }));
      await waitFor(() =>
        expect(screen.queryByRole("button", { name: /группу Экология/ })).not.toBeInTheDocument(),
      );

      fireEvent.change(screen.getByLabelText("Поступил с"), { target: { value: "2026-09-26" } });
      await waitFor(() => expect(lastListQuery()).toMatchObject({ minScore: 70, since: "2026-09-26" }));
      expect(await screen.findByText("Сигналов по выбранным фильтрам нет.")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Сбросить" }));

      expect(await screen.findByRole("button", { name: /группу Экология/ })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /группу Бурение/ })).toBeInTheDocument();
      const query = lastListQuery();
      expect([query.minScore, query.maxScore, query.since, query.until, query.theme, query.q]).toEqual(
        [undefined, undefined, undefined, undefined, undefined, undefined],
      );
      expect(query.sort ?? "score_desc").toBe("score_desc");
      expect(screen.getByRole("spinbutton", { name: "Балл от" })).toHaveValue(0);
      expect(screen.getByRole("combobox", { name: /^Сортировка/ })).toHaveValue("score_desc");
    });

    it("форма ОС — поля как в остальных формах, «Отмена» и «Сохранить» справа; «Отмена» ничего не отправляет", async () => {
      renderRadar(false);
      fireEvent.click(await screen.findByRole("button", { name: "Раскрыть группу Бурение" }));
      fireEvent.click(screen.getByRole("button", { name: "Раскрыть сигнал" }));
      fireEvent.click(screen.getByRole("button", { name: "Обратная связь" }));

      for (const label of [
        "Оценка сигнала",
        "ID дубля",
        "Обоснование оценки",
        "Рекомендуемый заголовок",
        "Рекомендуемая формулировка сути",
        "Рекомендации AI-агенту",
      ]) {
        expect(screen.getByText(label).closest("label")).toHaveClass("field");
      }
      const buttons = screen.getByRole("button", { name: "Сохранить" }).parentElement!;
      expect(buttons).toHaveClass("signalFeedbackButtons");
      expect([...buttons.querySelectorAll("button")].map((item) => item.textContent)).toEqual(["Отмена", "Сохранить"]);

      fireEvent.change(screen.getByLabelText("Рекомендации AI-агенту"), { target: { value: "искать по-китайски" } });
      fireEvent.click(screen.getByRole("button", { name: "Отмена" }));

      expect(screen.queryByRole("button", { name: "Сохранить" })).not.toBeInTheDocument();
      expect(createSignalFeedback).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Обратная связь" }));
      expect(screen.getByLabelText("Рекомендации AI-агенту")).toHaveValue("");
    });

    it("после «Сохранить» форма закрывается, а счётчик отзывов растёт", async () => {
      vi.mocked(createSignalFeedback).mockResolvedValue({ ok: true, event_id: 7, memory_ids: [], memories: 0 });
      renderRadar(false);
      fireEvent.click(await screen.findByRole("button", { name: "Раскрыть группу Бурение" }));
      fireEvent.click(screen.getByRole("button", { name: "Раскрыть сигнал" }));
      fireEvent.click(screen.getByRole("button", { name: "Обратная связь" }));

      fireEvent.change(screen.getByLabelText("Оценка сигнала"), { target: { value: "strong_signal" } });
      fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

      await waitFor(() => expect(screen.queryByRole("button", { name: "Сохранить" })).not.toBeInTheDocument());
      expect(createSignalFeedback).toHaveBeenCalledWith(expect.objectContaining({ signal_id: 21, verdict: "strong_signal" }));
      expect(screen.getByText("Обратная связь: 1")).toBeInTheDocument();
    });

    it("карточка — свёрнутая строка, как у «Бизнес-сигналов», и раскрывается", async () => {
      const { container } = render(
        <SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} />,
      );
      fireEvent.click(await screen.findByRole("button", { name: "Раскрыть группу Бурение" }));

      // Заголовок — ссылка на первую ссылку-доказательство, под ним — мета карточки.
      const title = screen.getByRole("link", { name: "Роботизированная буровая установка" });
      expect(title).toHaveAttribute("href", "https://worldoil.com/a");
      expect(title).toHaveAttribute("target", "_blank");
      expect(screen.getByText(/^Бурение · 2 ссылки · Зрелость: Наблюдать ·/)).toBeInTheDocument();
      expect(screen.getByText("#21")).toBeInTheDocument();
      // Тело свёрнуто: суть, «Почему сейчас» и ссылки — только по раскрытию.
      expect(screen.queryByText("Спутник мониторинга метана перестал выходить на связь.")).not.toBeInTheDocument();
      expect(screen.queryByText("Почему сейчас")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Обратная связь" })).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Раскрыть сигнал" }));

      expect(screen.getByText("Спутник мониторинга метана перестал выходить на связь.")).toBeInTheDocument();
      expect(screen.getByText("Почему сейчас")).toBeInTheDocument();
      expect(screen.getByText("Переносимость")).toBeInTheDocument();
      expect(screen.getByText("Robotic rig")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Обратная связь" })).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Свернуть сигнал" }));
      expect(screen.queryByText("Почему сейчас")).not.toBeInTheDocument();
      // Надзаголовка «Signal Discovery» больше нет — экран по-русски.
      expect(screen.queryByText("Signal Discovery")).not.toBeInTheDocument();
      expect(container.querySelector(".signalRadarCard")).toBeNull();
    });

    it.each([
      [85, "Высокая", "ok"],
      [70, "Выше средней", "ok"],
      [50, "Средняя", "warn"],
      [20, "Низкая", "bad"],
    ])("балл %s — цветной, со словесной оценкой «%s», как у бизнес-сигналов", async (score, label, tone) => {
      serve([{ ...drilling, score, score_label: label }]);
      renderRadar(false);

      const pill = await screen.findByTitle("Балл судьи радара");
      expect(pill).toHaveTextContent(String(score));
      expect(pill).toHaveClass("miniPill", tone);
      expect(screen.getByText(label, { selector: ".miniPill" })).toHaveClass(tone);
    });

    it("ссылки-доказательства — «издатель · дата публикации», под ними заголовок", async () => {
      const link = (id: number, fields: Record<string, unknown>) =>
        ({ id, signal_id: 21, article_id: null, evidence_type: "article", extracted_fact: null, summary_ru: null,
           strength: 0, raw_payload_json: {}, created_at: null, updated_at: null, title_ru: null, ...fields }) as never;
      serve([{
        ...drilling,
        evidence: [
          // 24.09 08:00 UTC — 11:00 по Москве: дата та же, что и у издателя.
          link(1, { source_url: "https://worldoil.com/a", title: "Robotic rig", title_ru: "Роботизированная буровая",
                    publisher: "worldoil.com", published_at: "2026-09-24T08:00:00Z" }),
          link(2, { source_url: "https://example.com/b", title: "Rig report", publisher: null, published_at: null }),
        ],
      }]);
      renderRadar(false);
      fireEvent.click(await screen.findByRole("button", { name: "Раскрыть сигнал" }));

      const first = screen.getByText("worldoil.com · 24.09.2026").closest("a")!;
      expect(first).toHaveAttribute("href", "https://worldoil.com/a");
      expect(first).toHaveTextContent("Роботизированная буровая");
      // Без издателя — по-русски, без даты — без пустого «·»; тип материала («article») не показываем.
      expect(screen.getByText("источник").closest("a")).toHaveTextContent("Rig report");
      expect(screen.queryByText("source")).not.toBeInTheDocument();
      expect(screen.queryByText("article")).not.toBeInTheDocument();
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

  // Решение владельца 29.09: у карточки закрытого месяца (месяц — по дате поступления на радар)
  // отметку «в дайджест» не ставят и не снимают, как статус статьи в архиве ленты. Закрыт ли
  // месяц карточки, говорит сервер (digest_locked): своей формулы у экрана нет.
  describe("карточка закрытого месяца", () => {
    const august: Signal = {
      ...baseSignal, id: 41, signal_key: "k41", theme: "Бурение", title_ru: "Августовская карточка",
      first_seen_at: "2026-08-20T09:00:00Z", digest_month: "2026-08", digest_locked: true,
    };
    const augustChosen: Signal = {
      ...august, id: 42, signal_key: "k42", title_ru: "Августовская, уже в выпуске",
      selected_for_digest: true, user_status: "digest",
    };
    const september: Signal = {
      ...august, id: 43, signal_key: "k43", title_ru: "Сентябрьская карточка",
      first_seen_at: "2026-09-20T09:00:00Z", digest_month: "2026-09", digest_locked: false,
    };

    function digestButton(title: string, name: string) {
      return within(screen.getByText(title).closest("article") as HTMLElement).getByRole("button", { name });
    }

    it("«В дайджест» и «Убрать» неактивны, с подсказкой; у открытого месяца — как раньше", async () => {
      serve([august, augustChosen, september]);
      renderRadar(false);
      await screen.findByText("Августовская карточка");

      const hint =
        "Карточка в архиве за август 2026 (по дате поступления): отметку «в дайджест» не поменять — архив только для просмотра.";
      for (const [title, name] of [["Августовская карточка", "В дайджест"], ["Августовская, уже в выпуске", "Убрать"]]) {
        const button = digestButton(title, name);
        expect(button).toBeDisabled();
        expect(button).toHaveAttribute("title", hint);
        fireEvent.click(button);
      }
      expect(updateSignal).not.toHaveBeenCalled();

      const open = digestButton("Сентябрьская карточка", "В дайджест");
      expect(open).toBeEnabled();
      expect(open).not.toHaveAttribute("title");
      fireEvent.click(open);
      await waitFor(() => expect(updateSignal).toHaveBeenCalledWith(43, { selected_for_digest: true }));
    });

    it("месяц закрылся, пока экран был открыт: отказ сервера — текстом, кнопка гаснет", async () => {
      const detail =
        "Карточка радара относится к архиву за сентябрь 2026 (по дате поступления на радар). Архив открыт " +
        "только для просмотра: отметку «в дайджест» у карточек прошлых месяцев ставить и снимать нельзя.";
      serve([september]);
      const showToast = vi.fn();
      render(<SignalRadarPage onUnauthorized={() => undefined} showToast={showToast} />);
      const button = await screen.findByRole("button", { name: "В дайджест" });
      // 5-е число застало вкладку открытой: на экране кнопка ещё активна, а сервер месяц уже закрыл
      // и отказывает настоящим ответом FastAPI — JSON {"detail": "…"} с кодом 409.
      serve([{ ...september, digest_locked: true }]);
      const actual = await vi.importActual<typeof import("../../api/signals")>("../../api/signals");
      vi.mocked(updateSignal).mockImplementation(actual.updateSignal);
      vi.stubGlobal(
        "fetch",
        vi.fn(async () =>
          new Response(JSON.stringify({ detail }), { status: 409, headers: { "Content-Type": "application/json" } }),
        ),
      );
      try {
        fireEvent.click(button);

        await waitFor(() => expect(showToast).toHaveBeenCalledWith(detail, "error"));
        expect(showToast).not.toHaveBeenCalledWith(expect.stringContaining('"detail"'), expect.anything());
        // Выборка перечитана: у карточки теперь признак сервера — кнопка неактивна, с подсказкой.
        await waitFor(() => expect(screen.getByRole("button", { name: "В дайджест" })).toBeDisabled());
        expect(screen.getByRole("button", { name: "В дайджест" })).toHaveAttribute(
          "title",
          "Карточка в архиве за сентябрь 2026 (по дате поступления): отметку «в дайджест» не поменять — архив только для просмотра.",
        );
      } finally {
        vi.unstubAllGlobals();
      }
    });
  });
});

describe("качество радара на экране (Виктор 29.09)", () => {
  const scored: Signal = {
    ...baseSignal,
    id: 501,
    signal_key: "k501",
    title_ru: "Энергоавтономный сенсор Chevron для мониторинга парафина",
    event_date: "2026-09-10",
    score: 68.5,
    score_profile: "tech_radar",
    score_items_json: [
      { criterion_id: 101, final_score: 80 },
      { criterion_id: 105, final_score: 90.4 },
    ],
    criteria_snapshot: [
      { id: 101, name: "Ценность для нефтесервиса", weight: 30 },
      { id: 105, name: "Свежесть", weight: 10 },
    ],
  };
  const legacy: Signal = { ...baseSignal, id: 502, signal_key: "k502", title_ru: "Карточка до правки", event_date: null };
  const hiddenCard: Signal = {
    ...baseSignal,
    id: 601,
    signal_key: "k601",
    title_ru: "SLB получила контракты Aramco",
    signal_category: "business",
    hidden_reason: "бизнес-сигнал, не технология",
  };

  function serveWithHidden(visible: Signal[], hidden: Signal[]) {
    vi.mocked(listSignals).mockImplementation(async (query: SignalQuery = {}) => (query.hidden ? hidden : visible).map((card) => ({ ...card })));
    vi.mocked(getSignalSummary).mockImplementation(async (query: SignalQuery = {}) => ({
      total: visible.length,
      new_7d: 0,
      in_digest: 0,
      with_feedback: 0,
      merged: 0,
      hidden: hidden.length,
      matching: query.hidden ? hidden.length : visible.length,
      themes: [],
    }));
  }

  beforeEach(() => {
    vi.mocked(listSignals).mockReset();
    vi.mocked(getSignalSummary).mockReset();
    vi.mocked(getSignalSearchHealth).mockReset();
    vi.mocked(getSignalSearchHealth).mockResolvedValue({ search_health: null } as never);
    serveWithHidden([scored, legacy], [hiddenCard]);
  });

  it("строка карточки показывает дату самого события, если судья её поставил", async () => {
    renderRadar(false);
    const card = (await screen.findByText(scored.title_ru as string)).closest("article") as HTMLElement;
    expect(within(card).getByText("Событие: 10.09.2026")).toBeInTheDocument();
    const old = screen.getByText("Карточка до правки").closest("article") as HTMLElement;
    expect(within(old).queryByText(/Событие:/)).not.toBeInTheDocument();
  });

  it("раскрытая карточка показывает разбивку балла по критериям с весами из снимка", async () => {
    renderRadar(false);
    const card = (await screen.findByText(scored.title_ru as string)).closest("article") as HTMLElement;
    expect(within(card).getByTitle("Балл по профилю «Технологический радар»")).toHaveTextContent("69");
    fireEvent.click(within(card).getByRole("button", { name: "Раскрыть сигнал" }));
    const breakdown = within(card).getByText("Балл по критериям").closest(".signalScoreBreakdown") as HTMLElement;
    const rows = within(breakdown).getAllByRole("listitem").map((row) => row.textContent);
    expect(rows).toEqual(["Ценность для нефтесервиса · вес 3080", "Свежесть · вес 1090"]);
  });

  it("у карточки без профиля разбивки нет, балл — судьи", async () => {
    renderRadar(false);
    const card = (await screen.findByText("Карточка до правки")).closest("article") as HTMLElement;
    expect(within(card).getByTitle("Балл судьи радара")).toBeInTheDocument();
    fireEvent.click(within(card).getByRole("button", { name: "Раскрыть сигнал" }));
    expect(within(card).queryByText("Балл по критериям")).not.toBeInTheDocument();
  });

  it("админ открывает скрытые карточки с причиной и возвращается к радару", async () => {
    renderRadar(true);
    await screen.findByText(scored.title_ru as string);
    expect(tileValue("Скрыто")).toBe("1");

    fireEvent.click(screen.getByRole("button", { name: "Скрытые (1)" }));

    expect(await screen.findByText("SLB получила контракты Aramco")).toBeInTheDocument();
    expect(lastListQuery().hidden).toBe(true);
    expect(screen.getByRole("heading", { name: "Скрытые карточки радара" })).toBeInTheDocument();
    expect(screen.getByText("Скрыта: бизнес-сигнал, не технология")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("на радар их не пускают правила качества");

    fireEvent.click(screen.getByRole("button", { name: "К радару" }));

    expect(await screen.findByText(scored.title_ru as string)).toBeInTheDocument();
    expect(lastListQuery().hidden).toBeUndefined();
  });

  it("обычный пользователь не видит ни переключателя, ни плитки «Скрыто»", async () => {
    renderRadar(false);
    await screen.findByText(scored.title_ru as string);
    expect(screen.queryByRole("button", { name: /^Скрытые/ })).not.toBeInTheDocument();
    expect(screen.queryByText("Скрыто")).not.toBeInTheDocument();
    expect(lastListQuery().hidden).toBeUndefined();
  });
});

describe("применение в нефтесервисе на карточке", () => {
  beforeEach(() => {
    vi.mocked(listSignals).mockReset();
    vi.mocked(getSignalSummary).mockReset();
  });

  it("показывает, где применить, и помечает перенос из другой отрасли", async () => {
    serve([
      { ...baseSignal, id: 701, signal_key: "k701", title_ru: "Aurora: автономная логистика фрак-песка",
        oilfield_relevance: "transferable", oilfield_application: "Доставка проппанта на кустовые площадки" },
      { ...baseSignal, id: 702, signal_key: "k702", title_ru: "Сенсор Chevron для парафина",
        oilfield_relevance: "direct", oilfield_application: "Мониторинг парафина в промысловых трубопроводах" },
      { ...baseSignal, id: 703, signal_key: "k703", title_ru: "Карточка до правки" },
    ]);
    render(<SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} />);
    for (const title of ["Aurora: автономная логистика фрак-песка", "Сенсор Chevron для парафина", "Карточка до правки"]) {
      const card = (await screen.findByText(title)).closest("article") as HTMLElement;
      fireEvent.click(within(card).getByRole("button", { name: "Раскрыть сигнал" }));
    }

    const aurora = screen.getByText("Aurora: автономная логистика фрак-песка").closest("article") as HTMLElement;
    expect(within(aurora).getByText("Применение в нефтесервисе · перенос из другой отрасли")).toBeInTheDocument();
    expect(within(aurora).getByText("Доставка проппанта на кустовые площадки")).toBeInTheDocument();
    const chevron = screen.getByText("Сенсор Chevron для парафина").closest("article") as HTMLElement;
    expect(within(chevron).getByText("Применение в нефтесервисе")).toBeInTheDocument();
    const legacy = screen.getByText("Карточка до правки").closest("article") as HTMLElement;
    expect(within(legacy).queryByText(/Применение в нефтесервисе/)).not.toBeInTheDocument();
  });
});
