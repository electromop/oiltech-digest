// Отклики потребителя выпуска на карточке витрины (#200): строку видит только администратор (сервер отдаёт счёт только
// ему). Счёт по корню склейки на момент чтения: «в работе у N групп» потребителя и сколько стоит остальных кнопок.

const LABELS: Record<string, string> = {
  save: "Сохранить", export: "В выгрузку", useful: "Полезный сигнал", not_oilfield: "Не про нефтесервис",
  not_new: "Не новое", few_facts: "Мало фактов", card_error: "Ошибка в карточке",
};
const ORDER = ["save", "export", "useful", "not_oilfield", "not_new", "few_facts", "card_error"];

// «у 1 группы», «у 2 групп», «у 21 группы».
export function groupsWord(count: number): string {
  return count % 10 === 1 && count % 100 !== 11 ? "группы" : "групп";
}

export function consumerReactionsText(counts: Record<string, number> | null | undefined): string | null {
  if (!counts) return null;
  const parts: string[] = [];
  if (counts.work) parts.push(`в работе у ${counts.work} ${groupsWord(counts.work)}`);
  for (const kind of ORDER) {
    if (counts[kind]) parts.push(`${LABELS[kind]}: ${counts[kind]}`);
  }
  return parts.length ? `Отклики потребителя: ${parts.join(" · ")}` : null;
}

export function SignalConsumerReactions({ counts }: { counts?: Record<string, number> | null }) {
  const text = consumerReactionsText(counts);
  return text ? <div className="signalCardStatus metaText" aria-label="Отклики потребителя">{text}</div> : null;
}
