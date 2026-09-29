import { apiFetch } from "./client";
import type { ScoringCriterion, ScoringProfile } from "./types";

function profileQuery(profile: ScoringProfile) {
  return `?profile=${encodeURIComponent(profile)}`;
}

export function listScoringCriteria(profile: ScoringProfile) {
  return apiFetch<ScoringCriterion[]>(`/api/scoring-criteria${profileQuery(profile)}`);
}

export function saveScoringCriteria(items: ScoringCriterion[], profile: ScoringProfile) {
  return apiFetch<{ ok: boolean; saved: number; weight_sum: number; profile: ScoringProfile }>(
    `/api/scoring-criteria${profileQuery(profile)}`,
    {
      method: "PUT",
      body: JSON.stringify(items),
    },
  );
}

export function deleteScoringCriterion(criterionId: number) {
  return apiFetch<{ ok: boolean }>(`/api/scoring-criteria/${criterionId}`, {
    method: "DELETE",
  });
}
