import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  createSignalFeedback,
  getSignalDictionaries,
  getSignalSearchHealth,
  getSignalSummary,
  listSignals,
  restoreSignal,
  returnSignal,
  type SignalQuery,
  type SignalShowcase,
} from "../../api/signals";
import type { Signal } from "../../api/types";
import { SignalRadarPage } from "./SignalRadarPage";
import { DICTIONARIES, contract, forum, publication } from "./businessFeedFixtures";

// Контур ОС (#188, спека #187), шов 3: одна форма ОС, как в технологическом радаре, у карточки любого происхождения
// и категории в обеих витринах и в «Отклонённых»; «Дубль» номером или выбором по словам заголовка; отметка участника
// видна блоком, явные действия администратора меняют карточку сразу. Экран на подставном API.

vi.mock("../../api/signals", () => ({
  listSignals: vi.fn(),
  getSignalSummary: vi.fn(),
  getSignalDictionaries: vi.fn(),
  updateSignal: vi.fn(),
  editIssueMaterial: vi.fn(),
  createSignalFeedback: vi.fn(),
  returnSignal: vi.fn(),
  restoreSignal: vi.fn(),
  getSignalSearchHealth: vi.fn(),
}));

const material: Signal = {
  ...contract, id: 9101, signal_key: "material:1", origin: "material", title: "Нефтяная компания выбрала подрядчика",
  title_ru: "Нефтяная компания выбрала подрядчика", evidence: [],
};
const finding: Signal = {
  ...contract, id: 9201, signal_key: "radar:1", origin: "radar", signal_category: "technology",
  title: "Robot driller tested offshore", title_ru: "Робот-бурильщик прошёл испытания", evidence: [publication(21, {})],
};
const techArticle: Signal = {
  ...contract, id: 9202, signal_key: "feed:9", origin: "feed", signal_category: "technology",
  title_ru: "Цифровой двойник бурения", evidence: [publication(22, {})],
};
const rejected: Signal = {
  ...contract, id: 9301, signal_key: "feed:30", otbor_decision: "reject", maturity: "reject",
  title_ru: "Котировки нефти", section_reason: "«Отбор» → Нефтегаз без связи с нефтесервисом",
};
const removed: Signal = {
  ...contract, id: 9302, signal_key: "feed:31", otbor_decision: "reject", maturity: "reject",
  title_ru: "Ремонт скважин вне интересов",
  section_reason: "Люди → Убрано людьми: admin@example.test: вне интересов Компании",
  human_removal: { author: "admin@example.test", reason: "вне интересов Компании", at: "2026-11-12T10:00:00Z" },
};

const FORM_FIELDS = ["Оценка сигнала", "ID дубля", "Обоснование оценки", "Рекомендуемый заголовок",
  "Рекомендуемая формулировка сути", "Рекомендации AI-агенту"];

type Screen = Record<SignalShowcase, Signal[]> & { rejected: Signal[] };

