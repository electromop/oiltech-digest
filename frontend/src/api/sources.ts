import { apiFetch } from "./client";
import type {
  BackgroundJob,
  CreateSourcePayload,
  ManualArticleImportPayload,
  ManualArticleImportResult,
  QueryMemoryRow,
  ScrapeResponse,
  Source,
  SourceDiagnostics,
  SourceHealth,
  SourcePatch,
} from "./types";

export function listSources() {
  return apiFetch<Source[]>("/api/sources?limit=500");
}

export function listSourceHealth() {
  return apiFetch<SourceHealth[]>("/api/source-health?limit=500");
}

export function createSource(payload: CreateSourcePayload) {
  return apiFetch<{ ok: boolean; id: number }>("/api/sources", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateSource(sourceId: number, payload: SourcePatch) {
  return apiFetch<{ ok: boolean }>(`/api/sources/${sourceId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function diagnoseSource(sourceId: number, payload: SourcePatch) {
  return apiFetch<SourceDiagnostics>(`/api/sources/${sourceId}/diagnose?limit=5`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function diagnoseSourceJob(sourceId: number, payload: SourcePatch) {
  return apiFetch<{ ok: boolean; job: BackgroundJob }>(`/api/sources/${sourceId}/diagnose?limit=5&background=true`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function scrapeSource(sourceId: number) {
  return apiFetch<ScrapeResponse>(`/api/sources/${sourceId}/scrape`, {
    method: "POST",
  });
}

export function scrapeSourceJob(sourceId: number) {
  return apiFetch<{ ok: boolean; job: BackgroundJob }>(`/api/sources/${sourceId}/scrape?background=true`, {
    method: "POST",
  });
}

export function importArticleByUrl(payload: ManualArticleImportPayload) {
  return apiFetch<ManualArticleImportResult>("/api/articles/import", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// Архив источника: не опрашивается И его статьи уходят из ленты. Жёсткого удаления нет
// намеренно — articles.source_id ссылается на sources БЕЗ ON DELETE, Postgres откажет.
export function archiveSource(sourceId: number) {
  return apiFetch<{ ok: boolean; id: number; name: string; archived: boolean }>(
    `/api/sources/${sourceId}/archive`,
    { method: "POST" },
  );
}

export function unarchiveSource(sourceId: number) {
  return apiFetch<{ ok: boolean; id: number; name: string; archived: boolean }>(
    `/api/sources/${sourceId}/unarchive`,
    { method: "POST" },
  );
}
