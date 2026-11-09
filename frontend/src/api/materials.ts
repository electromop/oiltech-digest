import { apiFetch } from "./client";
import type { ColleagueMaterial, ColleagueMaterialsResult } from "./types";

export function listColleagueMaterials(limit = 200) {
  return apiFetch<ColleagueMaterialsResult>(`/api/materials?limit=${limit}`);
}

export function colleagueMaterialFileUrl(materialId: number) {
  return `/api/materials/${materialId}/file`;
}

// Правка материала из бота на платформе (решение владельца 23.10): только админ, проверки полей: как у бота.
export type ColleagueMaterialPatch = {
  title?: string;
  description?: string;
  url?: string;
  companies?: string[];
  verdict?: string;
  verdict_comment?: string;
};

export function updateColleagueMaterial(materialId: number, patch: ColleagueMaterialPatch) {
  return apiFetch<{ ok: boolean; material: ColleagueMaterial }>(`/api/materials/${materialId}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteColleagueMaterial(materialId: number) {
  return apiFetch<{ ok: boolean }>(`/api/materials/${materialId}`, { method: "DELETE" });
}