// Сервер в миниатюре: витрина по showcase, «Отклонённые» по section, поиск по словам заголовка и по «#номеру».
function serve(cards: Partial<Screen>) {
  const all: Screen = { technology: [], business: [], rejected: [], ...cards };
  const found = (card: Signal, q: string) => {
    const number = q.replace(/^[#№]/, "");
    return String(card.id) === number || (card.title_ru || card.title).toLowerCase().includes(q.toLowerCase());
  };
  vi.mocked(listSignals).mockImplementation(async (query: SignalQuery = {}) => {
    const cardsOf = query.section ? all.rejected : all[query.showcase ?? "technology"];
    return cardsOf.filter((card) => !query.q || found(card, query.q)).map((card) => ({ ...card }));
  });
  vi.mocked(getSignalSummary).mockImplementation(async (query: SignalQuery = {}) => {
    const cardsOf = all[query.showcase ?? "technology"];
    return { total: cardsOf.length, new_7d: 0, in_digest: 0, with_feedback: 0, merged: 0,
             rejected: all.rejected.length, matching: cardsOf.length, themes: [] };
  });
}

const toast = vi.fn();

function renderShowcase(showcase: SignalShowcase, isAdmin = false) {
  render(<SignalRadarPage onUnauthorized={() => undefined} showToast={toast} isAdmin={isAdmin} showcase={showcase} />);
}

async function cardOf(title: string) {
  return (await screen.findByText(title)).closest("article") as HTMLElement;
}

// Форма ОС раскрытой карточки: подписи полей по порядку и пункты оценки.
function openForm(card: HTMLElement) {
  const expand = within(card).queryByRole("button", { name: "Раскрыть сигнал" });
  if (expand) fireEvent.click(expand);
  fireEvent.click(within(card).getByRole("button", { name: "Обратная связь" }));
  const box = card.querySelector(".signalFeedbackBox") as HTMLElement;
  return {
    box,
    fields: [...box.querySelectorAll(":scope > .signalFeedbackGrid > .field > span")].map((label) => label.textContent),
    verdicts: within(within(box).getByLabelText("Оценка сигнала")).getAllByRole("option").map((item) => item.textContent),
  };
}

beforeEach(() => {
  vi.mocked(listSignals).mockReset();
  vi.mocked(getSignalSummary).mockReset();
  vi.mocked(createSignalFeedback).mockReset();
  vi.mocked(restoreSignal).mockReset();
  vi.mocked(returnSignal).mockReset();
  vi.mocked(getSignalDictionaries).mockResolvedValue({
    ...DICTIONARIES,
    otbor_return: { industry: [{ code: "oil_gas", label: "Нефтегаз" }], oilfield_relevance: [], category: [] },
  });
  vi.mocked(getSignalSearchHealth).mockResolvedValue({ search_health: null });
  toast.mockReset();
});

describe("одна форма ОС в обеих витринах (#188)", () => {
  it("у сигнала ленты и материала в бизнес-ленте полная форма с «Дублем», «Не тот блок» по витрине", async () => {
    serve({ business: [contract, material] });
    renderShowcase("business");

    for (const title of [contract.title_ru, material.title_ru] as string[]) {
      const { fields, verdicts } = openForm(await cardOf(title));
      expect(fields).toEqual(FORM_FIELDS);
      expect(verdicts).toHaveLength(10);
      expect(verdicts).toContain("Дубль — тот же сигнал или технологический кластер");
      expect(verdicts).toContain("Не тот блок — технологический сигнал, а не бизнес");
    }
  });

  it("в технологическом радаре та же форма у находки поиска и у статьи ленты", async () => {
    serve({ technology: [finding, techArticle] });
    renderShowcase("technology");

    for (const title of [finding.title_ru, techArticle.title_ru] as string[]) {
      const { fields, verdicts } = openForm(await cardOf(title));
      expect(fields).toEqual(FORM_FIELDS);
      expect(verdicts).toContain("Не тот блок — бизнес-сигнал, а не технология");
      expect(verdicts).toContain("Дубль — тот же сигнал или технологический кластер");
    }
  });

  it("в «Отклонённых» у админа та же форма", async () => {
    serve({ business: [contract], rejected: [rejected] });
    renderShowcase("business", true);
    fireEvent.click(await screen.findByRole("button", { name: "Отклонённые: 1" }));

    const { fields, verdicts } = openForm(await cardOf("Котировки нефти"));
    expect(fields).toEqual(FORM_FIELDS);
    expect(verdicts).toHaveLength(10);
  });
});

describe("«Дубль»: главная номером или выбором по словам заголовка (#188)", () => {
  it("номер уходит в отметку числом", async () => {
    serve({ business: [contract] });
    vi.mocked(createSignalFeedback).mockResolvedValue({ ok: true, event_id: 1, applied: true, merged: true, block: null });
    renderShowcase("business", true);
    const { box } = openForm(await cardOf(contract.title_ru as string));

    fireEvent.change(within(box).getByLabelText("Оценка сигнала"), { target: { value: "merge_duplicate" } });
    fireEvent.change(within(box).getByLabelText("ID дубля"), { target: { value: "#9002" } });
    fireEvent.click(within(box).getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(createSignalFeedback).toHaveBeenCalledWith(expect.objectContaining({
      signal_id: contract.id, verdict: "merge_duplicate", duplicate_of_signal_id: 9002,
    })));
  });

  it("слова заголовка подсказывают видимые карточки обеих витрин, выбранная уходит номером", async () => {
    serve({ business: [contract, forum], technology: [finding] });
    vi.mocked(createSignalFeedback).mockResolvedValue({ ok: true, event_id: 2, applied: true, merged: true, block: null });
    renderShowcase("business", true);
    const { box } = openForm(await cardOf(contract.title_ru as string));
    fireEvent.change(within(box).getByLabelText("Оценка сигнала"), { target: { value: "merge_duplicate" } });

    fireEvent.change(within(box).getByLabelText("ID дубля"), { target: { value: "Форум" } });

    const hints = await within(box).findByRole("list", { name: "Подсказки главной карточки" });
    expect(within(hints).getAllByRole("button").map((item) => item.textContent)).toEqual([
      "№9002 · Форум нефтесервиса в Тюмени · бизнес-лента"]);
    // Подсказки ищут в обеих витринах: технологический радар и бизнес-лента.
    const searched = vi.mocked(listSignals).mock.calls.map(([query]) => query).filter((query) => query?.q === "Форум");
    expect(searched.map((query) => query?.showcase ?? "technology").sort()).toEqual(["business", "technology"]);
    fireEvent.click(within(hints).getByRole("button"));
    expect(within(box).getByText("Главная карточка: №9002 «Форум нефтесервиса в Тюмени»")).toBeInTheDocument();
    fireEvent.click(within(box).getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(createSignalFeedback).toHaveBeenCalledWith(expect.objectContaining({
      verdict: "merge_duplicate", duplicate_of_signal_id: 9002,
    })));
  });

  it("слова без выбора не уходят: экран просит выбрать главную", async () => {
    serve({ business: [contract] });
    renderShowcase("business", true);
    const { box } = openForm(await cardOf(contract.title_ru as string));

    fireEvent.change(within(box).getByLabelText("ID дубля"), { target: { value: "контракт Aramco" } });
    fireEvent.click(within(box).getByRole("button", { name: "Сохранить" }));

    expect(toast).toHaveBeenCalledWith("Выберите главную карточку из подсказок или впишите её номер", "error");
    expect(createSignalFeedback).not.toHaveBeenCalled();
  });

  it("отказ склейки: экран говорит почему", async () => {
    serve({ business: [contract] });
    vi.mocked(createSignalFeedback).mockResolvedValue({
      ok: true, event_id: 3, applied: true, merged: false, block: null,
      merge_refused: "карточка выбрана «в дайджест»: склейка убрала бы её из витрины, а в выпуске она осталась бы",
    });
    renderShowcase("business", true);
    const { box } = openForm(await cardOf(contract.title_ru as string));
    fireEvent.change(within(box).getByLabelText("Оценка сигнала"), { target: { value: "merge_duplicate" } });
    fireEvent.change(within(box).getByLabelText("ID дубля"), { target: { value: "9002" } });

    fireEvent.click(within(box).getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      "Отметка сохранена, карточка не склеена: карточка выбрана «в дайджест»: склейка убрала бы её из витрины, "
      + "а в выпуске она осталась бы"));
  });
});

describe("кто меняет карточку (#188)", () => {
  it("отметка участника видна блоком с предложенным заголовком и сутью, текст карточки прежний", async () => {
    serve({ business: [contract] });
    vi.mocked(createSignalFeedback).mockResolvedValue({
      ok: true, event_id: 4, applied: false,
      block: { id: 4, author: "colleague@example.test", verdict: "wrong_block", reason: null, comment: null,
               proposed_title: "Заголовок коллеги", proposed_thesis: "Суть коллеги.", duplicate_of_signal_id: null,
               applied: false, created_at: "2026-11-12T09:00:00Z" },
    });
    renderShowcase("business");
    const card = await cardOf(contract.title_ru as string);
    const { box } = openForm(card);
    fireEvent.change(within(box).getByLabelText("Оценка сигнала"), { target: { value: "wrong_block" } });
    fireEvent.change(within(box).getByLabelText("Рекомендуемый заголовок"), { target: { value: "Заголовок коллеги" } });
    fireEvent.change(within(box).getByLabelText("Рекомендуемая формулировка сути"), { target: { value: "Суть коллеги." } });

    fireEvent.click(within(box).getByRole("button", { name: "Сохранить" }));

    const blocks = await within(card).findByRole("list", { name: "Оценки людей" });
    expect(within(blocks).getByText("Предлагает заголовок: Заголовок коллеги")).toBeInTheDocument();
    expect(within(blocks).getByText("Предлагает суть: Суть коллеги.")).toBeInTheDocument();
    expect(within(card).getByText(contract.title_ru as string)).toBeInTheDocument();
    expect(toast).toHaveBeenCalledWith(
      "Отметка сохранена и видна на карточке; заголовок, суть, категорию и склейку меняет администратор");
  });

  it("правка администратора сразу на карточке", async () => {
    serve({ business: [contract] });
    vi.mocked(createSignalFeedback).mockResolvedValue({ ok: true, event_id: 5, applied: true, block: null });
    renderShowcase("business", true);
    const card = await cardOf(contract.title_ru as string);
    const { box } = openForm(card);
    fireEvent.change(within(box).getByLabelText("Рекомендуемый заголовок"), { target: { value: "Контракт на 40 скважин" } });
    fireEvent.change(within(box).getByLabelText("Рекомендуемая формулировка сути"), { target: { value: "Суть эксперта." } });

    fireEvent.click(within(box).getByRole("button", { name: "Сохранить" }));

    expect(await within(card).findByText("Контракт на 40 скважин")).toBeInTheDocument();
    expect(within(card).getByText("Суть эксперта.")).toBeInTheDocument();
  });

  it("«Не релевантно» администратора уводит карточку в «Убрано людьми»", async () => {
    serve({ business: [contract] });
    vi.mocked(createSignalFeedback).mockResolvedValue({ ok: true, event_id: 6, applied: true, removed: true, block: null });
    renderShowcase("business", true);
    const card = await cardOf(contract.title_ru as string);
    const { box } = openForm(card);
    fireEvent.change(within(box).getByLabelText("Оценка сигнала"), { target: { value: "wrong_domain" } });

    fireEvent.click(within(box).getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      "Отметка сохранена: карточка убрана в «Убрано людьми», вернуть её можно в «Отклонённых»"));
  });

  it("в «Отклонённых» убранное людьми возвращает «Вернуть» одним нажатием", async () => {
    serve({ business: [contract], rejected: [removed, rejected] });
    vi.mocked(restoreSignal).mockResolvedValue({ ok: true });
    renderShowcase("business", true);
    fireEvent.click(await screen.findByRole("button", { name: "Отклонённые: 2" }));

    fireEvent.click(within(await cardOf("Ремонт скважин вне интересов")).getByRole("button", { name: "Вернуть" }));

    await waitFor(() => expect(restoreSignal).toHaveBeenCalledWith(removed.id));
    expect(toast).toHaveBeenCalledWith("Карточка возвращена: прежнее решение «Отбора» восстановлено");
    // У отклонённого «Отбором» «Вернуть» по-прежнему открывает форму полей «Отбора» (#132).
    fireEvent.click(within(await cardOf("Котировки нефти")).getByRole("button", { name: "Вернуть" }));
    expect(returnSignal).not.toHaveBeenCalled();
    expect(restoreSignal).toHaveBeenCalledTimes(1);
  });
});
