import { apiFetch } from "./client";
import type { BacklogPayload, BacklogTask, BacklogTaskStatus } from "./types";

export function getBacklog() {
  return apiFetch<BacklogPayload>("/api/backlog");
}

export function createBacklogTask(payload: { title: string; priority: string; status?: BacklogTaskStatus }) {
  return apiFetch<BacklogTask>("/api/backlog/tasks", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateBacklogTaskStatus(taskId: string, status: BacklogTaskStatus) {
  return apiFetch<BacklogTask>(`/api/backlog/tasks/${encodeURIComponent(taskId)}`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });
}
