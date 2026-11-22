import type { PointRefusal, ReactionPackage } from "../../api/exchangeJournal";
import { formatMoment } from "../feedbackJournal/JournalEntryCard";

// Пакеты откликов потребителя и ответ платформы на них (#200), отказы точки обмена с причиной: пакет откликов или
// квитанция, которые точка не приняла (потребитель шлёт непринятое каждым проходом: попытки считаются).

const REFUSED: Record<PointRefusal["kind"], string> = { reactions: "Пакет откликов", receipt: "Квитанция на пакет" };

type Props = { packages: ReactionPackage[]; refusals: PointRefusal[] };

export function ExchangeReactionsPanel({ packages, refusals }: Props) {
  return (
    <section className="panel" aria-label="Пакеты откликов">
      <div className="panelHeader"><h2>Пакеты откликов</h2></div>
      {refusals.length ? (
        <div className="documentAlert bad" role="alert" aria-label="Отказы точки обмена">
          <p>Точка обмена не приняла:</p>
          <ul className="journalLines">
            {refusals.map((one) => (
              <li key={`${one.kind}:${one.number}:${one.reason}`}>
                {REFUSED[one.kind] ?? one.kind} №{one.number}: {one.reason} Попыток {one.attempts}, последняя{" "}
                {formatMoment(one.last_at)}.
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {packages.length ? (
        <ul className="journalLines" aria-label="Принятые пакеты откликов">
          {packages.map((one) => (
            <li key={one.number}>
              Пакет откликов №{one.number} · {one.source} · откликов {one.reactions} · принят{" "}
              {formatMoment(one.received_at)}{one.repeats ? `, повторов ${one.repeats}` : ""}: {one.answer}
            </li>
          ))}
        </ul>
      ) : (
        <div className="emptyState">Пакетов откликов ещё нет.</div>
      )}
    </section>
  );
}
