import type { ArchiveMonth } from "../../api/types";
import { monthLabel } from "../articles/feedWindow";

type Props = {
  months: ArchiveMonth[];
  // День смены окна месяца (сервер, FEED_ROLLOVER_DAY): прошлый месяц открыт по день накануне включительно.
  rolloverDay?: number;
  busy: boolean;
  onOpen: (month: string) => void;
  onDownload: (month: string) => void;
};

// Архив выпусков (решение владельца 23.10): итоговые выпуски прошлых месяцев. Месяц закрывается по окну отметок
// (прошлый месяц открыт по 10-е включительно); закрытый выпуск только смотрят и выгружают: менять его нельзя.
export function DigestArchive({ months, rolloverDay, busy, onOpen, onDownload }: Props) {
  const issues = months.filter((item) => item.digest > 0).sort((a, b) => (a.month < b.month ? 1 : -1));
  if (!issues.length) {
    return (
      <div className="emptyState">
        <strong>Прошлых выпусков пока нет</strong>
        <span>
          Выпуск попадает в архив, когда закрывается его месяц
          {rolloverDay && rolloverDay > 1 ? `: прошлый месяц открыт для отметок по ${rolloverDay - 1}-е число.` : "."}
        </span>
      </div>
    );
  }
  return (
    <div className="jobsTableWrap">
      <table className="jobsTable digestArchiveTable">
        <thead>
          <tr>
            <th scope="col">Выпуск</th>
            <th scope="col">Материалов в выпуске</th>
            <th scope="col">Статей месяца</th>
            <th scope="col"><span className="srOnly">Действия</span></th>
          </tr>
        </thead>
        <tbody>
          {issues.map((item) => (
            <tr key={item.month}>
              <th scope="row">{monthLabel(item.month)}</th>
              <td className="tabularNums">{item.digest}</td>
              <td className="tabularNums">{item.articles}</td>
              <td>
                <div className="digestArchiveActions">
                  <button type="button" className="ghostButton compactButton" onClick={() => onOpen(item.month)}>
                    Открыть
                  </button>
                  <button type="button" className="ghostButton compactButton" disabled={busy} onClick={() => onDownload(item.month)}>
                    Скачать PDF
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
