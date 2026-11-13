import { useState } from "react";
import type { JournalChange, JournalEntry, ProposalDecision } from "../../api/feedbackJournal";
import { verdictName } from "../signals/SignalFeedbackForm";

// Строка журнала ОС (#189): когда, кто, что пришло, что система сделала или предлагает, статус и путь действия.
// Кнопки решений, отмены и закрытия задачи сервер разрешает только администратору: экран показывает их по флагам
// строки (can_decide, can_undo, can_close).

export const STATUS_LABELS: Record<string, string> = {
  done: "сделано",
  pending: "ждёт решения",
  approved: "согласовано",
  modified: "изменено",
  dismissed: "убрано",
  applied: "применено",
  undone: "отменено",
  stale: "устарело",
};

export const AGENT_LABELS: Record<string, string> = {
  otbor: "«Отбор»",
  summary: "«Суть и перевод»",
  card: "«Карточка»",
  scores: "Баллы",
  dedup: "«Дедуп»",
};

export const SHOWCASE_LABELS: Record<string, string> = {
  business: "Бизнес-лента",
  technology: "Технологический радар",
  other: "Другое",
};

const KIND_LABELS: Record<string, string> = {
  mark: "Отметка",
  signal_proposal: "Предложение разбора ОС",
  sample_task: "Задача выборки",
};

const STATUS_TONE: Record<string, string> = {
  done: "ok",
  applied: "ok",
  pending: "warn",
  approved: "info",
  modified: "info",
  dismissed: "muted",
  undone: "muted",
  stale: "muted",
};

type Props = {
  entry: JournalEntry;
  highlighted: boolean;
  busy: boolean;
  onDecide: (entry: JournalEntry, decision: ProposalDecision, changes?: Array<{ field: string; value: string }>) => void;
  onUndo: (entry: JournalEntry) => void;
  onClose: (entry: JournalEntry) => void;
};

export function formatMoment(value: string | null | undefined): string {
  if (!value) return "";
  return new Date(value).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric",
    hour: "2-digit", minute: "2-digit" });
}

export function JournalEntryCard({ entry, highlighted, busy, onDecide, onUndo, onClose }: Props) {
  const [editing, setEditing] = useState(false);
  const [wording, setWording] = useState<Record<string, string>>({});

  function startEdit() {
    setWording(Object.fromEntries((entry.changes ?? []).map((change) => [change.field, change.shown])));
    setEditing(true);
  }

  function saveWording() {
    onDecide(entry, "modify", Object.entries(wording).map(([field, value]) => ({ field, value })));
    setEditing(false);
  }

  return (
    <article className={`journalEntry${highlighted ? " journalEntryHighlighted" : ""}`} id={`journal-${entry.key}`}
      aria-label={`${KIND_LABELS[entry.kind]} №${entry.id}`}>
      <header className="journalEntryHead">
        <span className="journalKind">{KIND_LABELS[entry.kind]} №{entry.id}</span>
        <span className={`miniPill ${STATUS_TONE[entry.status] ?? "muted"}`}>{STATUS_LABELS[entry.status] ?? entry.status}</span>
        <span className="metaText">{formatMoment(entry.at)} · {entry.author ?? "без имени"}</span>
        {entry.signal_id ? (
          <span className="metaText">
            Карточка №{entry.signal_id}{entry.signal_title ? `: ${entry.signal_title}` : ""}
            {entry.showcase ? ` · ${SHOWCASE_LABELS[entry.showcase] ?? entry.showcase}` : ""}
          </span>
        ) : null}
        {entry.agents.length ? (
          <span className="metaText">{entry.agents.map((agent) => AGENT_LABELS[agent] ?? agent).join(", ")}</span>
        ) : null}
      </header>
      <div className="journalEntryBody">
        <section aria-label="Что пришло">
          <h3 className="journalLabel">Что пришло</h3>
          <Incoming entry={entry} />
        </section>
        <section aria-label="Что сделала система">
          <h3 className="journalLabel">{entry.kind === "signal_proposal" ? "Что предлагает система" : "Что сделала система"}</h3>
          {editing ? <Wording changes={entry.changes ?? []} wording={wording} onChange={setWording} /> : <System entry={entry} />}
        </section>
      </div>
      <ol className="journalPath" aria-label="Путь действия">
        {entry.path.map((step, index) => (
          <li key={`${step.label}-${index}`}>
            {step.label}
            {step.author || step.at ? (
              <span className="metaText"> · {[step.author, formatMoment(step.at)].filter(Boolean).join(", ")}</span>
            ) : null}
          </li>
        ))}
      </ol>
      <div className="journalActions">
        {entry.can_decide && !editing ? (
          <>
            <button type="button" className="primaryButton compactButton" disabled={busy}
              onClick={() => onDecide(entry, "approve")}>Согласовать</button>
            <button type="button" className="ghostButton compactButton" disabled={busy} onClick={startEdit}>Изменить</button>
            <button type="button" className="ghostButton compactButton" disabled={busy}
              onClick={() => onDecide(entry, "dismiss")}>Убрать</button>
          </>
        ) : null}
        {editing ? (
          <>
            <button type="button" className="primaryButton compactButton" disabled={busy} onClick={saveWording}>
              Применить новую формулировку
            </button>
            <button type="button" className="ghostButton compactButton" onClick={() => setEditing(false)}>Отмена</button>
          </>
        ) : null}
        {entry.can_undo ? (
          <button type="button" className="ghostButton compactButton" disabled={busy} onClick={() => onUndo(entry)}>
            Отменить
          </button>
        ) : null}
        {entry.can_close ? (
          <button type="button" className="ghostButton compactButton" disabled={busy} onClick={() => onClose(entry)}>
            Просмотрено
          </button>
        ) : null}
      </div>
    </article>
  );
}

