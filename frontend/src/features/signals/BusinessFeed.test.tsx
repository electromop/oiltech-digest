import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  createSignalFeedback,
  getSignalSummary,
  getSignalWeekly,
  getSignalSearchHealth,
  updateSignal,
} from "../../api/signals";
import type { Signal } from "../../api/types";
import { SignalRadarPage } from "./SignalRadarPage";
import {
  AGENT_FIELDS, cardOf, chips, contract, forum, fresh, lastListQuery, publication, renderFeed,
  resetBusinessFeedApi, serve,
} from "./businessFeedFixtures";

// Бизнес-лента (#97, спека #93): экран радара в витрине «бизнес» — карточка события, атрибуты чипами,
// клик по компании — «всё про компанию». Фильтры — BusinessFeedFilters.test.tsx. Шов 3: экран на подставном API.

vi.mock("../../api/signals", () => ({
  listSignals: vi.fn(),
  getSignalSummary: vi.fn(),
  getSignalDictionaries: vi.fn(),
  updateSignal: vi.fn(),
  createSignalFeedback: vi.fn(),
  getSignalSearchHealth: vi.fn(),
  getSignalWeekly: vi.fn(),
}));

beforeEach(resetBusinessFeedApi);

describe("бизнес-лента", () => {
  it("экран радара в витрине «бизнес»: карточка события, публикации снизу, «в дайджест» и ОС, как на радаре", async () => {
    renderFeed(true);

    expect(await screen.findByRole("heading", { name: "Бизнес-лента" })).toBeInTheDocument();
    const card = await cardOf("SLB получила контракт Saudi Aramco на бурение");
    expect(lastListQuery()).toMatchObject({ showcase: "business" });
    // Лента устроена так же, как радар (решение владельца 23.10): и «Решение платформы» на карточке.
    expect(within(card).getByText(/^Бурение · 2 публикации · Решение платформы: Наблюдать ·/)).toBeInTheDocument();
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
    // Служебное радара — «Итоги недели», здоровье поиска — не про ленту; плашки и «Отклонённые» — те же.
    expect(getSignalWeekly).not.toHaveBeenCalled();
    expect(getSignalSearchHealth).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Отклонённые: 0" })).toBeInTheDocument();
    expect(screen.getByText("Объединено дублей")).toBeInTheDocument();
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

  it("у находки радара в ленте отзыв — тоже блок ОС: судью радара он не учит (#130)", async () => {
    const radarDeal: Signal = { ...contract, id: 9011, signal_key: "zena", origin: "radar",
                                title_ru: "ZenaTech купила Velocity Geomatics", evidence: [publication(8, {})] };
    serve([radarDeal]);
    vi.mocked(createSignalFeedback).mockResolvedValue({ ok: true, event_id: 9, memory_ids: [], memories: 0 });
    renderFeed();
    const card = await cardOf("ZenaTech купила Velocity Geomatics");
    fireEvent.click(within(card).getByRole("button", { name: "Раскрыть сигнал" }));
    fireEvent.click(within(card).getByRole("button", { name: "Обратная связь" }));

    expect(within(card).queryByLabelText("ID дубля")).not.toBeInTheDocument();
    const verdicts = within(within(card).getByLabelText("Оценка сигнала")).getAllByRole("option").map((item) => item.textContent);
    expect(verdicts).toContain("Не тот блок — технологический сигнал, а не бизнес");
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

    fireEvent.click(screen.getByRole("button", { name: "Фильтры" }));
    expect(screen.getByRole("combobox", { name: "Решение платформы" })).toBeInTheDocument();
    fireEvent.change(await screen.findByRole("combobox", { name: "Зрелость" }), { target: { value: "пилот/ОПИ" } });
    await waitFor(() => expect(lastListQuery()).toMatchObject({ technologyMaturity: "пилот/ОПИ" }));
    // «Всё про компанию» и на радаре: прочие фильтры снимаются, витрина — технологии.
    fireEvent.click(within(await cardOf("Робот-бурильщик на платформе Equinor")).getByRole("button", { name: "SLB" }));
    await waitFor(() => expect(lastListQuery()).toMatchObject({ company: "SLB" }));
    expect([lastListQuery().technologyMaturity, lastListQuery().showcase]).toEqual([undefined, undefined]);
  });
});