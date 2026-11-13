import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ExpertRulesOverview, JournalCheck, JournalEntry, JournalOverview } from "../../api/feedbackJournal";
import { FeedbackJournalPage } from "./FeedbackJournalPage";

// Правила эксперта (#190, спека #187), шов 3: вкладка «Обратная связь» на подставном API. Предложение правила с
// итогом автопроверки (к эталону, от эталона, совет, «мало данных»), согласование с отдельным подтверждением,
// новая формулировка, откат версии и проверка заново у администратора; набор правил агентов виден всем.

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const check: JournalCheck = {
  id: 5, status: "done", mode: "rule", rows: 42, toward: 6, away: 2, unchanged: 34, advice: "согласовать", note: null,
  little_data: false, no_count: false, baseline: { accepted: 40, computed: 2 }, search: null,
  finished_at: "2026-11-16T07:00:00+00:00",
  samples: [{ signal_id: 610, label: "Пилот робота на НПЗ", field: "technology_maturity", kind: "field",
    expected: "пилот/ОПИ", before: "внедрение", after: "пилот/ОПИ", direction: "toward" }],
};

const rule: JournalEntry = {
  key: "action:120", kind: "rule_proposal", id: 120, at: "2026-11-16T06:10:00+00:00", author: "Разбор ОС", role: null,
  signal_id: null, signal_title: null, showcase: null, agents: ["card"], status: "pending", quote: "PoC это не внедрение",
  event_ids: [21, 22],
  rule: { agent: "card", agent_label: "«Карточка»", step: "maturity", step_label: "стадия", category: null,
    category_label: null, topic: null, text: "«PoC» не промышленное внедрение." },
  proposed_rule: null, rule_kind: "new", replaced: [], check, confirm: [], version: null, can_rollback: false,
  can_recheck: true, can_decide: true,
  basis: [{ id: 21, author: "expert@example.test", role: "admin", at: null, verdict: "watch_later", reason: null,
    comment: "PoC это не внедрение", corrected_title: null, corrected_thesis: null, duplicate_of_signal_id: null }],
  path: [{ label: "Предложено разбором ОС (срез №4)", at: "2026-11-16T06:10:00+00:00", author: null }],
};

const littleData: JournalEntry = {
  ...rule, key: "action:121", id: 121, confirm: ["мало данных: 12"],
  check: { ...check, id: 6, rows: 12, toward: 3, away: 0, unchanged: 9, little_data: true, advice: "мало данных: 12" },
};

const applied: JournalEntry = {
  ...rule, key: "action:122", id: 122, status: "applied", version: 3, can_decide: false, can_recheck: false,
  can_rollback: true,
  path: [{ label: "Предложено разбором ОС (срез №3)", at: null, author: null },
    { label: "Согласовано", at: "2026-11-10T08:00:00+00:00", author: "expert@example.test" },
    { label: "Применено системой: версия 3 набора правил агента «Карточка»", at: null, author: null }],
};

const summary: JournalEntry = {
  ...rule, key: "action:123", id: 123, agents: ["summary"],
  rule: { ...rule.rule!, agent: "summary", agent_label: "«Суть и перевод»", step: "summary", step_label: "суть",
    text: "Суть начинай с заказчика работ." },
  check: { ...check, id: 7, no_count: true, rows: 1, toward: 0, away: 0, unchanged: 0,
    advice: "числа нет: сравните «было / стало» с текстом эксперта",
    samples: [{ signal_id: 611, label: "Контракт в Омане", field: "summary", expert: "Заказчик: оператор в Омане.",
      before: "Weatherford получила контракт.", after: "Оператор в Омане заказал бурение у Weatherford." }] },
};

