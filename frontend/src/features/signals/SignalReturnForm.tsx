import { useState } from "react";
import type { SignalReturnPayload } from "../../api/signals";
import type { Signal } from "../../api/types";

// Возврат отклонённого (#132, спека #128): человек вписывает то, что ставит «Отбор», — отрасль, связь с
// нефтесервисом и применение, категорию — и публикация идёт по остатку конвейера с пометкой «спорное: „Отбор“ не
// взял бы». Вернуть — значит взять: связь прямая или переносимая, у переносимой — применение. Коды — справочника
// «Отбора»; невозможные пары (переносимая у нефтесервиса, прямая у другой отрасли) отклонит сервер.

const INDUSTRIES = [
  { value: "oilfield_services", label: "Нефтесервис" },
  { value: "oil_gas", label: "Нефтегаз" },
  { value: "other_industry", label: "Другая отрасль" },
];
const RELEVANCE = [
  { value: "direct", label: "Прямая" },
  { value: "transferable", label: "Переносимая" },
];
const CATEGORIES = [
  { value: "business", label: "Бизнес" },
  { value: "technology", label: "Технология" },
  { value: "other", label: "Другое" },
];

type Props = {
  signal: Signal;
  saving: boolean;
  onSubmit: (payload: SignalReturnPayload) => void;
  onCancel: () => void;
};

export function SignalReturnForm({ signal, saving, onSubmit, onCancel }: Props) {
  // Начальные значения — что поставил «Отбор»; связи у отклонённого чаще нет, а вернуть без связи нельзя.
  const [industry, setIndustry] = useState(signal.industry || "oil_gas");
  const [relevance, setRelevance] = useState(signal.oilfield_relevance === "transferable" ? "transferable" : "direct");
  const [application, setApplication] = useState(signal.oilfield_application || "");
  const [category, setCategory] = useState(signal.signal_category || "business");
  const needsApplication = relevance === "transferable" && !application.trim();

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
          <select value={industry} onChange={(event) => setIndustry(event.target.value)}>
            {INDUSTRIES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </label>
        <label className="field">
          <span>Связь с нефтесервисом</span>
          <select value={relevance} onChange={(event) => setRelevance(event.target.value)}>
            {RELEVANCE.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
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
          <select value={category} onChange={(event) => setCategory(event.target.value)}>
            {CATEGORIES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
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
