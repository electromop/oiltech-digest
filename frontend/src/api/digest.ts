import { apiDownload, apiFetch } from "./client";
import type { BackgroundJob, DigestContent, DigestDraftSaveResult, MonthlyDigestDraft } from "./types";

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

// Предпросмотр выпуска сразу в PDF (решение владельца 23.10): тот же PDF и та же вёрстка, что у выгрузки, для окна
// экрана; сервер отдаёт его inline и не пишет файл. `fresh` — «Обновить PDF»: собрать заново мимо памяти
// предпросмотра на сервере (картинка сайта, который не ответил при прошлой сборке, LOW-2 ревью PR #155).
export function getDigestPreviewPdf(filters: DigestFilters, options?: { fresh?: boolean }) {
  const params = buildDigestParams(filters);
  if (options?.fresh) params.set("fresh", "1");
  return apiDownload(`/api/digest-preview.pdf?${params.toString()}`);
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
