import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { deleteDocument, listDocuments } from "../../api/documents";
import { deleteColleagueMaterial, listColleagueMaterials, updateColleagueMaterial } from "../../api/materials";
import type { ColleagueMaterial, UploadedDocument } from "../../api/types";
import { MaterialsPage } from "./MaterialsPage";

vi.mock("../../api/materials", () => ({
  listColleagueMaterials: vi.fn(),
  updateColleagueMaterial: vi.fn(),
  deleteColleagueMaterial: vi.fn(),
  colleagueMaterialFileUrl: (id: number) => `/api/materials/${id}/file`,
}));

vi.mock("../../api/documents", () => ({
  listDocuments: vi.fn(),
  getDocument: vi.fn(),
  deleteDocument: vi.fn(),
  uploadDocument: vi.fn(),
  documentOriginalUrl: (id: number) => `/api/documents/${id}/original`,
}));

function material(changes: Partial<ColleagueMaterial>): ColleagueMaterial {
  return {
    id: 1, kind: "event", kind_label: "Событие — описание своими словами", title: "Татнефть выбрала подрядчика",
    description: "Итог тендера на ГРП", url: null, companies: ["Татнефть"], verdict: "strong_signal",
    verdict_label: "Сильный сигнал — вынести в дайджест / обсуждать", verdict_comment: null, file_name: null,
    file_size: null, has_file: false, author: "Иван Иванов", author_username: "ivanov", signal_id: null,
    status: "accepted", status_label: "Принят на платформу",
    created_at: "2026-10-19T10:00:00+03:00", ...changes,
  };
}

const uploaded: UploadedDocument = {
  id: 9, filename: "Отчёт о ГРП.pdf", kind: "pdf", size_bytes: 4096, status: "ready", error_message: null,
  anchor_unit: "page", anchor_count: 4, empty_anchors: 0, text_chars: 1200, essence: "Отчёт об испытаниях ГРП",
  doc_type: "отчёт", publisher: "ДО", fact_count: 3, created_at: "2026-10-20T10:00:00+03:00",
};

const verdicts = [
  { value: "strong_signal", label: "Сильный сигнал — вынести в дайджест / обсуждать" },
  { value: "reject", label: "Низкая ценность / шум — по теме, но без новой ценности" },
];

describe("MaterialsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listColleagueMaterials).mockResolvedValue({
      ok: true,
      materials: [
        material({}),
        material({ id: 2, kind: "file", kind_label: "Файл + описание", title: "Обзор рынка", verdict: "reject",
          verdict_label: "Низкая ценность / шум — по теме, но без новой ценности", has_file: true,
          file_name: "обзор.pdf", file_size: 2048 }),
      ],
      verdicts,
    });
    vi.mocked(listDocuments).mockResolvedValue({ documents: [uploaded] } as never);
    vi.mocked(updateColleagueMaterial).mockReset();
    vi.mocked(deleteColleagueMaterial).mockReset();
    vi.mocked(deleteDocument).mockReset();
  });

  it("одним списком: у каждой записи видно, откуда она, фильтры по оценке и по источнику", async () => {
    render(<MaterialsPage onUnauthorized={vi.fn()} showToast={vi.fn()} isAdmin />);

    const fromBot = (await screen.findByText("Татнефть выбрала подрядчика")).closest("article") as HTMLElement;
    expect(within(fromBot).getByText("Telegram · бот")).toBeTruthy();
    expect(within(fromBot).getByText(/Иван Иванов \(@ivanov\)/)).toBeTruthy();
    const document = screen.getByText("Отчёт о ГРП.pdf").closest("article") as HTMLElement;
    expect(within(document).getByText("Внесено на платформе")).toBeTruthy();
    expect(screen.getByText(/обзор\.pdf/).closest("a")?.getAttribute("href")).toBe("/api/materials/2/file");

    fireEvent.change(screen.getByLabelText("Оценка автора"), { target: { value: "reject" } });
    expect(screen.queryByText("Татнефть выбрала подрядчика")).toBeNull();
    expect(screen.queryByText("Отчёт о ГРП.pdf")).toBeNull();
    expect(screen.getByText("Обзор рынка")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Оценка автора"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Внесено" }));
    expect(screen.getByText("Отчёт о ГРП.pdf")).toBeTruthy();
    expect(screen.queryByText("Обзор рынка")).toBeNull();
  });

  it("показывает, где материал в воронке платформы", async () => {
    vi.mocked(listColleagueMaterials).mockResolvedValue({
      ok: true,
      materials: [material({}), material({ id: 3, title: "Контракт на ГРП", signal_id: 7, status: "signal", status_label: "Стал сигналом №7" })],
      verdicts: [],
    });

    render(<MaterialsPage onUnauthorized={vi.fn()} showToast={vi.fn()} />);

    expect(await screen.findByText("Принят на платформу")).toBeTruthy();
    expect(screen.getByText("Стал сигналом №7")).toBeTruthy();
  });

  it("админ правит материал из бота в подокне", async () => {
    vi.mocked(updateColleagueMaterial).mockResolvedValue({ ok: true, material: material({ title: "Татнефть продлила договор по ГРП" }) });
    render(<MaterialsPage onUnauthorized={vi.fn()} showToast={vi.fn()} isAdmin />);

    const card = (await screen.findByText("Татнефть выбрала подрядчика")).closest("article") as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: "Править" }));
    fireEvent.change(screen.getByLabelText("Заголовок"), { target: { value: "Татнефть продлила договор по ГРП" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(updateColleagueMaterial).toHaveBeenCalled());
    expect(vi.mocked(updateColleagueMaterial).mock.calls[0][0]).toBe(1);
    expect(vi.mocked(updateColleagueMaterial).mock.calls[0][1]).toMatchObject({ title: "Татнефть продлила договор по ГРП", companies: ["Татнефть"] });
    expect(await screen.findByText("Татнефть продлила договор по ГРП")).toBeTruthy();
  });

  it("удаление — только после подтверждения", async () => {
    vi.mocked(deleteColleagueMaterial).mockResolvedValue({ ok: true });
    render(<MaterialsPage onUnauthorized={vi.fn()} showToast={vi.fn()} isAdmin />);

    const card = (await screen.findByText("Обзор рынка")).closest("article") as HTMLElement;
    fireEvent.click(within(card).getByRole("button", { name: "Удалить" }));
    expect(deleteColleagueMaterial).not.toHaveBeenCalled();
    fireEvent.click(within(card).getByRole("button", { name: "Да, удалить" }));

    await waitFor(() => expect(deleteColleagueMaterial).toHaveBeenCalledWith(2));
  });

  it("обычный пользователь видит материалы из бота, но не вносит, не правит и не удаляет", async () => {
    render(<MaterialsPage onUnauthorized={vi.fn()} showToast={vi.fn()} />);

    expect(await screen.findByText("Татнефть выбрала подрядчика")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Внести материал" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Править" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Удалить" })).toBeNull();
    expect(listDocuments).not.toHaveBeenCalled();
  });

  it("«Внести материал» открывает подокно загрузки, условия проверяются при отправке", async () => {
    render(<MaterialsPage onUnauthorized={vi.fn()} showToast={vi.fn()} isAdmin />);

    fireEvent.click(await screen.findByRole("button", { name: "Внести материал" }));
    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Выберите файл материала");
  });
});
