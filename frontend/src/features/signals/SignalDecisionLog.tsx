import { useId, useState } from "react";
import type { Signal } from "../../api/types";

// «Почему такое решение» и «Ход решения» (решение 03.10: заказчик видит причину и шаги). С пересмотра 23.10 шаги —
// лентой с этапами, свёрнуты по умолчанию: причина видна сразу, подробности — по кнопке.
export function SignalDecisionLog({ signal }: { signal: Signal }) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const steps = signal.decision_log ?? [];
  if (!signal.verdict_reason && !steps.length) return null;
  return (
    <div className="signalDecision">
      {signal.verdict_reason ? (
        <p className="signalDecisionReason">
          <strong>Почему такое решение:</strong> {signal.verdict_reason}
        </p>
      ) : null}
      {steps.length ? (
        <>
          <button
            type="button"
            className="signalDecisionToggle"
            aria-expanded={open}
            aria-controls={listId}
            onClick={() => setOpen((current) => !current)}
          >
            <svg className="groupChevron" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
              <path d="M4 6.5 8 10l4-3.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            Ход решения
            <span className="metaText">{steps.length} {stepWord(steps.length)}</span>
          </button>
          {open ? (
            <ol className="signalDecisionLog revealIn" id={listId}>
              {steps.map((step, index) => (
                <li key={index}>
                  <span className="signalDecisionStage">{step.stage}</span>
                  <span className="signalDecisionText">{step.text}</span>
                </li>
              ))}
            </ol>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function stepWord(count: number): string {
  const tens = count % 100;
  const units = count % 10;
  if (units === 1 && tens !== 11) return "шаг";
  if (units >= 2 && units <= 4 && (tens < 12 || tens > 14)) return "шага";
  return "шагов";
}
