import { apiFetch } from "./client";
import type { FeedWindowPayload, MonthlyAnalytics, MonthlyStats } from "./types";

// Открытые месяцы окна и прошлые месяцы для переключателя «Архив».
export function getFeedWindow() {
  return apiFetch<FeedWindowPayload>("/api/feed-window");
}

export function getMonthlyStats(months = 6) {
  return apiFetch<MonthlyStats>(`/api/stats/monthly?months=${months}`);
}

export function getMonthlyAnalytics(months = 6) {
  return apiFetch<MonthlyAnalytics>(`/api/analytics/monthly?months=${months}`);
}
