import { useId } from "react";
import type { SignalDictionaries } from "../../api/signals";
import { CHOICE_FIELDS, UNDEFINED } from "./SignalCardChips";

// Фильтры атрибутов карточки (#97) — сверху на обоих экранах: значения — из справочников агента
// «Карточка сигнала» (/api/signals/dictionaries), «не определено» — тоже значение (так видны пробелы).
// Компания — под любым из её имён: алиасы сводит сервер.

export type CardFilterValues = {
  eventType: string;
  region: string;
  technologyClass: string;
  technologyMaturity: string;
  company: string;
};

export const EMPTY_CARD_FILTERS: CardFilterValues = {
  eventType: "",
  region: "",
  technologyClass: "",
  technologyMaturity: "",
  company: "",
};

type Props = {
  // Справочники не пришли — нет и списков: фильтр по компании работает и без них.
  dictionaries: SignalDictionaries | null;
  values: CardFilterValues;
  onChange: (patch: Partial<CardFilterValues>) => void;
};

export function SignalCardFilters({ dictionaries, values, onChange }: Props) {
  const companiesId = useId();
  return (
    <div className="signalCardFilters">
      {CHOICE_FIELDS.map(({ field, filter, label, any }) => {
        const options = dictionaries?.[field];
        if (!Array.isArray(options)) return null;
        return (
          <label className="field" key={field}>
            <span>{label}</span>
            <select value={values[filter]} onChange={(event) => onChange({ [filter]: event.target.value })}>
              <option value="">{any}</option>
              {options.map((value) => <option value={value} key={value}>{value}</option>)}
              <option value={UNDEFINED}>{UNDEFINED}</option>
            </select>
          </label>
        );
      })}
      <label className="field">
        <span>Компания</span>
        <input
          list={companiesId}
          value={values.company}
          onChange={(event) => onChange({ company: event.target.value })}
          placeholder="Все компании"
          // Не длиннее, чем принимает API (company ≤ 200).
          maxLength={200}
        />
        <datalist id={companiesId}>
          {(dictionaries?.companies ?? []).map((name) => <option value={name} key={name} />)}
        </datalist>
      </label>
    </div>
  );
}
