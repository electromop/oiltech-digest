import { monthLabel } from "../articles/feedWindow";
import type { DigestExportFormat } from "./digestExport";

export type DigestView = "issue" | "archive";

type Props = {
  activeMonth: string;
  issueCount: number;
  month: string;
  months: string[];
  firstOpenMonth: string;
  statusText: string;
  onMonthChange: (month: string) => void;
};

// Шапка дайджеста (пересмотр 23.10): какой выпуск открыт и сколько в нём материалов, выбор выпуска и статус черновика.
// Выпуск — месяц; прошлые помечены «архив», «Все месяцы» остаются, только пока окно месяца неизвестно.
export function DigestHeader({ activeMonth, issueCount, month, months, firstOpenMonth, statusText, onMonthChange }: Props) {
  return (
    <header className="screenHeader digestHeader">
      <div>
        <h1>Месячный дайджест</h1>
        <p>
          Выпуск за {monthLabel(activeMonth)}: {issueCount} {materialsWord(issueCount)} в выпуске. Предпросмотр PDF
          показывает выпуск таким, каким он уйдёт.
        </p>
      </div>
      <div className="digestHeaderControls">
        <label className="field digestMonthField">
          <span>Выпуск</span>
          <select value={month} onChange={(event) => onMonthChange(event.target.value)}>
            {months.length ? null : <option value="">Все месяцы</option>}
            {months.map((option) => (
              <option key={option} value={option}>
                {firstOpenMonth && option < firstOpenMonth ? `${monthLabel(option)} · архив` : monthLabel(option)}
              </option>
            ))}
          </select>
        </label>
        <span className="statusPill">{statusText}</span>
      </div>
    </header>
  );
}

// Сборка выпуска или архив прошлых выпусков; у архива — сколько в нём выпусков.
export function DigestViewSwitch({ view, archiveCount, onChange }: { view: DigestView; archiveCount: number; onChange: (view: DigestView) => void }) {
  return (
    <div className="viewTabs digestViewSwitch" role="group" aria-label="Раздел дайджеста">
      <button type="button" className={view === "issue" ? "primaryButton" : "ghostButton"} aria-pressed={view === "issue"} onClick={() => onChange("issue")}>
        Сборка выпуска
      </button>
      <button type="button" className={view === "archive" ? "primaryButton" : "ghostButton"} aria-pressed={view === "archive"} onClick={() => onChange("archive")}>
        Архив выпусков{archiveCount ? ` · ${archiveCount}` : ""}
      </button>
    </div>
  );
}

type ActionProps = {
  archive: boolean;
  busy: boolean;
  draftBusy: boolean;
  hasManualChanges: boolean;
  onSave: () => void;
  onReset: () => void;
  onExport: (format: DigestExportFormat) => void;
};

// Действия с выпуском — две группы: черновик (у прошлого выпуска его нет) и выгрузка. В узкой колонке выгрузка
// уходит своей строкой вправо, а не рассыпается по кнопке.
export function DigestActions({ archive, busy, draftBusy, hasManualChanges, onSave, onReset, onExport }: ActionProps) {
  return (
    <div className="digestActionBar">
      {archive ? null : (
        <div className="digestActionGroup">
          <button type="button" className="ghostButton" disabled={draftBusy} onClick={onSave}>
            {draftBusy ? "Сохраняем черновик…" : "Сохранить черновик"}
          </button>
          <button type="button" className="ghostButton" disabled={!hasManualChanges} onClick={onReset}>
            Сбросить изменения
          </button>
        </div>
      )}
      <div className="digestActionGroup digestExportGroup" role="group" aria-label="Выгрузка выпуска">
        <button type="button" className="primaryButton" disabled={busy} onClick={() => onExport("pdf")}>
          Скачать PDF
        </button>
        <button type="button" className="ghostButton" disabled={busy} onClick={() => onExport("docx")}>
          DOCX
        </button>
        <button type="button" className="ghostButton" disabled={busy} onClick={() => onExport("html")}>
          HTML
        </button>
      </div>
    </div>
  );
}

function materialsWord(count: number): string {
  const tens = count % 100;
  const units = count % 10;
  if (units === 1 && tens !== 11) return "материал";
  if (units >= 2 && units <= 4 && (tens < 12 || tens > 14)) return "материала";
  return "материалов";
}
