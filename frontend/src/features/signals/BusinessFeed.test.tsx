import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  createSignalFeedback,
  getSignalDictionaries,
  getSignalSearchHealth,
  getSignalSummary,
  getSignalWeekly,
  listSignals,
  updateSignal,
  type SignalDictionaries,
  type SignalQuery,
} from "../../api/signals";
import type { Signal, SignalEvidence } from "../../api/types";
import { SignalRadarPage } from "./SignalRadarPage";

// Бизнес-лента (#97, спека #93): экран радара в витрине «бизнес» — карточка события, атрибуты чипами,
// фильтры сверху, клик по компании — «всё про компанию». Шов 3: экран на подставном API.

vi.mock("../../api/signals", () => ({
  listSignals: vi.fn(),
  getSignalSummary: vi.fn(),
  getSignalDictionaries: vi.fn(),
  updateSignal: vi.fn(),
  createSignalFeedback: vi.fn(),
  getSignalSearchHealth: vi.fn(),
  getSignalWeekly: vi.fn(),
}));

// Справочники — как их отдаёт сервер (/api/signals/dictionaries), сокращённо.
const DICTIONARIES: SignalDictionaries = {
  event_type: ["пилот", "контракт", "сделка (M&A)", "нет события"],
  region: ["Россия", "Европа", "Ближний Восток"],
  technology_class: ["робототехника", "датчики и IoT", "неприменимо"],
  technology_maturity: ["прототип", "пилот/ОПИ", "неприменимо"],
  companies: ["SLB", "Saudi Aramco", "Роснефть"],
};

const AGENT_FIELDS = ["event_date", "participants", "event_type", "region", "technology_class", "technology_maturity",
  "oilfield_relevance", "oilfield_application", "why_interesting"];

function publication(id: number, fields: Partial<SignalEvidence>): SignalEvidence {
  return {
    id, signal_id: 9001, article_id: id, source_url: `https://feed.example/${id}`, title: `Publication ${id}`,
    title_ru: null, publisher: "Нефтегаз", published_at: "2026-09-25T08:00:00Z", evidence_type: "feed_article",
    extracted_fact: null, summary_ru: null, strength: 1, raw_payload_json: {}, created_at: null, updated_at: null,
    ...fields,
  };
}

// Сигнал ленты с карточкой агента: статья и её перепечатка — публикации одного события.
const contract: Signal = {
  id: 9001,
  signal_key: "feed:1",
  origin: "feed",
  signal_category: "business",
  title: "SLB wins Aramco drilling contract",
  title_ru: "SLB получила контракт Saudi Aramco на бурение",
  theme: "Бурение",
  summary: "Контракт на бурение 40 скважин на шельфе.",
  thesis: null,
  transferability: null,
  maturity: "watch",
  confidence: 0,
  score: 74,
  score_label: "Выше средней",
  score_profile: "business",
  score_items_json: [{ criterion_id: 1, final_score: 80 }],
  criteria_snapshot: [{ id: 1, name: "Стратегическая значимость для нефтесервиса", weight: 30 }],
  why_now: null,
  why_not_noise: null,
  companies_json: [],
  industries_json: [],
  evidence_count: 2,
  first_seen_at: "2026-09-25T09:00:00Z",
  last_seen_at: null,
  created_at: null,
  updated_at: null,
  event_date: "2026-09-24",
  event_date_source: "из текста",
  event_type: "контракт",
  region: "Ближний Восток",
  technology_class: "неприменимо",
  technology_maturity: "неприменимо",
  oilfield_relevance: "direct",
  oilfield_application: "Бурение скважин на шельфе",
  why_interesting: "Крупнейший контракт на буровые услуги в регионе",
  participants_json: [
    { name: "SLB", quote: "SLB подписала контракт", verified: true },
    { name: "Saudi Aramco", quote: "Saudi Aramco заключила", verified: true },
  ],
  card_set_by: Object.fromEntries(AGENT_FIELDS.map((field) => [field, "agent"])),
  card_recipe: { agent: "signal_card", prompt_version: "2026-10-16.1" },
  evidence: [
    publication(1, { title_ru: "SLB получила контракт Aramco", publisher: "Нефтегаз" }),
    publication(2, { title_ru: "Aramco выбрала SLB", publisher: "Offshore Weekly", strength: 0.5 }),
  ],
};

