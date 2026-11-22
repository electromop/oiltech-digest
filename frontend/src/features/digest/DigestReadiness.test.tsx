import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DigestContentItem, DigestMaterial } from "../../api/types";
import { DigestPage } from "./DigestPage";

// Готовность материалов выпуска на экране «Дайджест» (#199, спека #197), шов 3: экран на подставном API. У материала
// видно, чего ему не хватает до готовности (суть, перевод, карточка, баллы), рядом с тем, чего не хватает выпуску;
// сверху число неготовых материалов месяца.

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" }, ...init });
}

describe("готовность материалов выпуска (#199)", () => {
  const fetchMock = vi.fn();
  const base: DigestMaterial = {
    item_type: "signal", article_id: null, signal_id: 77, material_signal_id: 77, section: "technology",
    rubric: "БУРЕНИЕ", label: "Пилот", label_default: "Пилот", title: "Пилот буровой", summary: "Суть пилота.",
    hypothesis: null, hypothesis_set_by: null, hypothesis_author: null, hypothesis_set_at: null,
    relevance: "прямая", url: "https://radar.example.org/77", image_own: false, picture: "own",
    missing: ["criteria"], readiness: ["card", "scores"],
  };
  const news: DigestContentItem[] = [{
    item_type: "signal", article_id: null, signal_id: 77, category: "Бурение", title: "Пилот буровой",
    summary: "Суть пилота.", url: "https://radar.example.org/77", source: "radar.example.org", score: 75,
  }];
  let shown: DigestMaterial = base;
  let summary: { materials: number; not_ready: number } | null = { materials: 3, not_ready: 1 };

  beforeEach(() => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/feed-window") {
        return Promise.resolve(jsonResponse({ months: ["2026-10"], month: null, read_only: false, rollover_day: 11,
          archive: [] }));
      }
      if (url.startsWith("/api/articles")) return Promise.resolve(jsonResponse([]));
      if (url.startsWith("/api/digest-content")) {
        return Promise.resolve(jsonResponse({
          month: "2026-10", title: "Нефтесервисный дайджест", news, readiness: summary,
          sections: [{ key: "technology", number: "02", title: "Технологические сигналы", materials: [shown] }],
        }));
      }
      if (url.startsWith("/api/monthly-digests/")) {
        return Promise.resolve(jsonResponse({ detail: "Digest not found" }, { status: 404 }));
      }
      return Promise.resolve(jsonResponse({ ok: true }));
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
    shown = base;
    summary = { materials: 3, not_ready: 1 };
  });

  it("у материала видно, чего не хватает до готовности, рядом с недостающим выпуску; сверху число неготовых", async () => {
    render(<DigestPage onUnauthorized={() => undefined} showToast={() => undefined} />);
    const row = (await screen.findByText("Пилот буровой")).closest("li") as HTMLElement;

    expect(within(row).getByRole("note", { name: "Готовность к выпуску" }))
      .toHaveTextContent("Не готов к выпуску: нет карточки, нет баллов");
    expect(within(row).getByRole("note", { name: "Чего не хватает выпуску" }))
      .toHaveTextContent("Не хватает: нет баллов по критериям");
    expect(screen.getByText("Не готовы к выпуску: 1 из 3")).toBeInTheDocument();
  });

  it("у готового материала строки готовности нет, а когда готово всё, нет и числа неготовых", async () => {
    shown = { ...base, readiness: [] };
    summary = { materials: 3, not_ready: 0 };
    render(<DigestPage onUnauthorized={() => undefined} showToast={() => undefined} />);
    const row = (await screen.findByText("Пилот буровой")).closest("li") as HTMLElement;

    expect(within(row).queryByRole("note", { name: "Готовность к выпуску" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Не готовы к выпуску/)).not.toBeInTheDocument();
  });
});
