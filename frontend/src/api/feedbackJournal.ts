import { apiFetch } from "./client";

// Журнал ОС (#189, спека #187): вкладка «Обратная связь». Отметки обеих витрин, предложения разбора ОС и задачи
// выборки одним журналом; решения, отмена и настройки только у администратора (сервер отвечает остальным 403).

export type JournalStatus = "done" | "pending" | "approved" | "modified" | "dismissed" | "applied" | "undone" | "stale";
// immediate: явное действие администратора без отметки (кнопка «Вернуть» в «Отклонённых»). rule_proposal: предложение
// правила эксперта агенту, set_check: ежемесячная проверка всего набора правил агента (#190).
export type JournalKind = "mark" | "signal_proposal" | "sample_task" | "immediate" | "rule_proposal" | "set_check";

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

// Правило эксперта (#190): агент и шаг, при нужде категория, у поиска тема; текст правила.
export type JournalRule = {
  agent: string;
  agent_label: string;
  step: string;
  step_label: string;
  category: string | null;
  category_label: string | null;
  topic: string | null;
  text: string;
};

// Строка автопроверки, которая сдвинулась (или «было / стало» у «Сути и перевода», найдено ли у поиска).
export type JournalCheckSample = {
  signal_id: number;
  label: string | null;
  field?: string | null;
  kind?: string | null;
  expected?: unknown;
  expert?: string | null;
  before?: unknown;
  after?: unknown;
  direction?: "toward" | "away" | null;
  with?: boolean;
  without?: boolean;
};

// Итог автопроверки правила на эталоне ОС: к эталону, от эталона, без изменений, совет; «мало данных» при малом числе строк.
export type JournalCheck = {
  id: number;
  status: "queued" | "done" | "postponed" | "failed";
  mode: "rule" | "set";
  rows: number;
  toward: number;
  away: number;
  unchanged: number;
  advice: string | null;
  note: string | null;
  little_data: boolean;
  no_count: boolean;
  baseline: { accepted: number; computed: number } | null;
  samples: JournalCheckSample[];
  search: Record<string, { found: number; error: string | null }> | null;
  finished_at: string | null;
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
  // «Изменить»: что предлагал разбор ОС, когда на карточку легла формулировка администратора (changes).
  proposed_changes?: JournalChange[] | null;
  basis?: JournalBasis[];
  can_decide?: boolean;
  // Задача выборки.
  publications?: JournalPublication[];
  month?: string | null;
  can_close?: boolean;
  // Предложение правила эксперта (#190) и проверка набора правил.
  rule?: JournalRule | null;
  proposed_rule?: JournalRule | null;
  rule_kind?: "new" | "refine" | "merge";
  replaced?: Array<JournalRule & { id: number }>;
  check?: JournalCheck | null;
  // Почему согласование требует отдельного подтверждения (мало данных, перевес «от эталона», проверки нет).
  confirm?: string[];
  version?: number | null;
  can_rollback?: boolean;
  can_recheck?: boolean;
  seed?: string | null;
  set_agent?: string;
  set_agent_label?: string;
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

// Решение по предложению. У предложения правила (#190): text: новая формулировка правила, confirm: отдельное
// подтверждение согласования без готовой проверки, с «мало данных» или с перевесом «от эталона».
export type RuleDecisionExtra = { text?: string; confirm?: boolean };

export function decideProposal(proposalId: number, decision: ProposalDecision,
                               changes: Array<{ field: string; value: string }> = [], extra: RuleDecisionExtra = {}) {
  return apiFetch<{ status: JournalStatus; version?: number; refused?: Array<{ label: string; reason: string }> }>(
    `/api/feedback-journal/proposals/${proposalId}/decision`,
    { method: "POST", body: JSON.stringify({ decision, changes, ...extra }) },
  );
}

export function rollbackRule(proposalId: number) {
  return apiFetch<{ status: JournalStatus; from_version: number; to_version: number }>(
    `/api/feedback-journal/proposals/${proposalId}/rollback`, { method: "POST" });
}

export function recheckRule(proposalId: number) {
  return apiFetch<{ check_id: number; status: string }>(`/api/feedback-journal/proposals/${proposalId}/check`,
    { method: "POST" });
}

// Правила эксперта по агентам (#190): действующая версия набора, её правила и прежние версии.
export type ExpertRuleView = JournalRule & { id: number; approved_by: string | null; approved_at: string };
export type ExpertRuleSetVersion = {
  version: number;
  rules: number;
  current: boolean;
  previous_version: number;
  author: string | null;
  at: string;
  action_id: number | null;
};
export type ExpertRulesAgent = {
  agent: string;
  label: string;
  steps: Array<{ step: string; label: string }>;
  version: number;
  action_id: number | null;
  rules: ExpertRuleView[];
  history: ExpertRuleSetVersion[];
};
export type ExpertRulesOverview = { agents: ExpertRulesAgent[]; limit: number; can_rollback: boolean };

export function listExpertRules() {
  return apiFetch<ExpertRulesOverview>("/api/feedback-journal/rules");
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
