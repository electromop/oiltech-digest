import { apiFetch } from "./client";

// Журнал обмена с потребителем выпуска (#198, спека #197): пакеты для потребителя, их состав и состояние, дозагрузка
// по номерам. Только администратору: участнику сервер отвечает 403.

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

export type ExchangeJournal = { settings: ExchangeSettings; packages: ExchangePackage[] };

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

export function buildAddition(month: string, numbers: number[]) {
  return apiFetch<AdditionResult>("/api/exchange-journal/additions", {
    method: "POST",
    body: JSON.stringify({ month, numbers }),
  });
}
