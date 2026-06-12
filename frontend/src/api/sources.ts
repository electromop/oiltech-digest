import { apiFetch } from "./client";
import type {
  BackgroundJob,
  CreateSourcePayload,
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
