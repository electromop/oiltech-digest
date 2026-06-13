import { apiFetch } from "./client";
import type { MaintenanceCleanupResult, MaintenanceStatus, ReadinessBenchmarkReport } from "./types";

export function getMaintenanceStatus() {
  return apiFetch<MaintenanceStatus>("/api/maintenance/status");
}

export function runMaintenanceCleanup(payload: { background_job_days?: number; export_job_days?: number }) {
  return apiFetch<{ ok: boolean; result: MaintenanceCleanupResult }>("/api/maintenance/cleanup", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getReadinessBenchmark(iterations = 3) {
  return apiFetch<ReadinessBenchmarkReport>(`/api/maintenance/benchmark?iterations=${iterations}`);
}
