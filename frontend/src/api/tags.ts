import { apiFetch } from "./client";
import type { Tag } from "./types";

export function listTags() {
  return apiFetch<Tag[]>("/api/tags");
}

export function saveTags(items: Tag[]) {
  return apiFetch<{ ok: boolean; saved: number }>("/api/tags", {
    method: "PUT",
    body: JSON.stringify(items),
  });
}

export function deleteTag(tagId: number) {
  return apiFetch<{ ok: boolean }>(`/api/tags/${tagId}`, {
    method: "DELETE",
  });
}
