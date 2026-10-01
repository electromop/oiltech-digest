import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ScoringPage } from "./ScoringPage";

function criterion(id: number, name: string, weight: number, profile: string) {
  return { id, name, description: "", weight, keywords_json: [], keywords_en_json: [], sort_order: id * 10, profile };
}

const business = [
  criterion(1, "Стратегическая значимость для нефтесервиса", 60, "business"),
  criterion(2, "Потенциальный бизнес-эффект", 40, "business"),
];
// Сумма 65: у вкладки своя сумма весов, а не общая с «Бизнес-сигналами».
const techRadar = [
  criterion(11, "Ценность для нефтесервиса", 30, "tech_radar"),
  criterion(12, "Технологическая новизна", 25, "tech_radar"),
  criterion(13, "Свежесть", 10, "tech_radar"),
];

function json(payload: unknown) {
  return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } });
}

const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input);
  const method = init?.method ?? "GET";
  if (method === "GET" && url === "/api/scoring-criteria?profile=business") return Promise.resolve(json(business));
  if (method === "GET" && url === "/api/scoring-criteria?profile=tech_radar") return Promise.resolve(json(techRadar));
  return Promise.resolve(json({ ok: true, saved: 3, weight_sum: 100 }));
});

function requested(method: string) {
  return fetchMock.mock.calls
    .filter(([, init]) => (init?.method ?? "GET") === method)
    .map(([input]) => String(input));
}

function renderPage() {
  render(<ScoringPage onUnauthorized={() => {}} showToast={() => {}} />);
}

describe("экран «Скоринг»: вкладки наборов критериев", () => {
  beforeEach(() => {
    fetchMock.mockClear();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("вкладка «Технологический радар» грузит свой набор, свою сумму и говорит, что им оцениваются карточки радара", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm");
    renderPage();

    expect(await screen.findByDisplayValue("Стратегическая значимость для нефтесервиса")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Бизнес-сигналы" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Сумма весов: 100%")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Технологический радар" }));

    expect(await screen.findByDisplayValue("Ценность для нефтесервиса")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Потенциальный бизнес-эффект")).not.toBeInTheDocument();
    expect(requested("GET")).toEqual(["/api/scoring-criteria?profile=business", "/api/scoring-criteria?profile=tech_radar"]);
    expect(screen.getByRole("tab", { name: "Технологический радар" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Сумма весов: 65%")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Сохранить" })).toBeDisabled();
    // Профиль подключён к радару (ADR 0002, Б3): плашка больше не говорит «пока не входит».
    expect(screen.getByRole("status")).toHaveTextContent("Этим набором оцениваются карточки технологического радара");
    expect(screen.getByRole("status")).toHaveTextContent("со следующего прогона радара");
    expect(screen.getByRole("status")).not.toHaveTextContent("пока не");
    expect(confirm).not.toHaveBeenCalled(); // правок не было — и спрашивать не о чем
  });

  it("«Сохранить» уходит в набор открытой вкладки", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByDisplayValue("Стратегическая значимость для нефтесервиса");
    await user.click(screen.getByRole("tab", { name: "Технологический радар" }));
    await screen.findByDisplayValue("Свежесть");

    fireEvent.change(screen.getAllByLabelText("Вес")[2], { target: { value: "45" } });
    expect(screen.getByText("Сумма весов: 100%")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(requested("PUT")).toEqual(["/api/scoring-criteria?profile=tech_radar"]));
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT")!;
    const saved = JSON.parse(String((put[1] as RequestInit).body)) as Array<{ id: number; weight: number }>;
    expect(saved.map((item) => [item.id, item.weight])).toEqual([[11, 30], [12, 25], [13, 45]]);
  });

  it("ключевые слова делятся и по переносу строки, как на экране «Теги»", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByDisplayValue("Стратегическая значимость для нефтесервиса");
    const [ru] = screen.getAllByLabelText("Ключевые слова RU / любые");
    const [en] = screen.getAllByLabelText("EN-нормализация");

    // Набор столбиком: перевод строки в конце не съедается, пока поле в фокусе.
    await user.type(ru, "ГРП{Enter}гидроразрыв,{Enter}грп");
    expect(ru).toHaveValue("ГРП\nгидроразрыв,\nгрп");
    // Вставка столбика из таблицы — отдельные слова, пустые строки отброшены.
    fireEvent.change(en, { target: { value: "hydraulic fracturing\n\nproppant" } });
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(requested("PUT")).toEqual(["/api/scoring-criteria?profile=business"]));
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT")!;
    const [first] = JSON.parse(String((put[1] as RequestInit).body)) as Array<{ keywords_json: string[]; keywords_en_json: string[] }>;
    expect(first.keywords_json).toEqual(["ГРП", "гидроразрыв"]); // повтор без учёта регистра не добавлен
    expect(first.keywords_en_json).toEqual(["hydraulic fracturing", "proppant"]);
  });

  it("уход с вкладки с несохранёнными правками — только после подтверждения", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPage();
    await screen.findByDisplayValue("Стратегическая значимость для нефтесервиса");

    fireEvent.change(screen.getAllByLabelText("Вес")[0], { target: { value: "50" } });
    await user.click(screen.getByRole("tab", { name: "Технологический радар" }));

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("«Бизнес-сигналы»"));
    expect(screen.getByRole("tab", { name: "Бизнес-сигналы" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByDisplayValue("50")).toBeInTheDocument(); // правка на месте
    expect(requested("GET")).toEqual(["/api/scoring-criteria?profile=business"]);

    confirm.mockReturnValue(true);
    await user.click(screen.getByRole("tab", { name: "Технологический радар" }));

    expect(await screen.findByDisplayValue("Ценность для нефтесервиса")).toBeInTheDocument();
    expect(requested("PUT")).toEqual([]); // правка отброшена, а не сохранена молча
  });
});
