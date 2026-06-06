import { apiFetch } from "./client";
import type { DashboardStats } from "./types";

export function getDashboardStats() {
  return apiFetch<DashboardStats>("/api/stats");
}
