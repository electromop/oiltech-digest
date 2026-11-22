import { ApiError } from "../../api/client";

// Итог действия вкладки «Журнал обмена» строкой у формы: ответ сервера или отказ с причиной.
export type Outcome = { refused: boolean; text: string };

// Сбой действия вкладки: вход истёк (401), отказ сервера с причиной (409, 422) строкой у формы, прочее тостом.
export function reportFailure(error: unknown, fallback: string, onUnauthorized: () => void,
  showToast: (text: string, tone?: "default" | "error") => void, setResult: (outcome: Outcome) => void) {
  if (error instanceof ApiError && error.status === 401) onUnauthorized();
  else if (error instanceof ApiError && (error.status === 409 || error.status === 422)) {
    setResult({ refused: true, text: error.message });
  } else showToast(error instanceof Error ? error.message : fallback, "error");
}
