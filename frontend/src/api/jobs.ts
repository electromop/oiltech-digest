import { apiDownload, apiFetch } from "./client";
import type { BackgroundJob } from "./types";

export type JobsFilter = {
  status?: BackgroundJob["status"] | "";
  kind?: string;
  queue?: string;
  limit?: number;
};

export function listJobs(filter: JobsFilter = {}) {
  const params = new URLSearchParams();
  if (filter.status) params.set("status", filter.status);
  if (filter.kind) params.set("kind", filter.kind);
  if (filter.queue) params.set("queue_name", filter.queue);
  params.set("limit", String(filter.limit ?? 50));
  return apiFetch<BackgroundJob[]>(`/api/jobs?${params.toString()}`);
}

export function getJob(jobId: number) {
  return apiFetch<BackgroundJob>(`/api/jobs/${jobId}`);
}

export function downloadJobResult(jobId: number) {
  return apiDownload(`/api/jobs/${jobId}/download`);
}
