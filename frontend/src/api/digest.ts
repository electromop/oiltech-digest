import { apiFetch, apiFetchText } from "./client";
import type { BackgroundJob, DigestBranding, DigestContent, DigestDraftSaveResult, MonthlyDigestDraft } from "./types";

type SaveDigestPayload = {
  month: string;
  limit: number;
  min_score: number;
  max_score?: number;
  search?: string;
  top_tag?: string;
};

type DigestFilters = {
  month: string;
  limit: number;
  minScore: number;
  maxScore?: number;
  search?: string;
  topTag?: string;
};

function buildDigestParams({ month, limit, minScore, maxScore, search, topTag }: DigestFilters) {
  const params = new URLSearchParams({
    month,
    limit: String(limit),
    min_score: String(minScore),
  });
  if (typeof maxScore === "number") params.set("max_score", String(maxScore));
  if (search?.trim()) params.set("search", search.trim());
  if (topTag?.trim()) params.set("top_tag", topTag.trim());
  return params;
}

export function getDigestContent(filters: DigestFilters) {
  const params = buildDigestParams(filters);
  return apiFetch<DigestContent>(`/api/digest-content?${params.toString()}`);
}

export function getDigestEmailHtml(filters: DigestFilters) {
  const params = buildDigestParams(filters);
  return apiFetchText(`/api/digest-email?${params.toString()}`);
}

export function getDigestBranding() {
  return apiFetch<DigestBranding>("/api/digest-branding");
}

export function saveDigestBranding(payload: DigestBranding) {
  return apiFetch<{ ok: boolean; branding: DigestBranding }>("/api/digest-branding", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function saveDigestDraft(payload: SaveDigestPayload) {
  return apiFetch<DigestDraftSaveResult>("/api/monthly-digests", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getMonthlyDigest(month: string) {
  return apiFetch<MonthlyDigestDraft>(`/api/monthly-digests/${encodeURIComponent(month)}`);
}

export function updateMonthlyDigest(
  month: string,
  payload: { title?: string; status?: string; items: Array<{ article_id: number; section?: string | null; editor_note?: string | null }> },
) {
  return apiFetch<DigestDraftSaveResult>(`/api/monthly-digests/${encodeURIComponent(month)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function enqueueDigestExport(
  month: string,
  limit: number,
  minScore: number,
  format: "pdf" | "docx" | "html",
  options?: { maxScore?: number; search?: string; topTag?: string },
) {
  return apiFetch<{ ok: boolean; job: BackgroundJob }>("/api/jobs/digest-export", {
    method: "POST",
    body: JSON.stringify({
      month,
      export_format: format,
      limit,
      min_score: minScore,
      max_score: options?.maxScore,
      search: options?.search || "",
      top_tag: options?.topTag || "",
    }),
  });
}