// Агент часть значений определить не смог: «не определено» — пробел карточки, его видно.
const forum: Signal = {
  ...contract,
  id: 9002,
  signal_key: "feed:2",
  title_ru: "Форум нефтесервиса в Тюмени",
  event_date: null,
  event_date_source: "не определено",
  event_type: "не определено",
  region: "Россия",
  technology_class: "не определено",
  technology_maturity: "не определено",
  oilfield_relevance: null,
  oilfield_application: null,
  why_interesting: null,
  participants_json: [],
  evidence: [publication(3, {})],
};

// Статья, принятая отбором до выхода агента в работу: карточку ещё не заполняли.
const fresh: Signal = {
  ...contract,
  id: 9003,
  signal_key: "feed:3",
  title_ru: "Новость без карточки",
  event_date: null,
  event_date_source: null,
  event_type: null,
  region: null,
  technology_class: null,
  technology_maturity: null,
  oilfield_relevance: null,
  oilfield_application: null,
  why_interesting: null,
  participants_json: null,
  card_set_by: null,
  card_recipe: null,
  evidence: [publication(4, {})],
};

function chips(card: HTMLElement) {
  const group = within(card).getByRole("group", { name: "Атрибуты сигнала" });
  return [...group.querySelectorAll(".signalChip")].map((chip) => [chip.textContent, chip.classList.contains("gap")]);
}

function matches(card: Signal, query: SignalQuery) {
  const names = (card.participants_json || []).map((item) => item.name);
  return (
    (!query.theme || card.theme === query.theme)
    && (!query.eventType || card.event_type === query.eventType)
    && (!query.region || card.region === query.region)
    && (!query.technologyClass || card.technology_class === query.technologyClass)
    && (!query.technologyMaturity || card.technology_maturity === query.technologyMaturity)
    && (!query.company || names.includes(query.company))
  );
}

// Сервер в миниатюре: выборка и числа — «на сервере» по всем карточкам витрины.
function serve(cards: Signal[]) {
  vi.mocked(listSignals).mockImplementation(async (query: SignalQuery = {}) =>
    cards.filter((card) => matches(card, query)).map((card) => ({ ...card })),
  );
  vi.mocked(getSignalSummary).mockImplementation(async (query: SignalQuery = {}) => ({
    total: cards.length,
    new_7d: 1,
    in_digest: 0,
    with_feedback: 0,
    merged: 1,
    hidden: 0,
    matching: cards.filter((card) => matches(card, query)).length,
    themes: [...new Set(cards.map((card) => card.theme))].map((theme) => ({ theme, count: 1 })),
  }));
}

function lastListQuery(): SignalQuery {
  return vi.mocked(listSignals).mock.lastCall?.[0] ?? {};
}

async function cardOf(title: string) {
  return (await screen.findByText(title)).closest("article") as HTMLElement;
}

function renderFeed(isAdmin = false) {
  render(<SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} isAdmin={isAdmin} showcase="business" />);
}

beforeEach(() => {
  vi.mocked(listSignals).mockReset();
  vi.mocked(getSignalSummary).mockReset();
  vi.mocked(getSignalWeekly).mockReset();
  vi.mocked(getSignalSearchHealth).mockReset();
  vi.mocked(getSignalDictionaries).mockReset();
  vi.mocked(getSignalDictionaries).mockResolvedValue(DICTIONARIES);
  vi.mocked(getSignalWeekly).mockRejectedValue(new Error("нет итогов"));
  vi.mocked(getSignalSearchHealth).mockResolvedValue({ search_health: null });
  serve([contract]);
});

