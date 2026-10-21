import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CompanyParent, CompanyParentLine } from "../../api/companyParents";
import { CompanyParentsPage } from "./CompanyParentsPage";

const CYCLE = "Получится цикл: «Газпром» → «Газпром нефть» → «Газпром»";

const pairsFixture: CompanyParent[] = [
  {
    id: 1, child_name: "Газпром нефть", parent_name: "Газпром", note: "контроль 95%", created_by: 7,
    created_by_email: "viktor@example.test", created_at: "2026-10-20T09:30:00+00:00",
  },
  {
    id: 2, child_name: "Газпромнефть-Заполярье", parent_name: "Газпром нефть", note: null, created_by: null,
    created_by_email: null, created_at: "2026-10-20T10:00:00+00:00",
  },
];

// Имена участников карточек из базы: «Роснефть» есть и в справочнике, подсказка одна.
const participants = ["Газпромнефть-Хантос", "Роснефть"];

const checkedLines: CompanyParentLine[] = [
  { line: 1, child_name: "Башнефть", parent_name: "Роснефть", note: null, problem: null },
  { line: 3, child_name: "Газпром", parent_name: "Газпром нефть", note: null, problem: CYCLE },
];

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}

// Подставной сервер: список живёт в переменной, ответы вставки и отказ формы задаёт тест.
let pairs: CompanyParent[] = [];
let addRefusal: string | null = null;
let importAnswer: (apply: boolean) => Response = () => json({ detail: "не задано" }, 500);

function route(input: RequestInfo | URL, init?: RequestInit): Response {
  const url = String(input);
  const method = init?.method ?? "GET";
  const body = init?.body ? JSON.parse(String(init.body)) : undefined;
  if (url === "/api/signals/dictionaries") {
    return json({ event_type: [], region: [], technology_class: [], technology_maturity: [], companies: ["Роснефть", "Газпром"] });
  }
  if (url === "/api/company-parents" && method === "GET") return json({ pairs, names: participants });
  if (url === "/api/company-parents" && method === "POST") {
    if (addRefusal) return json({ detail: addRefusal }, 400);
    const pair = { id: 10 + pairs.length, ...body, created_by: 5, created_by_email: "admin@example.test", created_at: "2026-10-20T12:00:00+00:00" };
    pairs = [...pairs, pair];
    return json({ ok: true, pair });
  }
  const removed = url.match(/^\/api\/company-parents\/(\d+)$/);
  if (removed && method === "DELETE") {
    pairs = pairs.filter((pair) => pair.id !== Number(removed[1]));
    return json({ ok: true });
  }
  if (url === "/api/company-parents/import" && method === "POST") return importAnswer(Boolean(body.apply));
  return json({ detail: "Not Found" }, 404);
}

const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => Promise.resolve(route(input, init)));

function calls(method: string, url: string) {
  return fetchMock.mock.calls.filter(([input, init]) => String(input) === url && (init?.method ?? "GET") === method);
}

function bodyOf(call: [RequestInfo | URL, RequestInit?]) {
  return JSON.parse(String(call[1]?.body));
}

