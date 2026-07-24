import { apiFetch } from "./client";
import type { DashboardStats, MonthlyStats } from "./types";

export function getDashboardStats() {
  return apiFetch<DashboardStats>("/api/stats");
}

export function getMonthlyStats(months = 6) {
  return apiFetch<MonthlyStats>(`/api/stats/monthly?months=${months}`);
}
