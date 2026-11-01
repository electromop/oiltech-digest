import { apiFetch } from "./client";
import type { Signal, SignalFeedbackBlock, SignalFeedbackPayload, SignalPatch } from "./types";

// Витрина (#95): технологический радар или бизнес-лента — сигналы категории «бизнес».
export type SignalShowcase = "technology" | "business";

// Выборка экрана радара — на сервере, по всей базе (замечание заказчика 19.09): поиск,
// тема, решение платформы, период «Поступил» (даты ГГГГ-ММ-ДД по Москве) и балл.
export type SignalFilters = {
  // Без витрины — технологический радар.
  showcase?: SignalShowcase;
  q?: string;
  maturity?: string;
  theme?: string;
  since?: string;
  until?: string;
  minScore?: number;
  maxScore?: number;
  // Отсеянные и скрытые (не на радаре) — только админу (решение владельца 05.10).
  hidden?: boolean;
  // Раздел отклонённого «кто убрал → причина» (#130, только админу): вместо витрины — его публикации.
  section?: string;
  // Атрибуты карточки (#97): значение справочника или «не определено»; компания — под любым из её имён.
  eventType?: string;
  region?: string;
  technologyClass?: string;
  technologyMaturity?: string;
  company?: string;
  // Страна (#137): имя справочника стран; сервер ищет и по стране события, и по стране участника.
  country?: string;
};

// По баллу (как раньше), «сначала новые» — по дате «Поступил», по баллу по возрастанию.
export type SignalSort = "score_desc" | "date_desc" | "score_asc";

export type SignalQuery = SignalFilters & {
  sort?: SignalSort;
  limit?: number;
  offset?: number;
  evidenceLimit?: number;
};

function filterParams(filters: SignalFilters) {
  const params = new URLSearchParams();
  if (filters.showcase) params.set("showcase", filters.showcase);
  if (filters.q) params.set("q", filters.q);
  if (filters.maturity) params.set("maturity", filters.maturity);
  if (filters.theme) params.set("theme", filters.theme);
  if (filters.since) params.set("since", filters.since);
  if (filters.until) params.set("until", filters.until);
  if (filters.minScore != null) params.set("min_score", String(filters.minScore));
  if (filters.maxScore != null) params.set("max_score", String(filters.maxScore));
  if (filters.hidden) params.set("hidden", "true");
  if (filters.section) params.set("section", filters.section);
  if (filters.eventType) params.set("event_type", filters.eventType);
  if (filters.region) params.set("region", filters.region);
  if (filters.technologyClass) params.set("technology_class", filters.technologyClass);
  if (filters.technologyMaturity) params.set("technology_maturity", filters.technologyMaturity);
  if (filters.company) params.set("company", filters.company);
  if (filters.country) params.set("country", filters.country);
  return params;
}

// Справочники карточки сигнала (#97) — значения фильтров атрибутов по полям карточки (параметры
// /api/signals); «не определено» — тоже значение фильтра, оно не входит в справочник.
export type SignalDictionaries = {
  event_type: string[];
  region: string[];
  technology_class: string[];
  technology_maturity: string[];
  // Подсказки к полю «Компания» — канонические имена справочника алиасов.
  companies: string[];
  // Значения фильтра «Страна» (#137) — имена справочника стран у кода.
  country: string[];
  // Что можно вписать, возвращая отклонённое (#132): коды и подписи справочника «Отбора».
  otbor_return?: SignalReturnChoices;
};

export type SignalChoice = { code: string; label: string };

export type SignalReturnChoices = {
  industry: SignalChoice[];
  oilfield_relevance: SignalChoice[];
  category: SignalChoice[];
};

export function getSignalDictionaries() {
  return apiFetch<SignalDictionaries>("/api/signals/dictionaries");
}

export function listSignals(query: SignalQuery = {}) {
  const params = filterParams(query);
  params.set("limit", String(query.limit ?? 100));
  params.set("evidence_limit", String(query.evidenceLimit ?? 5));
  if (query.sort) params.set("sort", query.sort);
  if (query.offset) params.set("offset", String(query.offset));
  return apiFetch<Signal[]>(`/api/signals?${params.toString()}`);
}