describe("бизнес-лента", () => {
  it("экран радара в витрине «бизнес»: карточка события, публикации снизу, «в дайджест» и ОС, как на радаре", async () => {
    renderFeed(true);

    expect(await screen.findByRole("heading", { name: "Бизнес-лента" })).toBeInTheDocument();
    const card = await cardOf("SLB получила контракт Saudi Aramco на бурение");
    expect(lastListQuery()).toMatchObject({ showcase: "business" });
    // «Решение платформы» у сигнала ленты посчитает код из ступени балла (#132): пока его на карточке нет.
    expect(within(card).getByText(/^Бурение · 2 публикации ·/)).not.toHaveTextContent("Решение платформы");
    expect(within(card).getByText("Событие: 24.09.2026 · из текста")).toBeInTheDocument();
    expect(within(card).getByTitle("Балл по профилю «Бизнес-сигналы»")).toHaveTextContent("74");
    expect(within(card).getByText("Выше средней")).toBeInTheDocument();
    // Одна механика на две витрины (решение владельца 23.10).
    expect(within(card).getByRole("button", { name: "В дайджест" })).toBeInTheDocument();

    fireEvent.click(within(card).getByRole("button", { name: "Раскрыть сигнал" }));

    expect(within(card).getByText("Контракт на бурение 40 скважин на шельфе.")).toBeInTheDocument();
    expect(within(card).getByText("Почему важно")).toBeInTheDocument();
    expect(within(card).getByText("Крупнейший контракт на буровые услуги в регионе")).toBeInTheDocument();
    const publications = within(card).getByText("Публикации").closest(".signalEvidenceList") as HTMLElement;
    expect(within(publications).getAllByRole("link").map((link) => link.getAttribute("href"))).toEqual([
      "https://feed.example/1",
      "https://feed.example/2",
    ]);
    // Блок ОС оставляет каждый, как на радаре (#130).
    expect(within(card).getByRole("button", { name: "Обратная связь" })).toBeInTheDocument();
    // Служебное радара — «Итоги недели», здоровье поиска, «Отсеянные и скрытые» — не про ленту.
    expect(getSignalWeekly).not.toHaveBeenCalled();
    expect(getSignalSearchHealth).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /^Отсеянные и скрытые/ })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("1 из 1 сигнала")).toBeInTheDocument());
  });

  it("атрибуты — чипами; «не определено» — видимым пробелом, а не пропуском", async () => {
    serve([contract, forum, fresh]);
    renderFeed();

    expect(chips(await cardOf("SLB получила контракт Saudi Aramco на бурение"))).toEqual([
      ["Категория: бизнес", false],
      ["Тип события: контракт", false],
      ["Регион: Ближний Восток", false],
      ["Класс технологии: неприменимо", false],
      ["Зрелость: неприменимо", false],
      ["Связь с нефтесервисом: прямая", false],
      ["Компании: SLB Saudi Aramco", false],
    ]);
    const gaps = await cardOf("Форум нефтесервиса в Тюмени");
    expect(chips(gaps)).toEqual([
      ["Категория: бизнес", false],
      ["Тип события: не определено", true],
      ["Регион: Россия", false],
      ["Класс технологии: не определено", true],
      ["Зрелость: не определено", true],
      ["Связь с нефтесервисом: не определено", true],
      ["Компании: не определено", true],
    ]);
    expect(within(gaps).getByText("Событие: не определено")).toHaveClass("gap");
    // Карточку ещё не заполняли — пробелов нет: «не определено» сказало бы, что агент не смог.
    const unfilled = await cardOf("Новость без карточки");
    expect(chips(unfilled)).toEqual([["Категория: бизнес", false]]);
    expect(within(unfilled).queryByText(/^Событие:/)).not.toBeInTheDocument();

    fireEvent.click(within(gaps).getByRole("button", { name: "Раскрыть сигнал" }));
    fireEvent.click(within(unfilled).getByRole("button", { name: "Раскрыть сигнал" }));
    expect(within(gaps).getByText("Почему важно").nextSibling).toHaveTextContent("не определено");
    expect(within(unfilled).getByText("Почему важно").nextSibling).toHaveTextContent("Карточка ещё не заполнена");
  });

  describe("фильтры и «всё про компанию»", () => {
    const deal: Signal = {
      ...contract, id: 9004, signal_key: "feed:4", theme: "Добыча", title_ru: "Роснефть купила сервисную компанию",
      event_type: "сделка (M&A)", region: "Россия", participants_json: [{ name: "Роснефть", quote: "Роснефть купила" }],
      evidence: [publication(5, {})],
    };
    const lift: Signal = {
      ...contract, id: 9005, signal_key: "feed:5", theme: "Добыча", title_ru: "SLB запустила установку мехдобычи",
      event_type: "запуск продукта", participants_json: [{ name: "SLB", quote: "SLB запустила" }],
      evidence: [publication(6, {})],
    };

    beforeEach(() => serve([contract, deal, lift]));

    it("справочники — в списках фильтров сверху, выбор уходит на сервер", async () => {
      renderFeed();
      fireEvent.click(await screen.findByRole("button", { name: "Развернуть всё" }));

      const eventType = await screen.findByRole("combobox", { name: "Тип события" });
      expect(within(eventType).getAllByRole("option").map((option) => option.textContent)).toEqual([
        "Любой тип", "пилот", "контракт", "сделка (M&A)", "нет события", "не определено",
      ]);
      fireEvent.change(eventType, { target: { value: "сделка (M&A)" } });

      await waitFor(() => expect(lastListQuery()).toMatchObject({ showcase: "business", eventType: "сделка (M&A)" }));
      await waitFor(() => expect(screen.queryByText("SLB получила контракт Saudi Aramco на бурение")).not.toBeInTheDocument());
      expect(screen.getByText("Роснефть купила сервисную компанию")).toBeInTheDocument();

      fireEvent.change(eventType, { target: { value: "" } });
      fireEvent.change(screen.getByRole("combobox", { name: "Регион" }), { target: { value: "Ближний Восток" } });
      fireEvent.change(screen.getByRole("combobox", { name: "Класс технологии" }), { target: { value: "неприменимо" } });
      fireEvent.change(screen.getByRole("combobox", { name: "Зрелость" }), { target: { value: "не определено" } });
      await waitFor(() => expect(lastListQuery()).toMatchObject({
        region: "Ближний Восток", technologyClass: "неприменимо", technologyMaturity: "не определено",
      }));
      expect(lastListQuery().eventType).toBeUndefined();
      // «Решение платформы» у сигнала ленты посчитает код (#132): фильтра по нему в ленте пока нет.
      expect(screen.queryByRole("combobox", { name: "Решение платформы" })).not.toBeInTheDocument();
    });

    it("клик по компании — все сигналы с её участием, в любой теме; «Сбросить» возвращает всё", async () => {
      renderFeed();
      fireEvent.click(await screen.findByRole("button", { name: "Развернуть всё" }));
      // Выбраны тема и регион, порядок — «сначала новые»: «всё про компанию» снимает фильтры, порядок оставляет.
      fireEvent.change(screen.getByRole("combobox", { name: /^Тема/ }), { target: { value: "Бурение" } });
      fireEvent.change(await screen.findByRole("combobox", { name: "Регион" }), { target: { value: "Ближний Восток" } });
      fireEvent.change(screen.getByRole("combobox", { name: /^Сортировка/ }), { target: { value: "date_desc" } });
      await waitFor(() => expect(lastListQuery()).toMatchObject({ theme: "Бурение", region: "Ближний Восток" }));
      const card = await cardOf("SLB получила контракт Saudi Aramco на бурение");

      fireEvent.click(within(card).getByRole("button", { name: "SLB" }));

      await waitFor(() => expect(lastListQuery()).toMatchObject({ showcase: "business", company: "SLB", sort: "date_desc" }));
      expect([lastListQuery().theme, lastListQuery().region]).toEqual([undefined, undefined]);
      expect(screen.getByRole("combobox", { name: /^Тема/ })).toHaveValue("");
      expect(screen.getByRole("combobox", { name: "Компания" })).toHaveValue("SLB");
      await waitFor(() => expect(screen.queryByText("Роснефть купила сервисную компанию")).not.toBeInTheDocument());
      // Блоки тем раскрыты сами: «всё про компанию» видно сразу, как при поиске.
      expect(screen.getByText("SLB запустила установку мехдобычи")).toBeInTheDocument();
      expect(screen.getByText("2 из 3 сигналов")).toBeInTheDocument();

      // Компанию вводят и руками: подсказки — канонические имена справочника алиасов.
      const input = screen.getByRole("combobox", { name: "Компания" });
      const suggestions = document.getElementById(input.getAttribute("list") ?? "") as HTMLDataListElement;
      expect([...suggestions.options].map((option) => option.value)).toEqual(DICTIONARIES.companies);
      fireEvent.click(screen.getByRole("button", { name: "Расширенные фильтры" }));
      fireEvent.click(screen.getByRole("button", { name: "Сбросить" }));

      await waitFor(() => expect(lastListQuery().company).toBeUndefined());
      expect(input).toHaveValue("");
    });

    it("компания с дочками по списку: рядом с полем видно, кого фильтр добавил; без дочек подсказки нет", async () => {
      // Сервер раскрывает компанию по списку «дочка → мать» и говорит, кого добавил (company_group в сводке).
      const groups: Record<string, string[]> = {
        "Газпром нефть": ["Газпром нефть", "Газпромнефть-Заполярье", "Газпромнефть-Хантос"],
      };
      const counts = vi.mocked(getSignalSummary).getMockImplementation()!;
      vi.mocked(getSignalSummary).mockImplementation(async (query: SignalQuery = {}) => ({
        ...(await counts(query)),
        ...(query.company ? { company_group: groups[query.company] ?? [query.company] } : {}),
      }));
      renderFeed();
      await screen.findByRole("button", { name: "Развернуть всё" });
      const input = screen.getByRole("combobox", { name: "Компания" });

      fireEvent.change(input, { target: { value: " Газпром нефть " } });

      expect(await screen.findByText("Газпром нефть и дочерние: Газпромнефть-Заполярье, Газпромнефть-Хантос"))
        .toBeInTheDocument();

      // Дочек у компании нет: сводка пришла (в выборке её сделка), подсказки нет.
      fireEvent.change(input, { target: { value: "Роснефть" } });

      expect(await screen.findByText("1 из 3 сигналов")).toBeInTheDocument();
      expect(screen.queryByText(/и дочерние:/)).not.toBeInTheDocument();
      expect(vi.mocked(getSignalSummary).mock.lastCall?.[0]).toMatchObject({ company: "Роснефть" });
    });

    it("компания — по имени целиком: пустая выборка подсказывает взять имя из подсказок", async () => {
      renderFeed();
      await screen.findByRole("button", { name: "Развернуть всё" });

      fireEvent.change(screen.getByRole("combobox", { name: "Компания" }), { target: { value: "Газпром" } });

      expect(await screen.findByText(/Компания ищется по имени целиком/)).toBeInTheDocument();
      expect(lastListQuery()).toMatchObject({ company: "Газпром" });
    });
  });

  it("блок ОС в ленте: оценка, причина и комментарий; блок добавляется в список на карточке (#130)", async () => {
    vi.mocked(createSignalFeedback).mockResolvedValue({
      ok: true, event_id: 7, memory_ids: [], memories: 0,
      block: { id: 7, author: "analyst@example.test", verdict: "approved", reason: "Меняет спрос на бурение",
               comment: "Взять в выпуск", created_at: "2026-10-23T09:00:00Z" },
    });
    renderFeed();
    const card = await cardOf("SLB получила контракт Saudi Aramco на бурение");
    fireEvent.click(within(card).getByRole("button", { name: "Раскрыть сигнал" }));
    fireEvent.click(within(card).getByRole("button", { name: "Обратная связь" }));

    // Блок ОС — оценка, причина, комментарий: правок карточки и «дубля» в ленте нет.
    expect(within(card).queryByLabelText("ID дубля")).not.toBeInTheDocument();
    expect(within(card).queryByLabelText("Рекомендуемый заголовок")).not.toBeInTheDocument();
    const verdicts = within(within(card).getByLabelText("Оценка сигнала")).getAllByRole("option").map((item) => item.textContent);
    expect(verdicts).toContain("Не тот блок — технологический сигнал, а не бизнес");
    expect(verdicts.some((label) => label?.startsWith("Дубль"))).toBe(false);
    fireEvent.change(within(card).getByLabelText("Оценка сигнала"), { target: { value: "approved" } });
    fireEvent.change(within(card).getByLabelText("Обоснование оценки"), { target: { value: "Меняет спрос на бурение" } });
    fireEvent.change(within(card).getByLabelText("Комментарий"), { target: { value: "Взять в выпуск" } });
    fireEvent.click(within(card).getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(createSignalFeedback).toHaveBeenCalledWith(expect.objectContaining({
      signal_id: contract.id, verdict: "approved", reason: "Меняет спрос на бурение", comment: "Взять в выпуск",
      corrected_title: null, duplicate_of_signal_id: null,
    })));
    const blocks = await within(card).findByRole("list", { name: "Оценки людей" });
    expect(within(blocks).getByText("analyst@example.test · 23.10.2026")).toBeInTheDocument();
    expect(within(blocks).getByText("Полезный сигнал")).toBeInTheDocument();
  });

  it("«В дайджест» в ленте — та же кнопка, что на радаре (#130)", async () => {
    vi.mocked(updateSignal).mockResolvedValue({ ok: true });
    renderFeed();
    const card = await cardOf("SLB получила контракт Saudi Aramco на бурение");

    fireEvent.click(within(card).getByRole("button", { name: "В дайджест" }));

    await waitFor(() => expect(updateSignal).toHaveBeenCalledWith(contract.id, { selected_for_digest: true }));
    expect(await within(card).findByRole("button", { name: "Убрать" })).toBeInTheDocument();
  });

  it("публикаций у карточки ленты — до 20, остальные — строкой «ещё N»", async () => {
    const many = Array.from({ length: 20 }, (_, index) => publication(100 + index, { title_ru: `Перепечатка ${index + 1}` }));
    serve([{ ...contract, evidence_count: 23, evidence: many }]);
    renderFeed();
    const card = await cardOf("SLB получила контракт Saudi Aramco на бурение");
    expect(lastListQuery()).toMatchObject({ evidenceLimit: 20 });

    fireEvent.click(within(card).getByRole("button", { name: "Раскрыть сигнал" }));

    const publications = within(card).getByText("Публикации").closest(".signalEvidenceList") as HTMLElement;
    expect(within(publications).getAllByRole("link")).toHaveLength(20);
    expect(within(publications).getByText("и ещё 3 публикации")).toBeInTheDocument();
  });

  it("в ленте нет «ранних карточек» радара: статья без тематики — в блоке своей темы", async () => {
    const unclassified = "Не классифицировано / новая тема";
    serve([contract, { ...fresh, theme: unclassified, theme_is_topic: false }]);
    renderFeed();

    expect(await screen.findByRole("button", { name: `Раскрыть группу ${unclassified}` })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Ранние карточки/ })).not.toBeInTheDocument();
  });

  it("подпись «почему» — по тому, кто написал: агент — «почему важно», ревью пачки радара — «почему интересно»", async () => {
    // Бизнес-находка радара: «почему интересно» дало ревью пачки, агент значение судьи не переписывает.
    const radarDeal: Signal = {
      ...contract, id: 9010, signal_key: "zena", origin: "radar", title_ru: "ZenaTech купила Velocity Geomatics",
      interest_score: 88, why_interesting: "Сильнее соседей по прогону: первая сделка с дронами для геофизики",
      card_set_by: { why_interesting: "radar_judge", event_date: "radar_judge" }, event_date: "2026-09-12",
      event_date_source: null, evidence: [publication(7, {})],
    };
    serve([contract, radarDeal]);
    renderFeed();
    const feedCard = await cardOf("SLB получила контракт Saudi Aramco на бурение");
    const radarCard = await cardOf("ZenaTech купила Velocity Geomatics");
    fireEvent.click(within(feedCard).getByRole("button", { name: "Раскрыть сигнал" }));
    fireEvent.click(within(radarCard).getByRole("button", { name: "Раскрыть сигнал" }));

    expect(within(feedCard).getByText("Почему важно").nextSibling).toHaveTextContent("Крупнейший контракт");
    expect(within(radarCard).queryByText("Почему важно")).not.toBeInTheDocument();
    expect(within(radarCard).getByText("Почему интересно · 88").nextSibling).toHaveTextContent("Сильнее соседей");
    // Источник даты события — и у даты судьи радара.
    expect(within(radarCard).getByText("Событие: 12.09.2026 · по оценке судьи")).toBeInTheDocument();
  });
});

