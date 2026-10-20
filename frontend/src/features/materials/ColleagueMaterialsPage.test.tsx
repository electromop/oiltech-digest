import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { listColleagueMaterials } from "../../api/materials";
import type { ColleagueMaterial } from "../../api/types";
import { ColleagueMaterialsPage } from "./ColleagueMaterialsPage";

vi.mock("../../api/materials", () => ({
  listColleagueMaterials: vi.fn(),
  colleagueMaterialFileUrl: (id: number) => `/api/materials/${id}/file`,
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

describe("ColleagueMaterialsPage", () => {
  beforeEach(() => {
    vi.mocked(listColleagueMaterials).mockResolvedValue({
      ok: true,
      materials: [
        material({}),
        material({ id: 2, kind: "file", kind_label: "Файл + описание", title: "Обзор рынка", verdict: "reject",
          verdict_label: "Низкая ценность / шум — по теме, но без новой ценности", has_file: true,
          file_name: "обзор.pdf", file_size: 2048 }),
      ],
      verdicts: [
        { value: "strong_signal", label: "Сильный сигнал — вынести в дайджест / обсуждать" },
        { value: "reject", label: "Низкая ценность / шум — по теме, но без новой ценности" },
      ],
    });
  });

  it("shows materials with file link and filters by verdict", async () => {
    render(<ColleagueMaterialsPage onUnauthorized={vi.fn()} showToast={vi.fn()} />);

    expect(await screen.findByText("Татнефть выбрала подрядчика")).toBeTruthy();
    expect(screen.getByText(/обзор\.pdf/).closest("a")?.getAttribute("href")).toBe("/api/materials/2/file");

    fireEvent.change(screen.getByLabelText("Оценка"), { target: { value: "reject" } });

    expect(screen.queryByText("Татнефть выбрала подрядчика")).toBeNull();
    expect(screen.getByText("Обзор рынка")).toBeTruthy();
  });

  it("shows where each material is in the platform funnel", async () => {
    vi.mocked(listColleagueMaterials).mockResolvedValue({
      ok: true,
      materials: [
        material({}),
        material({ id: 3, title: "Контракт на ГРП", signal_id: 7, status: "signal", status_label: "Стал сигналом №7" }),
      ],
      verdicts: [],
    });

    render(<ColleagueMaterialsPage onUnauthorized={vi.fn()} showToast={vi.fn()} />);

    expect(await screen.findByText("Принят на платформу")).toBeTruthy();
    expect(screen.getByText("Стал сигналом №7")).toBeTruthy();
  });
});
