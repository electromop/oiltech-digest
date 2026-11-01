import type { Article, DigestContentItem } from "../../api/types";
import { scoreClass } from "../shared/scoreScale";

export type ItemKind = { kind: string | null; label: string | null };
// Материал, как он выглядит в самом выпуске: вид и суть, сжатая сервером под площадь карточки выпуска.
export type IssueItem = ItemKind & { summary: string | null };

type Props = {
  items: Article[];
  // Сигналы радара, отмеченные в дайджест: в черновике не хранятся, в выпуске идут после статей, по баллу.
  radarItems: DigestContentItem[];
  available: Article[];
  issueItems: Map<number, IssueItem>;
  readOnly: boolean;
  busy: boolean;
  hasManualChanges: boolean;
  draftInfo: string;
  onMove: (articleId: number, direction: -1 | 1) => void;
  onRemoveFromIssue: (articleId: number) => void;
  onRemoveFromDigest: (articleId: number) => void;
  onRemoveSignal: (signalId: number) => void;
  onAdd: (articleId: number) => void;
};

type FilterProps = {
  search: string;
  tag: string;
  topTags: string[];
  scoreMin: number;
  scoreMax: number;
  onSearch: (value: string) => void;
  onTag: (value: string) => void;
  onScoreMin: (value: number) => void;
  onScoreMax: (value: number) => void;
};

// Фильтры состава: поиск, тема и балл; на узкой колонке — по два в ряд, на телефоне — столбиком.
export function DigestFilters(props: FilterProps) {
  return (
    <div className="digestFiltersRow">
      <label className="field">
        <span>Поиск</span>
        <input value={props.search} onChange={(event) => props.onSearch(event.target.value)} placeholder="Найти статью" />
      </label>
      <label className="field">
        <span>Тема</span>
        <select value={props.tag} onChange={(event) => props.onTag(event.target.value)}>
          <option value="">Все темы</option>
          {props.topTags.map((option) => (
            <option key={option} value={option}>{option}</option>
          ))}
        </select>
      </label>
      <label className="field digestScoreField">
        <span>Балл от</span>
        <input type="number" inputMode="numeric" value={props.scoreMin} onChange={(event) => props.onScoreMin(Number(event.target.value || 0))} />
      </label>
      <label className="field digestScoreField">
        <span>Балл до</span>
        <input type="number" inputMode="numeric" value={props.scoreMax} onChange={(event) => props.onScoreMax(Number(event.target.value || 100))} />
      </label>
    </div>
  );
}

function formatDate(value: string | null) {
  return value ? new Date(`${value}T00:00:00`).toLocaleDateString("ru-RU") : "—";
}

// Плашка «Технология / Бизнес» у материала выпуска (решение владельца 23.10) — та же, что в самом выпуске.
export function KindBadge({ kind }: { kind?: ItemKind }) {
  if (!kind?.kind || !kind.label) return null;
  return <span className={`kindBadge ${kind.kind}`}>{kind.label}</span>;
}

