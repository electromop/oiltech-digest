import type { ExchangeCard, ExchangePackage, ExchangeReceipt } from "../../api/exchangeJournal";
import { formatMoment } from "../feedbackJournal/JournalEntryCard";
import { monthLabel } from "../shared/feedWindow";

// Пакет для потребителя в журнале обмена (#198; квитанции и обновление: #200): вид, номер, месяц, состояние (собран,
// забран, принят, уже был, отклонён с ответом потребителя), состав со ссылками на карточки витрины и что сборка сделала
// сверх состава.

export const KIND_LABELS: Record<string, string> = { release: "Выпуск", addition: "Дозагрузка", update: "Обновление" };
// Итог квитанции потребителя: те же слова, что у сервера (exchange_store.RECEIPT_OUTCOMES, тест сверяет).
const RECEIPT_LABELS: Record<ExchangeReceipt["outcome"], string> = {
  accepted: "принят", already: "уже был", rejected: "отклонён",
};

// Состояние пакета: собран, забран или итог квитанции потребителя.
export type PackageState = "built" | "taken" | ExchangeReceipt["outcome"];
export const STATE_LABELS: Record<PackageState, string> = { built: "собран", taken: "забран", ...RECEIPT_LABELS };

export function packageState(item: ExchangePackage): PackageState {
  if (item.receipt) return item.receipt.outcome;
  return item.first_taken_at ? "taken" : "built";
}

// Карточка в витрине: бизнес на бизнес-ленте, остальное на технологическом радаре; витрина ищет её по номеру.
export function cardHref(card: ExchangeCard): string {
  const screen = card.category === "business" ? "business-feed" : "signal-radar";
  return `?screen=${screen}&q=${encodeURIComponent(`#${card.signal_id}`)}`;
}

export function packageTitle(item: ExchangePackage): string {
  const kind = KIND_LABELS[item.kind] ?? item.kind;
  return item.month ? `${kind} №${item.number} · ${monthLabel(item.month)}` : `${kind} №${item.number}`;
}

function State({ item }: { item: ExchangePackage }) {
  const state = packageState(item);
  const tone = state === "rejected" ? "bad" : state === "built" ? "warn" : "ok";
  return <span className={`miniPill ${tone}`}>{STATE_LABELS[state] ?? state}</span>;
}

function Composition({ item }: { item: ExchangePackage }) {
  if (item.cards.length) {
    return (
      <ul className="journalLines">
        {item.cards.map((card) => (
          <li key={card.signal_id}>
            <a href={cardHref(card)}>№{card.signal_id}{card.title ? ` ${card.title}` : ""}</a>
          </li>
        ))}
      </ul>
    );
  }
  return (
    <p className="metaText">
      {item.kind === "update" ? "Карточек нет: только склейки и снятия." : "Пустой выпуск: в месяце не было отметок «в дайджест»."}
    </p>
  );
}

export function PackageEntry({ item }: { item: ExchangePackage }) {
  const taken = Boolean(item.first_taken_at);
  const notes = item.notes ?? [];
  const receipt = item.receipt;
  const rejected = receipt?.outcome === "rejected";
  return (
    <article className={rejected ? "journalEntry journalEntryRefused" : "journalEntry"} aria-label={packageTitle(item)}>
      <header className="journalEntryHead">
        <span className="journalKind">{packageTitle(item)}</span>
        <State item={item} />
        {item.overdue ? <span className="miniPill warn">не забран больше суток</span> : null}
        <span className="metaText">Собран {formatMoment(item.built_at)}{item.built_by ? ` · ${item.built_by}` : ""}</span>
        <span className="metaText">
          {taken
            ? `Забран ${formatMoment(item.first_taken_at)}, последний раз ${formatMoment(item.last_taken_at)}`
            : "Потребитель ещё не забирал"}
        </span>
      </header>
      {receipt ? (
        <div className={rejected ? "documentAlert bad" : "documentAlert"} role={rejected ? "alert" : undefined}
          aria-label={`Квитанция на пакет №${item.number}`}>
          Квитанция {formatMoment(receipt.at)}: {receipt.message || RECEIPT_LABELS[receipt.outcome]}
        </div>
      ) : null}
      <div className="journalEntryBody">
        <section aria-label={`Состав пакета №${item.number}`}>
          <p className="journalLabel">Карточек: {item.cards.length}</p>
          <Composition item={item} />
        </section>
        {notes.length ? (
          <section aria-label={`Сборка пакета №${item.number}`}>
            <p className="journalLabel">Сборка</p>
            <ul className="journalLines">{notes.map((note) => <li key={note}>{note}</li>)}</ul>
          </section>
        ) : null}
      </div>
    </article>
  );
}
