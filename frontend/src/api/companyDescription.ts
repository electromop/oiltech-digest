import { apiFetch } from "./client";

// Описание Компании для агента «Гипотеза для Компании» (#162): действующий текст, кто и когда правил, прежние правки.
export type CompanyDescription = {
  text: string;
  updated_by: string | null;
  updated_at: string | null;
  history: Array<{ updated_by: string | null; updated_at: string; chars: number }>;
};

export function getCompanyDescription() {
  return apiFetch<CompanyDescription>("/api/company-description");
}

export function saveCompanyDescription(text: string) {
  return apiFetch<CompanyDescription>("/api/company-description", {
    method: "PUT",
    body: JSON.stringify({ text }),
  });
}
