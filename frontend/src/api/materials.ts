import { apiFetch } from "./client";
import type { ColleagueMaterialsResult } from "./types";

export function listColleagueMaterials(limit = 200) {
  return apiFetch<ColleagueMaterialsResult>(`/api/materials?limit=${limit}`);
}

export function colleagueMaterialFileUrl(materialId: number) {
  return `/api/materials/${materialId}/file`;
}
