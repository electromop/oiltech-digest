import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AnalyticsOverview } from "./AnalyticsOverview";
import { analyticsFixture } from "./fixture.test-data";

describe("обзор статистики", () => {
  it("незаконченный сентябрь: цифры месяца и честная база «те же дни августа»", () => {
    render(<AnalyticsOverview data={analyticsFixture} months={analyticsFixture.months} month="2026-09" />);
    expect(screen.getByText(/Сентябрь 2026 · по 19 сентября/)).toBeInTheDocument();
    // 756 сильных против 278 за 1–19 августа — рост кратный.
    expect(screen.getAllByText(/×2,7/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/с 1–19 августа/).length).toBeGreaterThan(0);
    expect(screen.getByText("Отобрано в дайджест")).toBeInTheDocument();
    // Статьи со старой таксономией не прячутся молча — экран их называет.
    expect(screen.getByText(/размечены прежними направлениями/)).toBeInTheDocument();
  });

  it("стоимость — только если сервер её отдал (администратор)", () => {
    const { ai_cost: _cost, ai_cost_previous_same_period: _prev, usd_rub: _rate, ...userView } = analyticsFixture;
    render(<AnalyticsOverview data={userView} months={userView.months} month="2026-08" />);
    expect(screen.queryByText("ИИ-обработка")).not.toBeInTheDocument();
    expect(screen.getAllByText(/с июлем 2026/).length).toBeGreaterThan(0);
  });
});
