import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { getMonthlyAnalytics, getMonthlyStats } from "../../api/stats";
import type { MonthlyStats } from "../../api/types";
import { analyticsFixture } from "./fixture.test-data";
import { StatisticsPage } from "./StatisticsPage";

vi.mock("../../api/stats", () => ({
  getMonthlyStats: vi.fn(),
  getMonthlyAnalytics: vi.fn(),
}));

describe("страница «Статистика»: расход ИИ по моделям", () => {
  it("строки радара помечены: та же модель у ленты и у радара — разные строки", async () => {
    // Синтетика: репозиторий публичный, реальные расходы — коммерческая сторона.
    const stats = {
      months: 6,
      platform: [],
      ai_cost: [
        { month: "2026-10", model: "gpt-5-mini-2025-08-07", area: "feed", runs: 120, cost_usd: 1.5 },
        { month: "2026-10", model: "gpt-5-2025-08-07", area: "radar", runs: 40, cost_usd: 2.25 },
      ],
      activity: [],
      activity_scope: "all",
    } as unknown as MonthlyStats;
    vi.mocked(getMonthlyStats).mockResolvedValue(stats);
    vi.mocked(getMonthlyAnalytics).mockResolvedValue(analyticsFixture);

    render(<StatisticsPage onUnauthorized={() => undefined} showToast={() => undefined} />);

    expect(await screen.findByText("gpt-5-mini · $1.50 · радар · gpt-5 · $2.25")).toBeInTheDocument();
    expect(screen.getByText("$3.75")).toBeInTheDocument();
  });
});
