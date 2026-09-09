import type { UploadedDocument } from "../../api/types";

export const DOCUMENT_STATUS_LABELS: Record<string, string> = {
  uploaded: "Загружен",
  parsed: "Разобран",
  processing: "В обработке",
  ready: "Готов",
  failed: "Ошибка",
};

// Состояния, при которых на сервере ещё идёт работа: пока в списке есть хоть одно
// такое — экран опрашивает список; как только их нет, опрос прекращается.
const PENDING_STATUSES = new Set(["uploaded", "processing"]);

export function isDocumentPending(document: UploadedDocument): boolean {
  return PENDING_STATUSES.has(String(document.status));
}

export function statusLabel(status: string): string {
  return DOCUMENT_STATUS_LABELS[status] ?? status;
}

export function statusTone(status: string): "ok" | "warn" | "bad" | "muted" {
  if (status === "ready") return "ok";
  if (status === "failed") return "bad";
  if (status === "processing" || status === "uploaded") return "warn";
  return "muted";
}

export function formatSize(bytes: number | null | undefined): string {
  const value = Number(bytes ?? 0);
  if (!Number.isFinite(value) || value <= 0) return "—";
  if (value < 1024) return `${value} Б`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} КБ`;
  return `${(value / 1024 / 1024).toFixed(1)} МБ`;
}

/**
 * Текст ошибки для пользователя.
 *
 * Клиент кладёт в Error.message СЫРОЕ тело ответа, а FastAPI отдаёт {"detail": "..."},
 * поэтому без разбора пользователь читал бы JSON с фигурными скобками. Тело не JSON —
 * показываем как есть, это честнее выдуманной формулировки.
 */
export function detailText(error: unknown, fallback: string): string {
  const raw = error instanceof Error ? error.message.trim() : "";
  if (!raw) return fallback;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = (parsed as { detail: unknown }).detail;
      if (typeof detail === "string") return detail || fallback;
      if (Array.isArray(detail)) {
        const parts = detail
          .map((item) => (item && typeof item === "object" && "msg" in item ? String((item as { msg: unknown }).msg) : String(item)))
          .filter(Boolean);
        return parts.length ? parts.join("; ") : fallback;
      }
      return detail === undefined || detail === null ? fallback : String(detail);
    }
  } catch {
    // тело не JSON — вернём как пришло
  }
  return raw;
}

export function toText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

// Список пунктов из JSON-поля. Сервер обещает список, но карточка не должна зависеть
// от этого обещания: строка станет списком из одного пункта, мусор — пустым списком.
export function asList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => toText(item)).filter((item) => item !== "—");
  }
  if (typeof value === "string" && value.trim()) return [value.trim()];
  return [];
}

// Якорь: «страница 14». Единица приходит с документом, номер — с фактом.
export function anchorText(anchor: number | string | null, unit: string | null): string {
  if (anchor === null || anchor === undefined || anchor === "") return "—";
  if (typeof anchor === "number" || /^\d+$/.test(String(anchor))) {
    return `${unit || "блок"} ${anchor}`;
  }
  return String(anchor);
}
