import { apiFetch } from "./client";
import type { ReadinessStep } from "./types";

// Журнал обмена с потребителем выпуска (#198, спека #197): пакеты для потребителя, их состав и состояние, дозагрузка
// по номерам, готовность выпуска месяца и «Отправить как есть» (#199). Только администратору: участнику сервер
// отвечает 403.

export type ExchangePackageKind = "release" | "addition";

export type ExchangeCard = {
  signal_id: number;
  title: string | null;
  // Категория сигнала: по ней ссылка ведёт на бизнес-ленту или технологический радар.
  category: string | null;
};

export type ExchangePackage = {
  number: number;
  kind: ExchangePackageKind;
  month: string | null;
  built_at: string;
  built_by: string | null;
  first_taken_at: string | null;
  last_taken_at: string | null;
  cards: ExchangeCard[];
  // Что сборка сделала сверх состава (дубли корнем склейки, уже отправленные пропущены), строками сервера: те же
  // строки печатает команда exchange-status.
  notes: string[];
};

export type ExchangeSettings = {
  enabled: boolean;
  first_month: string | null;
  signing_keys: string[];
  consumer_keys: string[];
  addresses: number;
  token: boolean;
  // Что мешает собрать пакет (ключи) и что мешает потребителю его забрать (адрес, хэш токена).
  problems: string[];
  point_problems: string[];
};

// Неготовый материал выпуска: номер карточки (по нему ссылка на витрину; у статьи без сигнала ленты пусто), вид и номер
// материала в выпуске, заголовок и категория карточки, чего не хватает до готовности.
export type ReadinessMaterial = {
  signal_id: number | null;
  kind: "article" | "signal";
  id: number;
  title: string | null;
  category: string | null;
  missing: ReadinessStep[];
};

// Готовность выпуска месяца: сколько материалов и сколько готово, собран ли пакет выпуска, можно ли отправить его как
// есть (месяц закрыт, пакета нет) и почему нельзя.
export type ExchangeReadiness = {
  month: string;
  closed: boolean;
  release: number | null;
  total: number;
  ready: number;
  not_ready: ReadinessMaterial[];
  as_is: { allowed: boolean; reason: string | null };
};

export type ExchangeJournal = { settings: ExchangeSettings; packages: ExchangePackage[]; readiness?: ExchangeReadiness };

export type ReleaseResult = {
  package: number;
  month: string;
  sent: number[];
  not_ready: Array<{ signal_id: number; missing: ReadinessStep[] }>;
  message: string;
};

export type AdditionResult = {
  package: number | null;
  sent: number[];
  skipped: Array<{ signal_id: number; package: number; month: string }>;
  moved: Array<{ requested: number; root: number }>;
  message: string;
};

export function getExchangeJournal() {
  return apiFetch<ExchangeJournal>("/api/exchange-journal");
}

export function getExchangeReadiness(month: string) {
  return apiFetch<ExchangeReadiness>(`/api/exchange-journal/readiness?month=${encodeURIComponent(month)}`);
}

export function sendReleaseAsIs(month: string) {
  return apiFetch<ReleaseResult>("/api/exchange-journal/releases", { method: "POST", body: JSON.stringify({ month }) });
}

export function buildAddition(month: string, numbers: number[]) {
  return apiFetch<AdditionResult>("/api/exchange-journal/additions", {
    method: "POST",
    body: JSON.stringify({ month, numbers }),
  });
}