describe("технологический радар", () => {
  // Находка радара: дату события и связь с нефтесервисом поставил судья, пустое дополнил агент.
  const robot: Signal = {
    ...contract,
    id: 501,
    signal_key: "k501",
    origin: "radar",
    signal_category: "technology",
    title_ru: "Робот-бурильщик на платформе Equinor",
    maturity: "shortlist",
    score_profile: "tech_radar",
    criteria_snapshot: [{ id: 1, name: "Ценность для нефтесервиса", weight: 30 }],
    event_date: "2026-09-01",
    event_date_source: null,
    event_type: "пилот",
    region: "Европа",
    technology_class: "робототехника",
    technology_maturity: "пилот/ОПИ",
    oilfield_relevance: "transferable",
    participants_json: [{ name: "Equinor", quote: "Equinor испытала" }, { name: "SLB", quote: "SLB поставила" }],
    card_set_by: {
      ...Object.fromEntries(AGENT_FIELDS.map((field) => [field, "agent"])),
      event_date: "radar_judge",
      oilfield_relevance: "radar_judge",
    },
  };

  it("прежний радар плюс чипы атрибутов: «в дайджест», отзыв и решение платформы — на месте", async () => {
    serve([robot]);
    render(<SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} />);

    expect(await screen.findByRole("heading", { name: "Технологический радар" })).toBeInTheDocument();
    const card = await cardOf("Робот-бурильщик на платформе Equinor");
    expect(lastListQuery().showcase).toBeUndefined();
    expect(chips(card)).toEqual([
      ["Категория: технология", false],
      ["Тип события: пилот", false],
      ["Регион: Европа", false],
      ["Класс технологии: робототехника", false],
      ["Зрелость: пилот/ОПИ", false],
      ["Связь с нефтесервисом: переносимая", false],
      ["Компании: Equinor SLB", false],
    ]);
    // Дату поставил судья радара — так и подписана.
    expect(within(card).getByText("Событие: 01.09.2026 · по оценке судьи")).toBeInTheDocument();
    // «Наблюдать / кандидат / подтверждено» — решение платформы, а не зрелость технологии.
    expect(within(card).getByText(/· Решение платформы: Кандидат ·/)).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "В дайджест" })).toBeInTheDocument();
    fireEvent.click(within(card).getByRole("button", { name: "Раскрыть сигнал" }));
    expect(within(card).getByRole("button", { name: "Обратная связь" })).toBeInTheDocument();
    expect(within(card).getByText("Ссылки")).toBeInTheDocument();
    // Ревью пачки «почему интересно» не дало — пустое поле заполнил агент: это «почему важно».
    expect(within(card).getByText("Почему важно").nextSibling).toHaveTextContent("Крупнейший контракт");

    expect(screen.getByRole("combobox", { name: "Решение платформы" })).toBeInTheDocument();
    fireEvent.change(await screen.findByRole("combobox", { name: "Зрелость" }), { target: { value: "пилот/ОПИ" } });
    await waitFor(() => expect(lastListQuery()).toMatchObject({ technologyMaturity: "пилот/ОПИ" }));
    // «Всё про компанию» и на радаре: прочие фильтры снимаются, витрина — технологии.
    fireEvent.click(within(await cardOf("Робот-бурильщик на платформе Equinor")).getByRole("button", { name: "SLB" }));
    await waitFor(() => expect(lastListQuery()).toMatchObject({ company: "SLB" }));
    expect([lastListQuery().technologyMaturity, lastListQuery().showcase]).toEqual([undefined, undefined]);
  });
});
