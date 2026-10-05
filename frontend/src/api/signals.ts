import { apiFetch } from "./client";
import type { Signal, SignalFeedbackPayload, SignalPatch } from "./types";

// Выборка экрана радара — на сервере, по всей базе (замечание заказчика 19.09): поиск,
// тема, зрелость, период «Поступил» (даты ГГГГ-ММ-ДД по Москве) и балл.
export type SignalFilters = {
  q?: string;
  maturity?: string;
  theme?: string;
  since?: string;
  until?: string;
  minScore?: number;
  maxScore?: number;
  // Отсеянные и скрытые (не на радаре) — всем: заказчик разбирает всё отсеянное.
  hidden?: boolean;
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
  if (filters.q) params.set("q", filters.q);
  if (filters.maturity) params.set("maturity", filters.maturity);
  if (filters.theme) params.set("theme", filters.theme);
  if (filters.since) params.set("since", filters.since);
  if (filters.until) params.set("until", filters.until);
  if (filters.minScore != null) params.set("min_score", String(filters.minScore));
  if (filters.maxScore != null) params.set("max_score", String(filters.maxScore));
  if (filters.hidden) params.set("hidden", "true");
  return params;
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
  // Отсеяно и скрыто — не на радаре.
  hidden?: number;
  matching: number;
  themes: Array<{ theme: string; count: number }>;
};

export function getSignalSummary(filters: SignalFilters = {}) {
  const params = filterParams(filters).toString();
  return apiFetch<SignalSummary>(params ? `/api/signals/summary?${params}` : "/api/signals/summary");
}

// «Итоги недели» радара (03.10): что вышло, что отсеяно и почему, что ждёт разбора, отзывы.
export type SignalWeekly = {
  days: number;
  on_radar: number;
  filtered: number;
  filtered_reasons: Array<{ reason: string; count: number }>;
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
  }>("/api/signals/feedback", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
