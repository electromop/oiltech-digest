import { apiFetch } from "./client";

// Журнал ОС (#189, спека #187): вкладка «Обратная связь». Отметки обеих витрин, предложения разбора ОС и задачи
// выборки одним журналом; решения, отмена и настройки только у администратора (сервер отвечает остальным 403).

export type JournalStatus = "done" | "pending" | "approved" | "modified" | "dismissed" | "applied" | "undone" | "stale";
export type JournalKind = "mark" | "signal_proposal" | "sample_task";

export type JournalStep = { label: string; at: string | null; author: string | null };

export type JournalChange = {
  field: string;
  label: string;
  value: unknown;
  shown: string;
  previous: string | null;
  quote: string | null;
  event_ids: number[];
};

export type JournalBasis = {
  id: number;
  author: string | null;
  role: string | null;
  at: string | null;
  verdict: string | null;
  reason: string | null;
  comment: string | null;
  corrected_title: string | null;
  corrected_thesis: string | null;
  duplicate_of_signal_id: number | null;
};

export type JournalPublication = {
  signal_id: number;
  title: string | null;
  url: string | null;
  theme: string | null;
  reason: string | null;
  origin: string | null;
};

export type JournalEntry = {
  key: string;
  kind: JournalKind;
  id: number;
  at: string | null;
  author: string | null;
  role: string | null;
  signal_id: number | null;
  signal_title: string | null;
  showcase: string | null;
  agents: string[];
  status: JournalStatus;
  path: JournalStep[];
  // Отметка.
  verdict?: string | null;
  reason?: string | null;
  comment?: string | null;
  corrected_title?: string | null;
  corrected_thesis?: string | null;
  duplicate_of_signal_id?: number | null;
  applied?: boolean;
  system?: string[];
  awaits_slice?: boolean;
  action_id?: number | null;
  can_undo?: boolean;
  proposals?: Array<{ id: number; status: string }>;
  // Предложение разбора ОС.
  quote?: string | null;
  event_ids?: number[];
  changes?: JournalChange[];
  basis?: JournalBasis[];
  can_decide?: boolean;
  // Задача выборки.
  publications?: JournalPublication[];
  month?: string | null;
  can_close?: boolean;
};

export type JournalFilters = {
  status?: string;
  showcase?: string;
  agent?: string;
  author?: string;
  signal_id?: number;
  kind?: string;
  limit?: number;
};

export type JournalPage = { entries: JournalEntry[]; authors: string[] };

export type JournalSlice = {
  id: number;
  trigger: string;
  status: string;
  window_from: string;
  window_to: string;
  signals: number;
  proposals: number;
  note: string | null;
  author: string | null;
  created_at: string;
  finished_at: string | null;
};

export type JournalOverview = {
  slices: JournalSlice[];
  running: boolean;
  next_slice_at: string;
  month_spend_usd: number;
  ceiling_usd: number;
  warning: string | null;
  can_collect: boolean;
  can_configure: boolean;
};

export type ContourSettingField = {
  key: string;
  label: string;
  kind: "int" | "number" | "bool" | "time" | "choice" | "model";
  default: unknown;
  low: number | null;
  high: number | null;
  later: boolean;
  choices: Array<{ value: string; label: string }>;
};

export type ContourSettings = { values: Record<string, unknown>; fields: ContourSettingField[]; can_edit: boolean };

export function listJournal(filters: JournalFilters = {}) {
  const params = new URLSearchParams();
  for (const key of ["status", "showcase", "agent", "author", "kind"] as const) {
    const value = filters[key];
    if (value) params.set(key, value);
  }
  if (filters.signal_id) params.set("signal_id", String(filters.signal_id));
  params.set("limit", String(filters.limit ?? 100));
  return apiFetch<JournalPage>(`/api/feedback-journal?${params.toString()}`);
}

export function journalOverview() {
  return apiFetch<JournalOverview>("/api/feedback-journal/overview");
}

export function collectNow() {
  return apiFetch<{ slice_id: number | null; status: string; signals?: number; note?: string }>(
    "/api/feedback-journal/slices", { method: "POST" });
}

export type ProposalDecision = "approve" | "modify" | "dismiss";

export function decideProposal(proposalId: number, decision: ProposalDecision,
                               changes: Array<{ field: string; value: string }> = []) {
  return apiFetch<{ status: JournalStatus; refused?: Array<{ label: string; reason: string }> }>(
    `/api/feedback-journal/proposals/${proposalId}/decision`,
    { method: "POST", body: JSON.stringify({ decision, changes }) },
  );
}

export function undoAction(actionId: number) {
  return apiFetch<{ status: JournalStatus; failed: string | null }>(
    `/api/feedback-journal/actions/${actionId}/undo`, { method: "POST" });
}

export function closeTask(actionId: number) {
  return apiFetch<{ status: JournalStatus }>(`/api/feedback-journal/tasks/${actionId}/close`, { method: "POST" });
}

export function getContourSettings() {
  return apiFetch<ContourSettings>("/api/feedback-journal/settings");
}

export function saveContourSettings(values: Record<string, unknown>) {
  return apiFetch<ContourSettings>("/api/feedback-journal/settings", { method: "PUT", body: JSON.stringify({ values }) });
}