const setCheck: JournalEntry = {
  key: "action:130", kind: "set_check", id: 130, at: "2026-12-07T06:30:00+00:00", author: "Автопроверка", role: null,
  signal_id: null, signal_title: null, showcase: null, agents: ["dedup"], status: "done", quote: null, event_ids: [],
  set_agent: "dedup", set_agent_label: "«Дедуп»", version: 2,
  check: { ...check, id: 8, mode: "set", rows: 4, toward: 1, away: 0, unchanged: 3, little_data: true,
    advice: "мало данных: 4", samples: [] },
  path: [{ label: "Проверка набора правил агента «Дедуп» (версия 2) против голой инструкции", at: null, author: null }],
};

const rules: ExpertRulesOverview = {
  limit: 20, can_rollback: true,
  agents: [
    { agent: "card", label: "«Карточка»", steps: [], version: 3, action_id: 122,
      rules: [{ id: 9, agent: "card", agent_label: "«Карточка»", step: "maturity", step_label: "стадия", category: null,
        category_label: null, topic: null, text: "«PoC» не промышленное внедрение.", approved_by: "expert@example.test",
        approved_at: "2026-11-10T08:00:00+00:00" }],
      history: [{ version: 3, rules: 1, current: true, previous_version: 2, author: "expert@example.test",
        at: "2026-11-10T08:00:00+00:00", action_id: 122 }] },
    { agent: "dedup", label: "«Дедуп»", steps: [], version: 0, action_id: null, rules: [], history: [] },
  ],
};

const overview: JournalOverview = {
  slices: [], running: false, next_slice_at: "2026-11-23T09:00:00+03:00", month_spend_usd: 1.2, ceiling_usd: 50,
  warning: null, can_collect: true, can_configure: false,
};

const fetchMock = vi.fn();

function serve(entries: JournalEntry[], extraRules: Partial<ExpertRulesOverview> = {}) {
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    if (url.startsWith("/api/feedback-journal/overview")) return Promise.resolve(json(overview));
    if (url.startsWith("/api/feedback-journal/rules")) return Promise.resolve(json({ ...rules, ...extraRules }));
    if ((init?.method ?? "GET") === "POST") return Promise.resolve(json({ status: "applied", version: 4 }));
    return Promise.resolve(json({ entries, authors: [] }));
  });
}

function asParticipant(entry: JournalEntry): JournalEntry {
  return { ...entry, can_decide: false, can_rollback: false, can_recheck: false };
}

function renderJournal(isAdmin: boolean) {
  render(<FeedbackJournalPage onUnauthorized={() => undefined} showToast={() => undefined} isAdmin={isAdmin} />);
}

function posted(url: string) {
  return fetchMock.mock.calls.filter(([called, init]) => called === url && init?.method === "POST")
    .map(([, init]) => JSON.parse(String(init?.body ?? "{}")));
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetchMock.mockReset();
});