// Состав выпуска одним списком в порядке PDF (решение владельца 24.10): статьи очереди, за ними сигналы радара;
// откуда материал, говорит плашка «Технология / Бизнес». Статьи двигаются стрелками, «Из выпуска» убирает только
// из этого выпуска, «Из дайджеста» снимает отметку; ниже — отмеченное, но не вошедшее в выпуск. Суть та же, что в PDF.
export function DigestQueue(props: Props) {
  const { items, radarItems, available, issueItems, readOnly, busy } = props;
  return (
    <div className="digestQueuePanel">
      <div className="digestQueueHeader">
        <div>
          <strong>Очередь выпуска</strong>
          <div className="metaText">{props.draftInfo}</div>
        </div>
        <div className="digestQueueHeaderMeta">
          {props.hasManualChanges ? <span className="miniPill warn">есть несохранённые изменения</span> : null}
          <span className="badge">{items.length + radarItems.length}</span>
        </div>
      </div>
      <ol className="digestPickListReact">
        {items.map((article, index) => (
          <li className="digestPickRow revealIn" key={article.id}>
            <div className="digestPickTop">
              <div className="digestPickIndex" aria-hidden="true">{String(index + 1).padStart(2, "0")}</div>
              <div className="digestPickMain">
                <div className="digestPickMetaRow">
                  <KindBadge kind={issueItems.get(article.id)} />
                  <span className="metaText">{article.tag}</span>
                </div>
                <a href={article.url} target="_blank" rel="noreferrer">
                  <strong>{article.title}</strong>
                </a>
              </div>
              <span className={`scoreBadge ${scoreClass(Number(article.score || 0))}`} title="Балл материала">
                <strong>{Math.round(Number(article.score || 0))}</strong>
              </span>
            </div>
            <p className="digestPickSummary">{issueItems.get(article.id)?.summary || article.summary || "Суть ещё не сформирована."}</p>
            <div className="digestPickFooter">
              <span>{article.source}</span>
              <span>{formatDate(article.date)}</span>
              {readOnly ? null : (
                <span className="digestPickActions">
                  <button
                    type="button"
                    className="ghostButton compactButton"
                    disabled={index === 0}
                    onClick={() => props.onMove(article.id, -1)}
                    aria-label="Поднять материал выше"
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    className="ghostButton compactButton"
                    disabled={index === items.length - 1}
                    onClick={() => props.onMove(article.id, 1)}
                    aria-label="Опустить материал ниже"
                  >
                    ↓
                  </button>
                  <button type="button" className="ghostButton compactButton" onClick={() => props.onRemoveFromIssue(article.id)}>
                    Из выпуска
                  </button>
                  <button type="button" className="dangerButton compactButton" disabled={busy} onClick={() => props.onRemoveFromDigest(article.id)}>
                    Из дайджеста
                  </button>
                </span>
              )}
            </div>
          </li>
        ))}
        {radarItems.map((item, index) => (
          <li className="digestPickRow revealIn" key={`signal-${item.signal_id ?? item.url}`}>
            <div className="digestPickTop">
              <div className="digestPickIndex" aria-hidden="true">{String(items.length + index + 1).padStart(2, "0")}</div>
              <div className="digestPickMain">
                <div className="digestPickMetaRow">
                  <KindBadge kind={{ kind: item.kind ?? null, label: item.kind_label ?? null }} />
                  <span className="metaText">{item.category}</span>
                </div>
                <a href={item.url} target="_blank" rel="noreferrer">
                  <strong>{item.title}</strong>
                </a>
              </div>
              <span className={`scoreBadge ${scoreClass(Number(item.score || 0))}`} title="Балл сигнала">
                <strong>{Math.round(Number(item.score || 0))}</strong>
              </span>
            </div>
            <p className="digestPickSummary">{item.summary || "Суть ещё не сформирована."}</p>
            <div className="digestPickFooter">
              {item.source ? <span>{item.source}</span> : null}
              {item.published_at ? <span>{formatDate(item.published_at)}</span> : null}
              {readOnly || item.signal_id == null ? null : (
                <span className="digestPickActions">
                  <button type="button" className="dangerButton compactButton" disabled={busy} onClick={() => props.onRemoveSignal(Number(item.signal_id))}>
                    Из дайджеста
                  </button>
                </span>
              )}
            </div>
          </li>
        ))}
      </ol>
      {available.length && !readOnly ? (
        <details className="digestAvailablePanel">
          <summary>
            Отмечено, но не в выпуске <span className="badge">{available.length}</span>
          </summary>
          <div className="digestAvailableList">
            {available.slice(0, 12).map((article) => (
              <div className="digestAvailableRow" key={article.id}>
                <div>
                  <strong>{article.title}</strong>
                  <div className="metaText">
                    <KindBadge kind={issueItems.get(article.id)} /> {article.tag} · {article.source}
                  </div>
                </div>
                <button type="button" className="ghostButton compactButton" onClick={() => props.onAdd(article.id)}>
                  Добавить
                </button>
              </div>
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}
