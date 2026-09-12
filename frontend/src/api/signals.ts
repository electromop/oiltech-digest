import { apiFetch } from "./client";
import type { Signal, SignalFeedbackPayload, SignalPatch } from "./types";

export type SignalQuery = {
  maturity?: string;
  theme?: string;
  limit?: number;
  evidenceLimit?: number;
};

export function listSignals(query: SignalQuery = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 100));
  params.set("evidence_limit", String(query.evidenceLimit ?? 5));
  if (query.maturity) params.set("maturity", query.maturity);
  if (query.theme) params.set("theme", query.theme);
  return apiFetch<Signal[]>(`/api/signals?${params.toString()}`);
}

export function updateSignal(signalId: number, payload: SignalPatch) {
  return apiFetch<{ ok: boolean }>(`/api/signals/${signalId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function createSignalFeedback(payload: SignalFeedbackPayload) {
  return apiFetch<{ ok: boolean; event_id: number; memory_ids: number[]; memories: number }>("/api/signals/feedback", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