describe("экран «Дочерние общества»", () => {
  beforeEach(() => {
    pairs = [...pairsFixture];
    addRefusal = null;
    fetchMock.mockClear();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("показывает список «дочка → мать, кто, когда» и подсказывает имена справочника, участников и списка", async () => {
    render(<CompanyParentsPage onUnauthorized={() => {}} showToast={() => {}} />);

    const zapolyarye = (await screen.findByRole("cell", { name: "Газпромнефть-Заполярье" })).closest("tr") as HTMLElement;
    const cellsOf = (row: HTMLElement) => within(row).getAllByRole("cell").map((cell) => cell.textContent);
    // Учётку автора удалили — «кто» пусто.
    expect(cellsOf(zapolyarye).slice(0, 5)).toEqual(["Газпромнефть-Заполярье", "→Газпром нефть", "", "", "20.10.2026, 13:00"]);
    const neft = screen.getByRole("cell", { name: "контроль 95%" }).closest("tr") as HTMLElement;
    expect(cellsOf(neft).slice(0, 5)).toEqual(["Газпром нефть", "→Газпром", "контроль 95%", "viktor@example.test", "20.10.2026, 12:30"]);
    expect(screen.getByText("2 связи")).toBeInTheDocument();

    await waitFor(() => expect(document.querySelectorAll("#company-parents-names option")).toHaveLength(5));
    const options = [...document.querySelectorAll("#company-parents-names option")].map((option) => option.getAttribute("value"));
    expect(options).toEqual(expect.arrayContaining(["Газпром", "Газпром нефть", "Газпромнефть-Заполярье", "Газпромнефть-Хантос", "Роснефть"]));
    expect(screen.getByLabelText("Дочка")).toHaveAttribute("list", "company-parents-names");
  });

  it("добавляет связь, а отказ сервера показывает под формой", async () => {
    const user = userEvent.setup();
    const showToast = vi.fn();
    render(<CompanyParentsPage onUnauthorized={() => {}} showToast={showToast} />);
    await screen.findByRole("cell", { name: "Газпромнефть-Заполярье" });

    await user.type(screen.getByLabelText("Дочка"), "Башнефть");
    await user.type(screen.getByLabelText("Мать"), "Роснефть");
    await user.click(screen.getByRole("button", { name: "Добавить" }));

    expect(await screen.findByRole("cell", { name: "Башнефть" })).toBeInTheDocument();
    expect(bodyOf(calls("POST", "/api/company-parents")[0])).toEqual({ child_name: "Башнефть", parent_name: "Роснефть", note: null });
    expect(screen.getByLabelText("Дочка")).toHaveValue("");
    expect(showToast).toHaveBeenCalledWith("Связь добавлена");

    addRefusal = "У «Газпром нефть» уже есть мать «Газпром»: удалите старую строку";
    await user.type(screen.getByLabelText("Дочка"), "Газпром нефть");
    await user.type(screen.getByLabelText("Мать"), "Роснефть");
    await user.click(screen.getByRole("button", { name: "Добавить" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(addRefusal);
    expect(screen.getByLabelText("Дочка")).toHaveValue("Газпром нефть");
  });

  it("удаляет связь только после подтверждения", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<CompanyParentsPage onUnauthorized={() => {}} showToast={() => {}} />);
    const remove = await screen.findByRole("button", { name: "Удалить связь «Газпромнефть-Заполярье» → «Газпром нефть»" });

    await user.click(remove);
    expect(confirm).toHaveBeenCalledWith("Удалить связь «Газпромнефть-Заполярье» → «Газпром нефть»?");
    expect(calls("DELETE", "/api/company-parents/2")).toHaveLength(0);

    await user.click(remove);
    await waitFor(() => expect(screen.queryByRole("cell", { name: "Газпромнефть-Заполярье" })).not.toBeInTheDocument());
    expect(calls("DELETE", "/api/company-parents/2")).toHaveLength(1);
    expect(screen.getByRole("cell", { name: "Газпром нефть" })).toBeInTheDocument();
  });

  it("вставка списком: ошибки разбора по строкам, с ошибкой ничего не сохраняется", async () => {
    const user = userEvent.setup();
    const showToast = vi.fn();
    render(<CompanyParentsPage onUnauthorized={() => {}} showToast={showToast} />);
    await screen.findByRole("cell", { name: "Газпромнефть-Заполярье" });
    const paste = screen.getByLabelText("Строки «дочка; мать»");
    await user.type(paste, "Башнефть; Роснефть{Enter}{Enter}Газпром; Газпром нефть");

    importAnswer = () => json({ ok: false, applied: false, problems: 1, lines: checkedLines });
    await user.click(screen.getByRole("button", { name: "Проверить" }));

    const analysis = await screen.findByRole("region", { name: "Разбор строк" });
    expect(within(analysis).getByRole("status")).toHaveTextContent("Строк: 2, с ошибками: 1");
    const bad = within(analysis).getByRole("cell", { name: CYCLE }).closest("tr") as HTMLElement;
    expect(within(bad).getByRole("cell", { name: "3" })).toBeInTheDocument();
    const fine = within(analysis).getByRole("cell", { name: "Башнефть" }).closest("tr") as HTMLElement;
    expect(fine).toHaveTextContent("в порядке");
    expect(bodyOf(calls("POST", "/api/company-parents/import")[0])).toEqual({
      text: "Башнефть; Роснефть\n\nГазпром; Газпром нефть",
      apply: false,
    });

    // «Сохранить» при ошибке: сервер отвечает 400 с тем же разбором и ничего не пишет.
    importAnswer = () => json({ detail: { message: "Ничего не записано: строк с ошибками 1 из 2", problems: 1, lines: checkedLines } }, 400);
    const listLoads = calls("GET", "/api/company-parents").length;
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(showToast).toHaveBeenCalledWith("Ничего не записано: строк с ошибками 1", "error"));
    expect(within(screen.getByRole("region", { name: "Разбор строк" })).getByRole("cell", { name: CYCLE })).toBeInTheDocument();
    expect(calls("GET", "/api/company-parents")).toHaveLength(listLoads);
    expect(paste).toHaveValue("Башнефть; Роснефть\n\nГазпром; Газпром нефть");

    // Исправили текст: старый разбор уходит; сохранение без ошибок пишет и обновляет список.
    await user.clear(paste);
    await user.type(paste, "Башнефть; Роснефть");
    expect(screen.queryByRole("region", { name: "Разбор строк" })).not.toBeInTheDocument();
    importAnswer = () => {
      pairs = [...pairs, { ...pairsFixture[0], id: 3, child_name: "Башнефть", parent_name: "Роснефть", note: null }];
      return json({ ok: true, applied: true, problems: 0, lines: [checkedLines[0]] });
    };
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByRole("cell", { name: "Башнефть" })).toBeInTheDocument();
    expect(showToast).toHaveBeenCalledWith("Записано связей: 1");
    expect(paste).toHaveValue("");
  });
});