function Incoming({ entry }: { entry: JournalEntry }) {
  if (entry.kind === "mark") {
    const lines = [
      entry.verdict ? `Оценка: ${verdictName(entry.verdict)}` : null,
      entry.reason ? `Обоснование: ${entry.reason}` : null,
      entry.comment ? `Рекомендации AI-агенту: ${entry.comment}` : null,
      entry.corrected_title ? `Заголовок: ${entry.corrected_title}` : null,
      entry.corrected_thesis ? `Суть: ${entry.corrected_thesis}` : null,
      entry.duplicate_of_signal_id ? `Дубль: главная №${entry.duplicate_of_signal_id}` : null,
    ].filter(Boolean) as string[];
    return <ul className="journalLines">{lines.map((line) => <li key={line}>{line}</li>)}</ul>;
  }
  if (entry.kind === "sample_task") {
    return <p>Случайные публикации, отклонённые «Отбором» за месяц{entry.month ? ` (${entry.month})` : ""}, поровну по темам: {entry.publications?.length ?? 0}.</p>;
  }
  return (
    <ul className="journalLines" aria-label="Основание">
      {(entry.basis ?? []).map((mark) => (
        <li key={mark.id}>
          Отметка №{mark.id} · {mark.author ?? "без имени"}
          {mark.verdict ? ` · ${verdictName(mark.verdict)}` : ""}
          {[mark.reason, mark.comment, mark.corrected_title, mark.corrected_thesis].filter(Boolean).map((text) => (
            <span className="journalQuote" key={String(text)}> «{text}»</span>
          ))}
        </li>
      ))}
    </ul>
  );
}

function System({ entry }: { entry: JournalEntry }) {
  if (entry.kind === "mark") {
    return entry.system?.length
      ? <ul className="journalLines">{entry.system.map((line) => <li key={line}>{line}</li>)}</ul>
      : <p className="metaText">Записано, карточку не меняет.</p>;
  }
  if (entry.kind === "sample_task") {
    return (
      <ul className="journalLines" aria-label="Публикации на просмотр">
        {(entry.publications ?? []).map((publication) => (
          <li key={publication.signal_id}>
            {publication.url ? <a href={publication.url} target="_blank" rel="noreferrer">{publication.title}</a> : publication.title}
            <span className="metaText"> · {publication.theme}{publication.reason ? ` · ${publication.reason}` : ""}</span>
          </li>
        ))}
      </ul>
    );
  }
  return (
    <table className="journalChanges">
      <thead><tr><th>Поле</th><th>Было</th><th>Станет</th><th>Цитата</th></tr></thead>
      <tbody>
        {(entry.changes ?? []).map((change) => (
          <tr key={change.field}>
            <td>{change.label}</td>
            <td>{change.previous ?? "не определено"}</td>
            <td>{change.shown}</td>
            <td>{change.quote ? `«${change.quote}»` : ""}{change.event_ids.length ? ` (отметки №${change.event_ids.join(", №")})` : ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Wording({ changes, wording, onChange }: { changes: JournalChange[]; wording: Record<string, string>;
  onChange: (next: Record<string, string>) => void }) {
  return (
    <div className="journalWording">
      {changes.map((change) => (
        <label className="field" key={change.field}>
          <span>{change.label}</span>
          {change.field === "summary" ? (
            <textarea value={wording[change.field] ?? ""} rows={4}
              onChange={(event) => onChange({ ...wording, [change.field]: event.target.value })} />
          ) : (
            <input value={wording[change.field] ?? ""}
              onChange={(event) => onChange({ ...wording, [change.field]: event.target.value })} />
          )}
        </label>
      ))}
    </div>
  );
}
