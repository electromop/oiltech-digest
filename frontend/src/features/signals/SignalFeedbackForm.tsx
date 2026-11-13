import { useEffect, useId, useState } from "react";
import { listSignals } from "../../api/signals";
import type { Signal, SignalFeedbackPayload } from "../../api/types";

// Форма ОС (#188, спека #187): одна, как в технологическом радаре, у любой карточки обеих витрин и в «Отклонённых»,
// при любом происхождении (находка поиска, сигнал ленты, материал коллеги) и категории. Явные действия (правки, «Не тот
// блок», «Дубль», «Не релевантно») сразу применяет только отметка администратора; это решает сервер.

export type FeedbackDraft = {
  verdict: NonNullable<SignalFeedbackPayload["verdict"]> | "";
  reason: string;
  correctedTitle: string;
  correctedThesis: string;
  // Поле главной карточки «Дубля»: что вписано (номер или слова её заголовка); выбранная из подсказок: номер и
  // заголовок.
  duplicateText: string;
  duplicatePick: DuplicateChoice | null;
  comment: string;
};

export type DuplicateChoice = { id: number; title: string };

export const EMPTY_FEEDBACK_DRAFT: FeedbackDraft = {
  verdict: "",
  reason: "",
  correctedTitle: "",
  correctedThesis: "",
  duplicateText: "",
  duplicatePick: null,
  comment: "",
};

// Шкала заказчика (список эксперта заказчика от 13.09). Это оценка ЧЕЛОВЕКА и она намеренно
// отличается от «Решения платформы»: та: оценка модели. «Наблюдать» встречается в обеих,
// поэтому машинная подписана в карточке как «Решение платформы», а эта: как «Оценка сигнала».
export const VERDICT_LABELS: Array<{ value: FeedbackDraft["verdict"]; label: string }> = [
  { value: "", label: "Не оценено" },
  { value: "strong_signal", label: "Сильный сигнал — вынести в дайджест / обсуждать" },
  { value: "approved", label: "Полезный сигнал — релевантно, сохранить в базе" },
  { value: "watch_later", label: "Наблюдать — рано, нужен следующий этап" },
  { value: "background_material", label: "Фоновый материал — ориентир или контекст" },
  { value: "reject", label: "Низкая ценность / шум — по теме, но без новой ценности" },
  { value: "wrong_domain", label: "Не релевантно — вне интересов Компании" },
  // Встреча с заказчиком 21.09, решение 5: находка годная, но это бизнес-сигнал.
  { value: "wrong_block", label: "Не тот блок — бизнес-сигнал, а не технология" },
  { value: "merge_duplicate", label: "Дубль — тот же сигнал или технологический кластер" },
  // 03.10: находка годная, но балл выше, чем она стоит.
  { value: "overrated", label: "Оценка завышена — сигнал годный, балл слишком высокий" },
];

// «Не тот блок» у бизнес-карточки, наоборот: технология, а не бизнес. Остальные пункты у всех карточек одни.
function verdictOptions(signal: Signal): typeof VERDICT_LABELS {
  return signal.signal_category === "business"
    ? VERDICT_LABELS.map((item) =>
      item.value === "wrong_block" ? { ...item, label: "Не тот блок — технологический сигнал, а не бизнес" } : item)
    : VERDICT_LABELS;
}

// Оценка человека в списке блоков, коротко: первая часть подписи шкалы («Сильный сигнал»).
export function verdictName(verdict: string | null): string {
  const found = VERDICT_LABELS.find((item) => item.value === verdict);
  return found ? found.label.split(" — ")[0] : verdict || "";
}

