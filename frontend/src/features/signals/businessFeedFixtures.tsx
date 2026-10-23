// Бизнес-лента (#97, спека #93): заготовки тестов экрана в витрине «бизнес» — справочники, сигналы ленты,
// сервер в миниатюре и помощники. Тесты — BusinessFeed.test.tsx и BusinessFeedFilters.test.tsx (шов 3).
import { render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import {
  getSignalDictionaries,
  getSignalSearchHealth,
  getSignalSummary,
  getSignalWeekly,
  listSignals,
  type SignalDictionaries,
  type SignalQuery,
} from "../../api/signals";
import type { Signal, SignalEvidence } from "../../api/types";
import { SignalRadarPage } from "./SignalRadarPage";

// Справочники — как их отдаёт сервер (/api/signals/dictionaries), сокращённо.
export const DICTIONARIES: SignalDictionaries = {
  event_type: ["пилот", "контракт", "сделка (M&A)", "нет события"],
  region: ["Россия", "Европа", "Ближний Восток"],
  technology_class: ["робототехника", "датчики и IoT", "неприменимо"],
  technology_maturity: ["прототип", "пилот/ОПИ", "неприменимо"],
  companies: ["SLB", "Saudi Aramco", "Роснефть"],
};

export const AGENT_FIELDS = ["event_date", "participants", "event_type", "region", "technology_class", "technology_maturity",
  "oilfield_relevance", "oilfield_application", "why_interesting"];

export function publication(id: number, fields: Partial<SignalEvidence>): SignalEvidence {
  return {
    id, signal_id: 9001, article_id: id, source_url: `https://feed.example/${id}`, title: `Publication ${id}`,
    title_ru: null, publisher: "Нефтегаз", published_at: "2026-09-25T08:00:00Z", evidence_type: "feed_article",
    extracted_fact: null, summary_ru: null, strength: 1, raw_payload_json: {}, created_at: null, updated_at: null,
    ...fields,
  };
}

// Сигнал ленты с карточкой агента: статья и её перепечатка — публикации одного события.
export const contract: Signal = {
  id: 9001,
  signal_key: "feed:1",
  origin: "feed",
  signal_category: "business",
  title: "SLB wins Aramco drilling contract",
  title_ru: "SLB получила контракт Saudi Aramco на бурение",
  theme: "Бурение",
  summary: "Контракт на бурение 40 скважин на шельфе.",
  thesis: null,
  transferability: null,
  maturity: "watch",
  confidence: 0,
  score: 74,
  score_label: "Выше средней",
  score_profile: "business",
  score_items_json: [{ criterion_id: 1, final_score: 80 }],
  criteria_snapshot: [{ id: 1, name: "Стратегическая значимость для нефтесервиса", weight: 30 }],
  why_now: null,
  why_not_noise: null,
  companies_json: [],
  industries_json: [],
  evidence_count: 2,
  first_seen_at: "2026-09-25T09:00:00Z",
  last_seen_at: null,
  created_at: null,
  updated_at: null,
  event_date: "2026-09-24",
  event_date_source: "из текста",
  event_type: "контракт",
  region: "Ближний Восток",
  technology_class: "неприменимо",
  technology_maturity: "неприменимо",
  oilfield_relevance: "direct",
  oilfield_application: "Бурение скважин на шельфе",
  why_interesting: "Крупнейший контракт на буровые услуги в регионе",
  participants_json: [
    { name: "SLB", quote: "SLB подписала контракт", verified: true },
    { name: "Saudi Aramco", quote: "Saudi Aramco заключила", verified: true },
  ],
  card_set_by: Object.fromEntries(AGENT_FIELDS.map((field) => [field, "agent"])),
  card_recipe: { agent: "signal_card", prompt_version: "2026-10-16.1" },
  evidence: [
    publication(1, { title_ru: "SLB получила контракт Aramco", publisher: "Нефтегаз" }),
    publication(2, { title_ru: "Aramco выбрала SLB", publisher: "Offshore Weekly", strength: 0.5 }),
  ],
};

// Агент часть значений определить не смог: «не определено» — пробел карточки, его видно.
export const forum: Signal = {
  ...contract,
  id: 9002,
  signal_key: "feed:2",
  title_ru: "Форум нефтесервиса в Тюмени",
  event_date: null,
  event_date_source: "не определено",
  event_type: "не определено",
  region: "Россия",
  technology_class: "не определено",
  technology_maturity: "не определено",
  oilfield_relevance: null,
  oilfield_application: null,
  why_interesting: null,
  participants_json: [],
  evidence: [publication(3, {})],
};

// Статья, принятая отбором до выхода агента в работу: карточку ещё не заполняли.
export const fresh: Signal = {
  ...contract,
  id: 9003,
  signal_key: "feed:3",
  title_ru: "Новость без карточки",
  event_date: null,
  event_date_source: null,
  event_type: null,
  region: null,
  technology_class: null,
  technology_maturity: null,
  oilfield_relevance: null,
  oilfield_application: null,
  why_interesting: null,
  participants_json: null,
  card_set_by: null,
  card_recipe: null,
  evidence: [publication(4, {})],
};

export function chips(card: HTMLElement) {
  const group = within(card).getByRole("group", { name: "Атрибуты сигнала" });
  return [...group.querySelectorAll(".signalChip")].map((chip) => [chip.textContent, chip.classList.contains("gap")]);
}

export function matches(card: Signal, query: SignalQuery) {
  const names = (card.participants_json || []).map((item) => item.name);
  return (
    (!query.theme || card.theme === query.theme)
    && (!query.eventType || card.event_type === query.eventType)
    && (!query.region || card.region === query.region)
    && (!query.technologyClass || card.technology_class === query.technologyClass)
    && (!query.technologyMaturity || card.technology_maturity === query.technologyMaturity)
    && (!query.company || names.includes(query.company))
  );
}

// Сервер в миниатюре: выборка и числа — «на сервере» по всем карточкам витрины.
export function serve(cards: Signal[]) {
  vi.mocked(listSignals).mockImplementation(async (query: SignalQuery = {}) =>
    cards.filter((card) => matches(card, query)).map((card) => ({ ...card })),
  );
  vi.mocked(getSignalSummary).mockImplementation(async (query: SignalQuery = {}) => ({
    total: cards.length,
    new_7d: 1,
    in_digest: 0,
    with_feedback: 0,
    merged: 1,
    rejected: 0,
    matching: cards.filter((card) => matches(card, query)).length,
    themes: [...new Set(cards.map((card) => card.theme))].map((theme) => ({ theme, count: 1 })),
  }));
}

export function lastListQuery(): SignalQuery {
  return vi.mocked(listSignals).mock.lastCall?.[0] ?? {};
}

export async function cardOf(title: string) {
  return (await screen.findByText(title)).closest("article") as HTMLElement;
}

export function renderFeed(isAdmin = false) {
  render(<SignalRadarPage onUnauthorized={() => undefined} showToast={() => undefined} isAdmin={isAdmin} showcase="business" />);
}

// Перед каждым тестом: подставной API чистый, справочники пришли, итогов недели и здоровья поиска нет.
export function resetBusinessFeedApi() {
  vi.mocked(listSignals).mockReset();
  vi.mocked(getSignalSummary).mockReset();
  vi.mocked(getSignalWeekly).mockReset();
  vi.mocked(getSignalSearchHealth).mockReset();
  vi.mocked(getSignalDictionaries).mockReset();
  vi.mocked(getSignalDictionaries).mockResolvedValue(DICTIONARIES);
  vi.mocked(getSignalWeekly).mockRejectedValue(new Error("нет итогов"));
  vi.mocked(getSignalSearchHealth).mockResolvedValue({ search_health: null });
  serve([contract]);
}
