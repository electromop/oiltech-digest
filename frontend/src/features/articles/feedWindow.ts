import type { FeedWindowInfo } from "../../api/types";

// Подписи окна месяца (ADR 0001, п. 6). Само правило живёт на сервере (feed_window.py),
// здесь только тексты для человека.

const MONTHS = [
  "январь", "февраль", "март", "апрель", "май", "июнь",
  "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
];
const MONTHS_GENITIVE = [
  "января", "февраля", "марта", "апреля", "мая", "июня",
  "июля", "августа", "сентября", "октября", "ноября", "декабря",
];

// «2026-08» → «август 2026». Незнакомый формат возвращается как есть.
export function monthLabel(month: string): string {
  const match = /^(\d{4})-(\d{2})$/.exec(month);
  const name = match ? MONTHS[Number(match[2]) - 1] : undefined;
  return match && name ? `${name} ${match[1]}` : month;
}

// Какие месяцы открыты и до какого числа виден прошлый.
export function windowPeriodText(feedWindow: FeedWindowInfo | null | undefined): string {
  if (!feedWindow || !feedWindow.months.length) return "";
  const first = feedWindow.months[0];
  const last = feedWindow.months[feedWindow.months.length - 1];
  if (first === last) return `Показан ${monthLabel(first)}. Прошлые месяцы — в архиве, только для просмотра.`;
  const firstName = MONTHS[Number(first.slice(5, 7)) - 1] ?? first;
  const lastGenitive = MONTHS_GENITIVE[Number(last.slice(5, 7)) - 1] ?? last;
  return `Показаны ${monthLabel(first)} и ${monthLabel(last)}: ${firstName} виден до ${feedWindow.rollover_day} ${lastGenitive}, пока собирается его выпуск.`;
}

export function archiveNoticeText(month: string): string {
  return `Архив за ${monthLabel(month)} — только просмотр: статус и отметку «в дайджест» здесь не поменять.`;
}