// Номер главной карточки, вписанный рукой: «9002», «#9002», «№ 9002». Слова заголовка номером не считаются.
const NUMBER_ONLY = /^\s*[#№]?\s*(\d{1,18})\s*$/;

// Номер главной карточки «Дубля» из поля формы: число; null: поле пусто; "words": вписаны слова, а главная не выбрана.
export function duplicateMainId(draft: FeedbackDraft): number | null | "words" {
  const text = draft.duplicateText.trim();
  if (!text) return null;
  const typed = NUMBER_ONLY.exec(text);
  if (typed) return Number(typed[1]);
  return draft.duplicatePick ? draft.duplicatePick.id : "words";
}

// Подсказки главной: видимые карточки обеих витрин по словам заголовка (поиск списка сигналов: номер и слова), не
// раньше трёх знаков и не на каждую букву.
const HINT_MIN_CHARS = 3;
const HINT_DELAY_MS = 300;
const HINTS_PER_SHOWCASE = 6;
const SHOWCASE_NAMES = { technology: "технологический радар", business: "бизнес-лента" } as const;

type Hint = DuplicateChoice & { showcase: keyof typeof SHOWCASE_NAMES };

function DuplicateField({ signal, draft, onChange }: {
  signal: Signal;
  draft: FeedbackDraft;
  onChange: (patch: Partial<FeedbackDraft>) => void;
}) {
  const labelId = useId();
  const pickedId = useId();
  const hintsId = useId();
  const [hints, setHints] = useState<Hint[]>([]);
  const words = draft.duplicateText.trim();
  const searchable = !draft.duplicatePick && words.length >= HINT_MIN_CHARS && !NUMBER_ONLY.test(words);

  useEffect(() => {
    if (!searchable) {
      setHints([]);
      return undefined;
    }
    let stale = false;
    const timer = window.setTimeout(() => {
      const query = { q: words, limit: HINTS_PER_SHOWCASE, evidenceLimit: 0 };
      Promise.all([listSignals(query), listSignals({ ...query, showcase: "business" })])
        .then(([technology, business]) => {
          if (stale) return;
          const seen = new Set<number>([signal.id]);
          const found: Hint[] = [];
          for (const [showcase, rows] of [["technology", technology], ["business", business]] as const) {
            for (const row of rows) {
              if (seen.has(row.id)) continue;
              seen.add(row.id);
              found.push({ id: row.id, title: row.title_ru || row.title, showcase });
            }
          }
          setHints(found);
        })
        // Подсказки не пришли: номер можно вписать и без них.
        .catch(() => !stale && setHints([]));
    }, HINT_DELAY_MS);
    return () => {
      stale = true;
      window.clearTimeout(timer);
    };
  }, [searchable, words, signal.id]);

  return (
    // Подписи и подсказки рядом с полем, а не внутри <label>: кнопки подсказок внутри подписи перехватывали бы фокус.
    <div className="field signalFeedbackDuplicate">
      <span id={labelId}>ID дубля</span>
      <input
        aria-labelledby={labelId}
        value={draft.duplicateText}
        aria-describedby={draft.duplicatePick ? pickedId : undefined}
        aria-controls={hints.length ? hintsId : undefined}
        onChange={(event) => onChange({ duplicateText: event.target.value, duplicatePick: null })}
        placeholder="номер главной карточки или слова её заголовка"
      />
      {draft.duplicatePick ? (
        <small className="metaText" id={pickedId}>
          {`Главная карточка: №${draft.duplicatePick.id} «${draft.duplicatePick.title}»`}
        </small>
      ) : null}
      {hints.length ? (
        <ul className="signalDuplicateHints" id={hintsId} aria-label="Подсказки главной карточки">
          {hints.map((hint) => (
            <li key={hint.id}>
              <button
                type="button"
                className="ghostButton compactButton"
                onClick={() => onChange({ duplicateText: String(hint.id), duplicatePick: { id: hint.id, title: hint.title } })}
              >
                {`№${hint.id} · ${hint.title} · ${SHOWCASE_NAMES[hint.showcase]}`}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

type Props = {
  signal: Signal;
  draft: FeedbackDraft;
  saving: boolean;
  onChange: (patch: Partial<FeedbackDraft>) => void;
  onCancel: () => void;
  onSubmit: () => void;
};

// Форма ОС (документ заказчика 19.09: «сделать поприятнее оформление»): на десктопе две колонки, пары полей одной
// высоты, подписи как в остальных формах, «Отмена» и «Сохранить» справа.
export function SignalFeedbackForm({ signal, draft, saving, onChange, onCancel, onSubmit }: Props) {
  return (
    <div className="signalFeedbackBox">
      <div className="signalFeedbackGrid">
        <label className="field">
          <span>Оценка сигнала</span>
          <select
            value={draft.verdict}
            onChange={(event) => onChange({ verdict: event.target.value as FeedbackDraft["verdict"] })}
          >
            {verdictOptions(signal).map((item) => (
              <option value={item.value} key={item.value || "empty"}>{item.label}</option>
            ))}
          </select>
        </label>
        <DuplicateField signal={signal} draft={draft} onChange={onChange} />
        <label className="field">
          <span>Обоснование оценки</span>
          <input
            value={draft.reason}
            onChange={(event) => onChange({ reason: event.target.value })}
            placeholder="чем обоснована оценка"
          />
        </label>
        <label className="field">
          <span>Рекомендуемый заголовок</span>
          <input
            value={draft.correctedTitle}
            onChange={(event) => onChange({ correctedTitle: event.target.value })}
            placeholder="если нужно переименовать карточку"
          />
        </label>
        <label className="field">
          <span>Рекомендуемая формулировка сути</span>
          <textarea
            value={draft.correctedThesis}
            onChange={(event) => onChange({ correctedThesis: event.target.value })}
            placeholder="эталонная формулировка сути сигнала"
          />
        </label>
        <label className="field">
          <span>Рекомендации AI-агенту</span>
          <textarea
            value={draft.comment}
            onChange={(event) => onChange({ comment: event.target.value })}
            placeholder="термины, поисковый угол, сильный источник..."
          />
        </label>
      </div>
      <div className="signalFeedbackActions">
        <span className="metaText">Обратная связь: {signal.feedback_count || 0}</span>
        <div className="signalFeedbackButtons">
          <button type="button" className="ghostButton" disabled={saving} onClick={onCancel}>
            Отмена
          </button>
          <button type="button" className="primaryButton" disabled={saving} onClick={onSubmit}>
            Сохранить
          </button>
        </div>
      </div>
    </div>
  );
}
