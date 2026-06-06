import { apiFetch } from "./client";
import type { ScoringCriterion } from "./types";

export function listScoringCriteria() {
  return apiFetch<ScoringCriterion[]>("/api/scoring-criteria");
}

export function saveScoringCriteria(items: ScoringCriterion[]) {
  return apiFetch<{ ok: boolean; saved: number; weight_sum: number }>("/api/scoring-criteria", {
    method: "PUT",
    body: JSON.stringify(items),
  });
}

export function deleteScoringCriterion(criterionId: number) {
  return apiFetch<{ ok: boolean }>(`/api/scoring-criteria/${criterionId}`, {
    method: "DELETE",
  });
}
