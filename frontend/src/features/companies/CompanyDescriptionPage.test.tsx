import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CompanyDescriptionPage } from "./CompanyDescriptionPage";

// Текста описания нет в репозитории (#162): тест берёт заглушку.
const STUB = "Заглушка описания: бурение, ТКРС, геологоразведка.";

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

describe("экран «Описание Компании» (#162)", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") {
        const text = JSON.parse(String(init.body)).text;
        return Promise.resolve(jsonResponse({
          text, updated_by: "editor@example.test", updated_at: "2026-11-04T10:00:00Z",
          history: [{ updated_by: "editor@example.test", updated_at: "2026-11-04T10:00:00Z", chars: text.length }],
        }));
      }
      return Promise.resolve(jsonResponse({ text: "", updated_by: null, updated_at: null, history: [] }));
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  it("предупреждает, что текст уходит внешней модели, подсказывает, что писать, и показывает, кто и когда правил", async () => {
    render(<CompanyDescriptionPage onUnauthorized={() => undefined} showToast={() => undefined} />);
    expect(screen.getByRole("note")).toHaveTextContent("Текст уходит внешней модели");
    expect(screen.getByRole("note")).toHaveTextContent("утверждённую заказчиком");
    expect(screen.getByText(/функции \(бурение, ТКРС, геологоразведка, промышленная безопасность\), а не подразделения/))
      .toBeInTheDocument();
    expect(await screen.findByText(/Описания ещё нет/)).toBeInTheDocument();

    fireEvent.change(screen.getByRole("textbox", { name: "Описание" }), { target: { value: STUB } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith("/api/company-description", expect.objectContaining({ method: "PUT" })),
    );
    expect(await screen.findByText(/Последняя правка: editor@example\.test, 04\.11\.2026/)).toBeInTheDocument();
  });
});