describe("правила эксперта на вкладке «Обратная связь»", () => {
  it("показывает предложение правила, его основание и итог автопроверки; участнику без кнопок", async () => {
    serve([asParticipant(rule)], { can_rollback: false });
    renderJournal(false);

    const row = await screen.findByRole("article", { name: "Предложение правила №120" });
    expect(within(row).getByLabelText("Правило")).toHaveTextContent("Новое правило для агента «Карточка»: стадия");
    expect(within(row).getByText("«PoC» не промышленное внедрение.")).toBeInTheDocument();
    expect(within(row).getByLabelText("Основание")).toHaveTextContent("expert@example.test");
    const checked = within(row).getByLabelText("Автопроверка");
    expect(checked).toHaveTextContent("согласовать");
    expect(checked).toHaveTextContent("Строк эталона ОС: 42 · к эталону: 6 · от эталона: 2 · без изменений: 34");
    expect(checked).toHaveTextContent("из принятых решений 40, посчитан заново 2");
    const moved = within(row).getByRole("table", { name: "Решения, которые сдвинулись" });
    expect(within(moved).getByText("к эталону")).toBeInTheDocument();
    for (const name of ["Согласовать", "Изменить", "Убрать", "Проверить снова"]) {
      expect(within(row).queryByRole("button", { name })).toBeNull();
    }
    expect(screen.queryByRole("button", { name: /Откатить/ })).toBeNull();
  });

  it("согласование с «мало данных» идёт через отдельное подтверждение", async () => {
    serve([littleData]);
    renderJournal(true);
    const row = await screen.findByRole("article", { name: "Предложение правила №121" });

    fireEvent.click(within(row).getByRole("button", { name: "Согласовать" }));
    expect(within(row).getByRole("alert")).toHaveTextContent("Нужно отдельное подтверждение: мало данных: 12.");
    expect(posted("/api/feedback-journal/proposals/121/decision")).toEqual([]);
    fireEvent.click(within(row).getByRole("button", { name: "Подтверждаю, согласовать" }));

    await waitFor(() => expect(posted("/api/feedback-journal/proposals/121/decision")).toEqual([
      { decision: "approve", changes: [], confirm: true }]));
  });

  it("администратор согласует, меняет формулировку, проверяет заново и откатывает версию", async () => {
    serve([rule, applied]);
    renderJournal(true);
    const pending = await screen.findByRole("article", { name: "Предложение правила №120" });

    fireEvent.click(within(pending).getByRole("button", { name: "Согласовать" }));
    await waitFor(() => expect(posted("/api/feedback-journal/proposals/120/decision")).toEqual([
      { decision: "approve", changes: [] }]));

    fireEvent.click(within(pending).getByRole("button", { name: "Изменить" }));
    fireEvent.change(within(pending).getByRole("textbox", { name: "Новая формулировка правила" }),
      { target: { value: "«PoC» и пилот не промышленное внедрение." } });
    fireEvent.click(within(pending).getByRole("button", { name: "Применить новую формулировку" }));
    await waitFor(() => expect(posted("/api/feedback-journal/proposals/120/decision")).toContainEqual(
      { decision: "modify", changes: [], text: "«PoC» и пилот не промышленное внедрение." }));

    fireEvent.click(within(pending).getByRole("button", { name: "Проверить снова" }));
    await waitFor(() => expect(posted("/api/feedback-journal/proposals/120/check")).toHaveLength(1));

    const done = screen.getByRole("article", { name: "Предложение правила №122" });
    fireEvent.click(within(done).getByRole("button", { name: "Откатить версию 3" }));
    await waitFor(() => expect(posted("/api/feedback-journal/proposals/122/rollback")).toHaveLength(1));
  });

  it("у «Сути и перевода» «было / стало» рядом с текстом эксперта, проверка набора: итог строкой", async () => {
    serve([summary, setCheck]);
    renderJournal(false);

    const text = await screen.findByRole("article", { name: "Предложение правила №123" });
    const table = within(text).getByRole("table", { name: "Было и стало" });
    expect(table).toHaveTextContent("Заказчик: оператор в Омане.");
    expect(table).toHaveTextContent("Weatherford получила контракт.");
    expect(table).toHaveTextContent("Оператор в Омане заказал бурение у Weatherford.");
    expect(within(text).getByLabelText("Автопроверка")).not.toHaveTextContent("Строк эталона ОС");

    const set = screen.getByRole("article", { name: "Проверка набора правил №130" });
    expect(set).toHaveTextContent("весь набор правил агента «Дедуп» против голой инструкции");
    expect(within(set).getByLabelText("Автопроверка")).toHaveTextContent("мало данных: 4");
  });

  it("набор правил агентов виден всем, откатить его к прежней версии может администратор", async () => {
    serve([]);
    renderJournal(true);

    const panel = await screen.findByRole("region", { name: "Правила эксперта" });
    const card = within(panel).getByRole("region", { name: "Правила: «Карточка»" });
    expect(card).toHaveTextContent("«Карточка»: версия 3, правил 1");
    expect(card).toHaveTextContent("стадия: «PoC» не промышленное внедрение.");
    expect(panel).toHaveTextContent("Правил нет: «Дедуп».");
    fireEvent.click(within(card).getByRole("button", { name: "Откатить к версии 2" }));
    await waitFor(() => expect(posted("/api/feedback-journal/proposals/122/rollback")).toHaveLength(1));
  });
});
