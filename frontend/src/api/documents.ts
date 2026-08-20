import { apiFetch } from "./client";
import type { DocumentDetails, DocumentListResult, DocumentUploadResult } from "./types";

export function listDocuments() {
  return apiFetch<DocumentListResult>("/api/documents");
}

export function getDocument(documentId: number) {
  return apiFetch<DocumentDetails>(`/api/documents/${documentId}`);
}

// Тело — FormData, и заголовок Content-Type здесь НЕ ставится специально: boundary
// многочастного тела проставляет браузер. Исключение для FormData живёт в apiFetch,
// поэтому загрузка идёт через общий клиент, а не в обход него.
export function uploadDocument(file: File, attested: boolean) {
  const body = new FormData();
  body.append("file", file);
  body.append("attested", attested ? "true" : "false");
  return apiFetch<DocumentUploadResult>("/api/documents", { method: "POST", body });
}

export function deleteDocument(documentId: number) {
  return apiFetch<{ ok: boolean }>(`/api/documents/${documentId}`, { method: "DELETE" });
}

export function documentOriginalUrl(documentId: number) {
  return `/api/documents/${documentId}/original`;
}
