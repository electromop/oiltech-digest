import { apiFetch } from "./client";
import type { ReadinessStep } from "./types";

// Журнал обмена с потребителем выпуска (#198, спека #197): пакеты для потребителя, их состав и состояние, дозагрузка
// по номерам, готовность выпуска месяца и «Отправить как есть» (#199), квитанции потребителя, пакеты откликов, отказы
// точки обмена и обновление (#200). Только администратору: участнику сервер отвечает 403.

export type ExchangePackageKind = "release" | "addition" | "update";

// Квитанция потребителя на пакет: принят, уже был или отклонён, и его ответ одной строкой.
export type ExchangeReceipt = { outcome: "accepted" | "already" | "rejected"; message: string; at: string };

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
  // Не забран больше суток.
  overdue?: boolean;
  receipt?: ExchangeReceipt | null;
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
  // Ключи откликов: расшифровка платформы и подпись потребителя (отпечатки).
  decryption_keys?: string[];
  consumer_signing_keys?: string[];
  addresses: number;
  token: boolean;
  // Что мешает собрать пакет (ключи), принять пакет откликов (ключи откликов) и потребителю забрать пакет (адрес, хэш
  // токена).
  problems: string[];
  reaction_problems?: string[];
  point_problems: string[];
};

// Пакет откликов потребителя: номер его счёта, метка источника, сколько откликов, ответ платформы, когда принят и
// сколько раз приходил повтором.
export type ReactionPackage = {
  number: number;
  source: string;
  signer: string;
  reactions: number;
  answer: string;
  received_at: string;
  last_received_at: string;
  repeats: number;
};

// Отказ точки обмена: что не принято (пакет откликов или квитанция), номер, причина, сколько попыток.
export type PointRefusal = {
  kind: "reactions" | "receipt";
  number: number;
  reason: string;
  attempts: number;
  first_at: string;
  last_at: string;
};

// Запуск обновления: шаг раз в сутки или «Собрать обновление сейчас», пакет и итог словами.
export type UpdateRun = {
  trigger: "schedule" | "manual";
  package_number: number | null;
  author: string | null;
  note: string | null;
  started_at: string;
};

// Отправленный сигнал, который у потребителя не действует как прежде: снят, ушёл склейкой, расклеен на платформе.
export type SentState = {
  signal_id: number;
  state: "removed" | "merged" | "unmerged";
  state_label: string;
  merged_into: number | null;
  state_package: number | null;
  state_at: string | null;
  title: string | null;
  category: string | null;
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

export type ExchangeJournal = {
  settings: ExchangeSettings;
  packages: ExchangePackage[];
  readiness?: ExchangeReadiness;
  refusals?: PointRefusal[];
  reaction_packages?: ReactionPackage[];
  updates?: UpdateRun[];
  sent_states?: SentState[];
};

export type UpdateResult = { package: number | null; message: string };

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

export function buildUpdateNow() {
  return apiFetch<UpdateResult>("/api/exchange-journal/updates", { method: "POST" });
}

export function buildAddition(month: string, numbers: number[]) {
  return apiFetch<AdditionResult>("/api/exchange-journal/additions", {
    method: "POST",
    body: JSON.stringify({ month, numbers }),
  });
}