// Числа над списком — по всему радару и по текущей выборке; тематики — для фильтра «Тема»
// (только тематики заказчика: ранние карточки со свободной темой сервер сюда не кладёт).
export type SignalSummary = {
  total: number;
  // Плитки — по всему радару, поиск их не меняет; «в дайджесте» — выбор этого пользователя.
  new_7d: number;
  in_digest: number;
  with_feedback: number;
  merged: number;
  // «Отклонённые» (#130): все разделы отклонённого одним числом, одно в обеих лентах; только админу.
  rejected?: number;
  matching: number;
  themes: Array<{ theme: string; count: number }>;
  // Фильтр «всё про компанию»: кого он показывает — сама компания первой, затем её дочки по списку
  // «дочка → мать» (экран «Дочерние общества»). Без фильтра по компании поля нет.
  company_group?: string[];
};

export function getSignalSummary(filters: SignalFilters = {}) {
  const params = filterParams(filters).toString();
  return apiFetch<SignalSummary>(params ? `/api/signals/summary?${params}` : "/api/signals/summary");
}

// «Итоги недели» радара (03.10): что вышло, что отсеяно и почему, что ждёт разбора, отзывы.
export type SignalWeekly = {
  days: number;
  on_radar: number;
  // Отсеянное и скрытое — служебное: сервер отдаёт только админу (решение владельца 05.10).
  filtered?: number;
  filtered_reasons?: Array<{ reason: string; count: number }>;
  top: Array<{ id: number; title: string; theme: string; score: number; event_date: string | null }>;
  awaiting_review: number;
  feedback: number;
  feedback_verdicts: Array<{ verdict: string; count: number }>;
};

export function getSignalWeekly(days = 7) {
  return apiFetch<SignalWeekly>(`/api/signals/weekly?days=${days}`);
}

export function updateSignal(signalId: number, payload: SignalPatch) {
  return apiFetch<{ ok: boolean }>(`/api/signals/${signalId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

// Здоровье поиска последнего ежедневного прогона — отдаётся только админу (/api/signals/search-health).
export type SignalSearchHealth = {
  run_id: number;
  job_id: number | null;
  run_at: string | null;
  status: string;
  signals: number | null;
  topics: number;
  failed: number;
  first_error: string | null;
  http_status: number | null;
  provider: string | null;
  // not_configured — поиск не вызывался (нет ключа, провайдер не подключён); network — таймаут
  // или обрыв соединения; connection — TLS или прокси; null — сбоя нет. Считает ядро (signal_discovery._search_failure_cause).
  cause: "not_configured" | "unsupported_provider" | "http" | "network" | "connection" | "other" | null;
};

// Возврат отклонённого (#132): поля «Отбора», которые вписывает человек, — коды справочника «Отбора».
export type SignalReturnPayload = {
  industry: string;
  oilfield_relevance: string;
  oilfield_application: string;
  signal_category: string;
};

export function returnSignal(signalId: number, payload: SignalReturnPayload) {
  return apiFetch<{ ok: boolean }>(`/api/signals/${signalId}/return`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getSignalSearchHealth() {
  return apiFetch<{ search_health: SignalSearchHealth | null }>("/api/signals/search-health");
}

export function createSignalFeedback(payload: SignalFeedbackPayload) {
  return apiFetch<{
    ok: boolean;
    event_id: number;
    memory_ids: number[];
    memories: number;
    // Сколько прежних вердиктов по карточке погашено и скрыта ли она как дубль («Дубль #ID»).
    superseded?: number;
    merged?: boolean;
    // «Дубль» не скрыл карточку: она выбрана в дайджест или главной с таким номером нет.
    merge_skipped?: boolean;
    // Сохранённый блок ОС (#130) — экран добавляет его в список на карточке, не перечитывая витрину.
    block?: SignalFeedbackBlock;
    // «Не тот блок» (#132): новая категория сигнала — он уже в другой витрине, баллы пересчитываются.
    recategorized?: string;
  }>("/api/signals/feedback", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
