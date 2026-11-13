import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { JournalEntry, JournalOverview } from "../../api/feedbackJournal";
import { FeedbackJournalPage } from "./FeedbackJournalPage";

// Вкладка «Обратная связь» (#189, спека #187), шов 3: экран на подставном API. Один журнал отметок и действий обеих
// витрин со статусами и путём действия; кнопки решений, отмены, «собрать сейчас» и настройки только у администратора.

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const mark: JournalEntry = {
  key: "mark:11", kind: "mark", id: 11, at: "2026-11-14T09:00:00+00:00", author: "expert@example.test", role: "admin",
  signal_id: 501, signal_title: "Контракт на бурение 40 скважин", showcase: "business", agents: ["summary"],
  status: "done", verdict: "approved", reason: null, comment: null, corrected_title: "Контракт на сорок скважин",
  corrected_thesis: null, duplicate_of_signal_id: null, applied: true, system: ["Правка: заголовок"],
  awaits_slice: false, action_id: 31, can_undo: true, proposals: [],
  path: [{ label: "Записано: отметка администратора", at: "2026-11-14T09:00:00+00:00", author: "expert@example.test" },
    { label: "Сразу: Правка: заголовок", at: "2026-11-14T09:00:00+00:00", author: null }],
};

const proposal: JournalEntry = {
  key: "action:77", kind: "signal_proposal", id: 77, at: "2026-11-16T06:10:00+00:00", author: "Разбор ОС", role: null,
  signal_id: 602, signal_title: "Пилот роботизированной буровой", showcase: "technology", agents: ["card"],
  status: "pending", quote: "уже пилот у оператора", event_ids: [12, 13],
  changes: [{ field: "technology_maturity", label: "Стадия (зрелость)", value: { technology_maturity: "пилот/ОПИ" },
    shown: "пилот/ОПИ", previous: "прототип", quote: "уже пилот у оператора", event_ids: [12, 13] }],
  basis: [{ id: 12, author: "colleague@example.test", role: "user", at: null, verdict: "watch_later", reason: null,
    comment: "Это уже пилот у оператора", corrected_title: null, corrected_thesis: null, duplicate_of_signal_id: null }],
  can_decide: true, can_undo: false,
  path: [{ label: "Предложено разбором ОС (срез №4)", at: "2026-11-16T06:10:00+00:00", author: null }],
};

const applied: JournalEntry = {
  ...proposal, key: "action:78", id: 78, status: "applied", can_decide: false, can_undo: true,
  path: [{ label: "Предложено разбором ОС (срез №3)", at: null, author: null },
    { label: "Согласовано", at: "2026-11-10T08:00:00+00:00", author: "expert@example.test" },
    { label: "Применено системой", at: "2026-11-10T08:00:00+00:00", author: "expert@example.test" }],
};

const overview: JournalOverview = {
  slices: [{ id: 4, trigger: "schedule", status: "done", window_from: "2026-11-13T07:55:00+00:00",
    window_to: "2026-11-16T06:00:00+00:00", signals: 3, proposals: 1, note: null, author: null,
    created_at: "2026-11-16T06:00:00+00:00", finished_at: "2026-11-16T06:12:00+00:00" }],
  running: false, next_slice_at: "2026-11-23T09:00:00+03:00", month_spend_usd: 0.42, ceiling_usd: 50, warning: null,
  can_collect: true, can_configure: true,
};

const settings = {
  values: { slice_weekday: "1", slice_time: "09:00", card_badge: true, monthly_ceiling_usd: 50 },
  fields: [
    { key: "slice_weekday", label: "День недельного среза", kind: "choice", default: "1", low: null, high: null,
      later: false, choices: [{ value: "1", label: "понедельник" }, { value: "2", label: "вторник" }] },
    { key: "slice_time", label: "Время недельного среза, МСК", kind: "time", default: "09:00", low: null, high: null,
      later: false, choices: [] },
    { key: "card_badge", label: "Значок «ждёт решения в журнале» на карточке", kind: "bool", default: true, low: null,
      high: null, later: false, choices: [] },
    { key: "monthly_ceiling_usd", label: "Потолок расходов контура в месяц, $", kind: "number", default: 50, low: 0,
      high: 10000, later: false, choices: [] },
  ],
  can_edit: true,
};

const fetchMock = vi.fn();

function serveJournal(entries: JournalEntry[], extra: Partial<JournalOverview> = {}) {
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.startsWith("/api/feedback-journal/overview")) return Promise.resolve(json({ ...overview, ...extra }));
    if (url.startsWith("/api/feedback-journal/settings")) {
      return Promise.resolve(json(method === "PUT" ? { ...settings, values: JSON.parse(String(init?.body)).values } : settings));
    }
    if (method === "POST") return Promise.resolve(json({ status: "applied" }));
    return Promise.resolve(json({ entries, authors: ["colleague@example.test", "expert@example.test"] }));
  });
}

// Строка так, как её отдаёт сервер участнику: кнопок решений, отмены и закрытия у него нет (флаги считает сервер).
function asParticipant(entry: JournalEntry): JournalEntry {
  return { ...entry, can_decide: false, can_undo: false, can_close: false };
}

function renderJournal(isAdmin: boolean) {
  render(<FeedbackJournalPage onUnauthorized={() => undefined} showToast={() => undefined} isAdmin={isAdmin} />);
}

function journalCalls(): URLSearchParams[] {
  return fetchMock.mock.calls.map(([url]) => String(url))
    .filter((url) => url.startsWith("/api/feedback-journal?"))
    .map((url) => new URLSearchParams(url.split("?")[1]));
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetchMock.mockReset();
});

