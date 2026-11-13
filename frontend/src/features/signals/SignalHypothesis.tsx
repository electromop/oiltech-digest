import { useState } from "react";
import type { Signal } from "../../api/types";

type Props = {
  signal: Signal;
  saving: boolean;
  // Правит гипотезу только администратор (#188): у участника она видна без правки.
  editable: boolean;
  onSave: (text: string) => void;
};

// Кто поставил гипотезу и когда (#162): агент (#163) или человек его почтой; день по Москве, как «Поступил».
export function hypothesisAuthorship(setBy: string | null | undefined, author: string | null | undefined,
                                     setAt: string | null | undefined): string {
  const date = setAt ? new Date(setAt) : null;
  const day = date && !Number.isNaN(date.getTime())
    ? date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "Europe/Moscow" })
    : "";
  const who = setBy === "agent" ? "агент" : setBy === "human" ? `человек${author ? ` (${author})` : ""}` : "";
  return [who, day].filter(Boolean).join(", ");
}

// «Гипотеза для Компании» на карточке сигнала, сразу после «Применения» (#162): та же, что в выпуске, с правкой. Блок
// есть у сигнала с гипотезой и у отмеченного «в дайджест» (агент пишет черновик только им). В закрытом месяце правки
// нет: выпуск того месяца только на просмотр (сервер отвечает 409); у участника без прав администратора тоже (#188).
export function SignalHypothesis({ signal, saving, editable, onSave }: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const text = signal.hypothesis ?? "";
  if (!text && !signal.selected_for_digest) return null;
  const locked = Boolean(signal.digest_locked);
  const authorship = hypothesisAuthorship(signal.card_set_by?.hypothesis, signal.hypothesis_author, signal.hypothesis_set_at);
  return (
    <div className="signalHypothesis">
      <span>
        Гипотеза для Компании{authorship ? <span className="metaText"> · {authorship}</span> : null}
      </span>
      {editing ? (
        <>
          <textarea
            aria-label="Гипотеза для Компании"
            value={draft}
            maxLength={1000}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Одно предложение: «Оценить…», «Рассмотреть…» или «Изучить…»"
          />
          <div className="signalFeedbackButtons">
            <button type="button" className="ghostButton compactButton" disabled={saving} onClick={() => setEditing(false)}>
              Отмена
            </button>
            <button
              type="button"
              className="primaryButton compactButton"
              disabled={saving}
              onClick={() => {
                onSave(draft.trim());
                setEditing(false);
              }}
            >
              Сохранить
            </button>
          </div>
        </>
      ) : (
        <>
          <p>{text || "Гипотезы пока нет."}</p>
          {locked || !editable ? null : (
            <button
              type="button"
              className="ghostButton compactButton"
              onClick={() => {
                setDraft(text);
                setEditing(true);
              }}
            >
              {text ? "Править гипотезу" : "Написать гипотезу"}
            </button>
          )}
        </>
      )}
    </div>
  );
}
