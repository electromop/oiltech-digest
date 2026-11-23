import { useState } from "react";
import { buildUpdateNow, type SentState, type UpdateRun } from "../../api/exchangeJournal";
import { formatMoment } from "../feedbackJournal/JournalEntryCard";
import { cardHref } from "./ExchangePackageEntry";
import { reportFailure, type Outcome } from "./outcome";

// Обновление для потребителя (#200): раз в сутки уходят изменённые карточки отправленных сигналов, склейки, снятия и
// возвраты; «Собрать обновление сейчас» собирает его сразу. Последние запуски с итогом и отправленные сигналы, которые
// у потребителя сняты, ушли склейкой или расклеены на платформе (ссылки ведут на карточку витрины).

type Props = {
  runs: UpdateRun[];
  states: SentState[];
  onUnauthorized: () => void;
  showToast: (text: string, tone?: "default" | "error") => void;
  onBuilt: () => Promise<void>;
};

function runLabel(run: UpdateRun): string {
  const how = run.trigger === "schedule" ? "шаг раз в сутки" : `вручную${run.author ? `, ${run.author}` : ""}`;
  return `${formatMoment(run.started_at)} · ${how}: ${run.note ?? ""}`;
}

export function ExchangeUpdatePanel({ runs, states, onUnauthorized, showToast, onBuilt }: Props) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Outcome | null>(null);

  async function build() {
    setBusy(true);
    try {
      const answer = await buildUpdateNow();
      setResult({ refused: false, text: answer.message });
      await onBuilt();
    } catch (error) {
      reportFailure(error, "Обновление не собрано", onUnauthorized, showToast, setResult);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel" aria-label="Обновление">
      <div className="panelHeader"><h2>Обновление</h2></div>
      <p className="metaText">
        Раз в сутки потребителю уходят изменённые карточки отправленных сигналов, склейки, снятия и возвраты. Без
        изменений пакет не собирается.
      </p>
      <div className="journalActions">
        <button type="button" className="primaryButton compactButton" disabled={busy} onClick={() => void build()}>
          {busy ? "Собираем…" : "Собрать обновление сейчас"}
        </button>
      </div>
      {result ? (
        <div className={result.refused ? "documentAlert bad" : "documentAlert"} role={result.refused ? "alert" : "status"}>
          {result.text}
        </div>
      ) : null}
      {runs.length ? (
        <section aria-label="Запуски обновления">
          <p className="journalLabel">Последние запуски</p>
          <ul className="journalLines">
            {runs.map((run, index) => <li key={`${run.started_at}:${index}`}>{runLabel(run)}</li>)}
          </ul>
        </section>
      ) : null}
      {states.length ? (
        <section aria-label="Сняты и склеены у потребителя">
          <p className="journalLabel">Сняты и склеены у потребителя</p>
          <ul className="journalLines">
            {states.map((one) => (
              <li key={one.signal_id}>
                <a href={cardHref(one)}>№{one.signal_id}{one.title ? ` ${one.title}` : ""}</a>{" "}
                <span className="metaText">{one.state_label}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </section>
  );
}
