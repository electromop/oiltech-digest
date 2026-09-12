import { apiFetch } from "./client";
import type { FeedbackEntry, FeedbackReason, FeedbackSourceSummary } from "./types";

export function listFeedbackReasons() {
  return apiFetch<FeedbackReason[]>("/api/feedback/reasons");
}

export function getFeedback(target: { articleId?: number; sourceId?: number }) {
  const params = new URLSearchParams();
  if (target.articleId) params.set("article_id", String(target.articleId));
  if (target.sourceId) params.set("source_id", String(target.sourceId));
  return apiFetch<{ ok: boolean; entry: FeedbackEntry | null }>(`/api/feedback?${params.toString()}`);
}

export function saveFeedback(payload: {
  article_id?: number;
  source_id?: number;
  reason?: string | null;
  usefulness?: number | null;
  translation?: number | null;
  source_quality?: number | null;
  comment?: string | null;
}) {
  return apiFetch<{ ok: boolean; entry: FeedbackEntry }>("/api/feedback", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listFeedbackBySource(limit = 300) {
  return apiFetch<FeedbackSourceSummary[]>(`/api/feedback/sources?limit=${limit}`);
}