describe("вкладка «Обратная связь»", () => {
  it("показывает отметки и действия обеих витрин одним журналом со статусами и путём действия", async () => {
    serveJournal([proposal, mark, applied].map(asParticipant), { can_collect: false, can_configure: false });
    renderJournal(false);

    const pending = await screen.findByRole("article", { name: "Предложение разбора ОС №77" });
    expect(within(pending).getByText("ждёт решения")).toBeInTheDocument();
    expect(within(pending).getByText(/Технологический радар/)).toBeInTheDocument();
    expect(within(within(pending).getByRole("table")).getByText("прототип")).toBeInTheDocument();
    expect(within(pending).getByText(/«уже пилот у оператора»/)).toBeInTheDocument();
    expect(within(pending).getByLabelText("Основание")).toHaveTextContent("colleague@example.test");
    const edited = screen.getByRole("article", { name: "Отметка №11" });
    expect(within(edited).getByText("сделано")).toBeInTheDocument();
    expect(within(edited).getByText(/Бизнес-лента/)).toBeInTheDocument();
    expect(within(edited).getByText("Правка: заголовок")).toBeInTheDocument();
    const path = within(screen.getByRole("article", { name: "Предложение разбора ОС №78" })).getByRole("list", { name: "Путь действия" });
    expect(within(path).getAllByRole("listitem").map((step) => step.textContent?.split(" · ")[0])).toEqual([
      "Предложено разбором ОС (срез №3)", "Согласовано", "Применено системой"]);

    // Участник видит журнал без кнопок решений, «собрать сейчас» и настроек.
    for (const name of ["Согласовать", "Изменить", "Убрать", "Отменить", "Собрать сейчас", "Сохранить настройки"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });

  it("фильтрует журнал по статусу, витрине, агенту и автору", async () => {
    serveJournal([proposal]);
    renderJournal(false);
    await screen.findByRole("article", { name: "Предложение разбора ОС №77" });

    fireEvent.change(screen.getByRole("combobox", { name: "Статус" }), { target: { value: "pending" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Витрина" }), { target: { value: "technology" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Агент" }), { target: { value: "card" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Автор" }), { target: { value: "colleague@example.test" } });

    await waitFor(() => expect(Object.fromEntries(journalCalls().slice(-1)[0] ?? [])).toMatchObject({
      status: "pending", showcase: "technology", agent: "card", author: "colleague@example.test" }));
  });

  it("у администратора: согласовать, изменить формулировку, отменить применённое и собрать сейчас", async () => {
    serveJournal([proposal, applied]);
    renderJournal(true);
    const pending = await screen.findByRole("article", { name: "Предложение разбора ОС №77" });

    fireEvent.click(within(pending).getByRole("button", { name: "Согласовать" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/feedback-journal/proposals/77/decision",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ decision: "approve", changes: [] }) })));

    fireEvent.click(within(pending).getByRole("button", { name: "Изменить" }));
    fireEvent.change(within(pending).getByRole("textbox", { name: "Стадия (зрелость)" }), { target: { value: "прототип" } });
    fireEvent.click(within(pending).getByRole("button", { name: "Применить новую формулировку" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/feedback-journal/proposals/77/decision",
      expect.objectContaining({ body: JSON.stringify({ decision: "modify",
        changes: [{ field: "technology_maturity", value: "прототип" }] }) })));

    fireEvent.click(within(screen.getByRole("article", { name: "Предложение разбора ОС №78" })).getByRole("button", { name: "Отменить" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/feedback-journal/actions/78/undo",
      expect.objectContaining({ method: "POST" })));

    fireEvent.click(screen.getByRole("button", { name: "Собрать сейчас" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/feedback-journal/slices",
      expect.objectContaining({ method: "POST" })));
  });

  it("отмена явного действия отметки идёт по номеру её действия, предупреждение о потолке видит администратор", async () => {
    serveJournal([mark], { warning: "Потолок расходов контура ОС: $50.00 из $50.00 в этом месяце." });
    renderJournal(true);

    expect(await screen.findByRole("alert")).toHaveTextContent("Потолок расходов контура ОС");
    fireEvent.click(within(await screen.findByRole("article", { name: "Отметка №11" })).getByRole("button", { name: "Отменить" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/feedback-journal/actions/31/undo",
      expect.objectContaining({ method: "POST" })));
  });

  it("настройки контура правит администратор на вкладке", async () => {
    serveJournal([]);
    renderJournal(true);

    const panel = await screen.findByRole("region", { name: "Настройки контура" });
    fireEvent.change(within(panel).getByRole("combobox", { name: "День недельного среза" }), { target: { value: "2" } });
    fireEvent.click(within(panel).getByRole("checkbox", { name: "Значок «ждёт решения в журнале» на карточке" }));
    fireEvent.click(within(panel).getByRole("button", { name: "Сохранить настройки" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/feedback-journal/settings", expect.objectContaining({
      method: "PUT",
      body: JSON.stringify({ values: { ...settings.values, slice_weekday: "2", card_badge: false } }),
    })));
  });

  it("ссылка со значка карточки открывает журнал этой карточки и выделяет строку предложения", async () => {
    window.history.replaceState(null, "", "/?screen=feedback-journal&signal=602&action=77");
    serveJournal([proposal]);
    renderJournal(false);

    const row = await screen.findByRole("article", { name: "Предложение разбора ОС №77" });
    expect(row).toHaveClass("journalEntryHighlighted");
    expect(Object.fromEntries(journalCalls()[0])).toMatchObject({ signal_id: "602" });
  });
});
