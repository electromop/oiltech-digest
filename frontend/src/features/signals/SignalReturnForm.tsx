import { useState } from "react";
import type { SignalChoice, SignalReturnChoices, SignalReturnPayload } from "../../api/signals";
import type { Signal } from "../../api/types";

// Возврат отклонённого (#132, спека #128): человек вписывает то, что ставит «Отбор», — отрасль, связь с
// нефтесервисом и применение, категорию — и публикация идёт по остатку конвейера с пометкой «спорное: „Отбор“ не
// взял бы». Коды и подписи — справочника «Отбора» с сервера (/api/signals/dictionaries): экран их не держит. Связь
// сервер даёт только прямую и переносимую (вернуть — значит взять); невозможные пары отрасли и связи он отклонит.

type Props = {
  signal: Signal;
  choices: SignalReturnChoices;
  saving: boolean;
  onSubmit: (payload: SignalReturnPayload) => void;
  onCancel: () => void;
};

// Начальное значение — что поставил «Отбор», если оно есть в списке; иначе первое.
function initial(options: SignalChoice[], value: string | null | undefined): string {
  return options.some((item) => item.code === value) ? (value as string) : options[0]?.code ?? "";
}

export function SignalReturnForm({ signal, choices, saving, onSubmit, onCancel }: Props) {
  const [industry, setIndustry] = useState(initial(choices.industry, signal.industry));
  const [relevance, setRelevance] = useState(initial(choices.oilfield_relevance, signal.oilfield_relevance));
  const [application, setApplication] = useState(signal.oilfield_application || "");
  const [category, setCategory] = useState(initial(choices.category, signal.signal_category));
  // Применение нужно переносимой связи: так проверяет и сервер (таблица конфликтов «Отбора»).
  const needsApplication = relevance === "transferable" && !application.trim();

  function options(list: SignalChoice[]) {
    return list.map((item) => <option key={item.code} value={item.code}>{item.label}</option>);
  }

  return (
    // Та же разметка, что у формы ОС на карточке: две колонки на десктопе, «Отмена» и «Вернуть» справа.
    <form
      className="signalFeedbackBox"
      aria-label="Вернуть публикацию"
      onSubmit={(event) => {
        event.preventDefault();
        if (needsApplication) return;
        onSubmit({
          industry,
          oilfield_relevance: relevance,
          oilfield_application: relevance === "transferable" ? application.trim() : "",
          signal_category: category,
        });
      }}
    >
      <p className="metaText">
        Вернуть значит взять: впишите то, что ставит «Отбор». Публикация пройдёт суть, карточку и баллы с пометкой
        «спорное».
      </p>
      <div className="signalFeedbackGrid">
        <label className="field">
          <span>Отрасль</span>
          <select value={industry} onChange={(event) => setIndustry(event.target.value)}>{options(choices.industry)}</select>
        </label>
        <label className="field">
          <span>Связь с нефтесервисом</span>
          <select value={relevance} onChange={(event) => setRelevance(event.target.value)}>
            {options(choices.oilfield_relevance)}
          </select>
        </label>
        {relevance === "transferable" ? (
          <label className="field">
            <span>Применение в нефтесервисе</span>
            <input
              value={application}
              onChange={(event) => setApplication(event.target.value)}
              placeholder="какую задачу нефтесервиса решит, что доработать"
            />
          </label>
        ) : null}
        <label className="field">
          <span>Категория</span>
          <select value={category} onChange={(event) => setCategory(event.target.value)}>{options(choices.category)}</select>
        </label>
      </div>
      <div className="signalFeedbackActions">
        <span className="metaText">{needsApplication ? "У переносимой связи нужно применение" : ""}</span>
        <div className="signalFeedbackButtons">
          <button type="button" className="ghostButton" disabled={saving} onClick={onCancel}>
            Отмена
          </button>
          <button type="submit" className="primaryButton" disabled={saving || needsApplication}>
            Вернуть
          </button>
        </div>
      </div>
    </form>
  );
}
