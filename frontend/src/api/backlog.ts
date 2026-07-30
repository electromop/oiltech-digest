import { apiFetch } from "./client";
import type { BacklogPayload, BacklogTask, BacklogTaskStatus } from "./types";

export function getBacklog() {
  return apiFetch<BacklogPayload>("/api/backlog");
}

export function createBacklogTask(payload: { title: string; priority: string; status?: BacklogTaskStatus; details?: string; due_date?: string }) {
  return apiFetch<BacklogTask>("/api/backlog/tasks", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateBacklogTask(taskId: string, payload: { status?: BacklogTaskStatus; due_date?: string | null }) {
  return apiFetch<BacklogTask>(`/api/backlog/tasks/${encodeURIComponent(taskId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function updateBacklogTaskStatus(taskId: string, status: BacklogTaskStatus) {
  return updateBacklogTask(taskId, { status });
}

export function addBacklogTaskComment(taskId: string, text: string) {
  return apiFetch<BacklogTask>(`/api/backlog/tasks/${encodeURIComponent(taskId)}/comments`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}
