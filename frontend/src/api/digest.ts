import { apiFetch } from "./client";
import type { BackgroundJob, DigestContent, MonthlyDigestDraft } from "./types";

type SaveDigestPayload = {
  month: string;
  limit: number;
  min_score: number;
};

export function getDigestContent(month: string, limit: number, minScore: number) {
  const params = new URLSearchParams({
    month,
    limit: String(limit),
    min_score: String(minScore),
  });
  return apiFetch<DigestContent>(`/api/digest-content?${params.toString()}`);
}

export function saveDigestDraft(payload: SaveDigestPayload) {
  return apiFetch<{ id: number; month: string; items: number; status: string }>("/api/monthly-digests", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getMonthlyDigest(month: string) {
  return apiFetch<MonthlyDigestDraft>(`/api/monthly-digests/${encodeURIComponent(month)}`);
}

export function enqueueDigestExport(month: string, limit: number, minScore: number, format: "pdf" | "doc" | "html") {
  return apiFetch<{ ok: boolean; job: BackgroundJob }>("/api/jobs/digest-export", {
    method: "POST",
    body: JSON.stringify({
    month,
    export_format: format,
      limit,
      min_score: minScore,
    }),
  });
}
