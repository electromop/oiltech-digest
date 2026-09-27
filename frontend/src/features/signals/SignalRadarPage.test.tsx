import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getSignalSearchHealth, type SignalSearchHealth } from "../../api/signals";
import type { Signal } from "../../api/types";
import { SignalRadarPage } from "./SignalRadarPage";

const baseSignal: Signal = {
  id: 107,
  signal_key: "k107",
  title: "MethaneSAT lost contact",
  title_ru: "Потеря связи со спутником MethaneSAT",
  theme: "Экология",
  summary: "Спутник мониторинга метана перестал выходить на связь.",
  thesis: null,
  transferability: "Спутниковый мониторинг утечек",
  maturity: "watch",
  confidence: 0.7,
  score: 72,
  why_now: "Сбой единственного открытого спутника",
  why_not_noise: null,
  companies_json: ["EDF / MethaneSAT"],
  industries_json: [],
  evidence_count: 2,
  first_seen_at: null,
  last_seen_at: null,
  created_at: null,
  updated_at: null,
  evidence: [],
};

vi.mock("../../api/signals", () => ({
  listSignals: vi.fn(async () => [
    { ...baseSignal, interest_score: 91.4, why_interesting: "Единственный открытый источник данных по метану" },
    { ...baseSignal, id: 3, signal_key: "k3", title_ru: "Карточка без ревью пачки" },
  ]),
  updateSignal: vi.fn(),
  createSignalFeedback: vi.fn(),
  getSignalSearchHealth: vi.fn(),
}));

// Ежедневный прогон 27.09: крон 07:15 МСК = 04:15 UTC, Brave 402 во всех темах.
const failedEverywhere: SignalSearchHealth = {
  run_id: 31,
  job_id: 5120,
  run_at: "2026-09-27T04:15:02+00:00",
  status: "failed",
  signals: 0,
  topics: 13,
  failed: 13,
  first_error: "HTTP 402 Usage limit exceeded",
  http_status: 402,
  provider: "brave",
};

function renderRadar(isAdmin: boolean) {
  render(<SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} isAdmin={isAdmin} />);
}

describe("SignalRadarPage", () => {
  beforeEach(() => {
    vi.mocked(getSignalSearchHealth).mockReset();
  });

  it("показывает «почему интересно» только там, где ревью пачки его дало", async () => {
    render(<SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} />);

    expect(await screen.findByText("Единственный открытый источник данных по метану")).toBeInTheDocument();
    expect(screen.getAllByText(/Почему интересно/)).toHaveLength(1);
    expect(screen.getByText(/Почему интересно · 91/)).toBeInTheDocument();
  });

  it("админ видит плашку: поиск не ответил во всех темах", async () => {
    vi.mocked(getSignalSearchHealth).mockResolvedValue({ search_health: failedEverywhere });

    renderRadar(true);

    expect(
      await screen.findByText(
        "Прогон 27.09 в 07:15: поиск не ответил в 13 из 13 тем (HTTP 402 — исчерпан месячный лимит поиска Brave). Новых сигналов нет.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveClass("archiveNotice");
  });

  it("админ видит плашку и при частичном сбое", async () => {
    vi.mocked(getSignalSearchHealth).mockResolvedValue({
      search_health: { ...failedEverywhere, status: "ok", failed: 3, signals: 4 },
    });

    renderRadar(true);

    expect(
      await screen.findByText(
        "Прогон 27.09 в 07:15: поиск не ответил в 3 из 13 тем (HTTP 402 — исчерпан месячный лимит поиска Brave).",
      ),
    ).toBeInTheDocument();
  });

  it("обычный пользователь плашку не видит и здоровье поиска не запрашивает", async () => {
    vi.mocked(getSignalSearchHealth).mockResolvedValue({ search_health: failedEverywhere });

    renderRadar(false);

    expect(await screen.findByText("Карточка без ревью пачки")).toBeInTheDocument();
    expect(getSignalSearchHealth).not.toHaveBeenCalled();
    expect(screen.queryByText(/поиск не ответил/)).not.toBeInTheDocument();
  });

  it("после успешного прогона плашки нет", async () => {
    vi.mocked(getSignalSearchHealth).mockResolvedValue({
      search_health: { ...failedEverywhere, status: "ok", failed: 0, signals: 9, first_error: null, http_status: null },
    });

    renderRadar(true);

    expect(await screen.findByText("Карточка без ревью пачки")).toBeInTheDocument();
    expect(getSignalSearchHealth).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/поиск не ответил/)).not.toBeInTheDocument();
  });
});
