import { ApiError, apiFetch } from "./client";

// Строка списка дочерних обществ «дочка → мать» (экран «Дочерние общества», решение владельца 20.10).
// Кто добавил — почта учётки; учётку удалили — пусто.
export type CompanyParent = {
  id: number;
  child_name: string;
  parent_name: string;
  note: string | null;
  created_by: number | null;
  created_by_email: string | null;
  created_at: string | null;
};

// Строка вставки списком после разбора: номер строки в тексте и ошибка (null — строка в порядке).
export type CompanyParentLine = {
  line: number;
  child_name: string;
  parent_name: string;
  note: string | null;
  problem: string | null;
};

export type CompanyParentsImport = {
  ok: boolean;
  applied: boolean;
  problems: number;
  lines: CompanyParentLine[];
};

// Список и подсказки формы: names, имена участников карточек из базы (справочник отдаёт /api/signals/dictionaries).
export type CompanyParentsList = {
  pairs: CompanyParent[];
  names: string[];
};

export function listCompanyParents() {
  return apiFetch<CompanyParentsList>("/api/company-parents");
}

export function addCompanyParent(pair: { child_name: string; parent_name: string; note: string | null }) {
  return apiFetch<{ ok: boolean; pair: CompanyParent }>("/api/company-parents", {
    method: "POST",
    body: JSON.stringify(pair),
  });
}

export function deleteCompanyParent(pairId: number) {
  return apiFetch<{ ok: boolean }>(`/api/company-parents/${pairId}`, {
    method: "DELETE",
  });
}

// Вставка списком «дочка; мать». Без apply сервер только разбирает строки. С apply пишет все строки разом,
// а при ошибке хоть в одной отвечает 400 с тем же разбором: ничего не записано, разбор возвращаем как ответ.
export async function importCompanyParents(text: string, apply: boolean): Promise<CompanyParentsImport> {
  try {
    return await apiFetch<CompanyParentsImport>("/api/company-parents/import", {
      method: "POST",
      body: JSON.stringify({ text, apply }),
    });
  } catch (error) {
    const lines = error instanceof ApiError && error.status === 400 ? refusedLines(error.message) : null;
    if (!lines) throw error;
    return { ok: false, applied: false, problems: lines.filter((line) => line.problem).length, lines };
  }
}

// Текст отказа с разбором строк: apiFetch отдаёт тело как есть, когда detail не строка.
function refusedLines(message: string): CompanyParentLine[] | null {
  try {
    const parsed = JSON.parse(message) as { detail?: { lines?: unknown } };
    return Array.isArray(parsed?.detail?.lines) ? (parsed.detail.lines as CompanyParentLine[]) : null;
  } catch {
    return null;
  }
}
