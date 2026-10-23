import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getSignalSummary, type SignalQuery } from "../../api/signals";
import type { Signal } from "../../api/types";
import {
  DICTIONARIES, cardOf, contract, lastListQuery, publication, renderFeed, resetBusinessFeedApi, serve,
} from "./businessFeedFixtures";

// Бизнес-лента (#97, #130): фильтры атрибутов в панели «Фильтры · N» и «всё про компанию». Шов 3.

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

describe("бизнес-лента: фильтры и «всё про компанию»", () => {
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
    fireEvent.click(screen.getByRole("button", { name: "Фильтры" }));

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
    // Те же фильтры, что на радаре, и «Решение платформы» тоже.
    expect(screen.getByRole("combobox", { name: "Решение платформы" })).toBeInTheDocument();
  });

  it("клик по компании — все сигналы с её участием, в любой теме; «Сбросить» возвращает всё", async () => {
    renderFeed();
    fireEvent.click(await screen.findByRole("button", { name: "Развернуть всё" }));
    fireEvent.click(screen.getByRole("button", { name: "Фильтры" }));
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
    expect(screen.getByRole("button", { name: "Снять: Компания: SLB" })).toBeInTheDocument();
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
    fireEvent.click(screen.getByRole("button", { name: "Фильтры" }));
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
    fireEvent.click(screen.getByRole("button", { name: "Фильтры" }));

    fireEvent.change(screen.getByRole("combobox", { name: "Компания" }), { target: { value: "Газпром" } });

    expect(await screen.findByText(/Компания ищется по имени целиком/)).toBeInTheDocument();
    expect(lastListQuery()).toMatchObject({ company: "Газпром" });
  });
});
