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
    // Рубли — по курсу ЦБ своего месяца: $30 (синтетика) × 84,1975 ₽/$ на 19.09.
    expect(screen.getAllByText(/^2\s526 ₽$/).length).toBeGreaterThan(0);
    expect(screen.getByText(/84,20 ₽\/\$ на 19\.09\.2026/)).toBeInTheDocument();
    // Плитку «Источников дали релевантное» владелец убрал 19.09.
    expect(screen.queryByText("Источников дали релевантное")).not.toBeInTheDocument();
  });

  it("стоимость — только если сервер её отдал (администратор)", () => {
    const { ai_cost: _cost, ai_cost_previous_same_period: _prev, ...userView } = analyticsFixture;
    render(<AnalyticsOverview data={userView} months={userView.months} month="2026-08" />);
    expect(screen.queryByText("ИИ-обработка")).not.toBeInTheDocument();
    expect(screen.getAllByText(/с июлем 2026/).length).toBeGreaterThan(0);
  });
});

describe("расход технологического радара", () => {
  // Сентябрь: $30 всего, из них $10 — радар (синтетика); статей ленты 4 000, курс 84,1975 ₽/$.
  const withRadar = {
    ...analyticsFixture,
    ai_cost: analyticsFixture.ai_cost?.map((row) => (row.month === "2026-09" ? { ...row, radar_cost_usd: 10 } : row)),
  };

  it("столбец ИИ-обработки делится на ленту и радар", () => {
    render(<AnalyticsOverview data={withRadar} months={withRadar.months} month="2026-09" />);
    expect(screen.getAllByText("Лента").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Технологический радар").length).toBeGreaterThan(0);
  });

  it("«₽ за статью» — без радара: у него нет статей", () => {
    render(<AnalyticsOverview data={withRadar} months={withRadar.months} month="2026-09" />);
    // ($30 − $10) × 84,1975 / 4 000 = 0,42 ₽, а не 0,63 ₽ с радаром.
    expect(screen.getByText(/0,42 ₽ за статью/)).toBeInTheDocument();
    expect(screen.queryByText(/0,63 ₽ за статью/)).not.toBeInTheDocument();
  });

  it("без расхода радара экран прежний: за статью — весь расход", () => {
    render(<AnalyticsOverview data={analyticsFixture} months={analyticsFixture.months} month="2026-09" />);
    expect(screen.getByText(/0,63 ₽ за статью/)).toBeInTheDocument();
  });
});
