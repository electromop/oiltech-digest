import { useId } from "react";
import type { SignalDictionaries } from "../../api/signals";

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

const CHOICES = [
  { key: "eventType", dictionary: "event_type", any: "Любой тип" },
  { key: "region", dictionary: "region", any: "Любой регион" },
  { key: "technologyClass", dictionary: "technology_class", any: "Любой класс" },
  { key: "technologyMaturity", dictionary: "technology_maturity", any: "Любая зрелость" },
] as const;

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
      {CHOICES.map(({ key, dictionary, any }) => {
        const block = dictionaries?.[dictionary];
        if (!dictionaries || !block?.values) return null;
        return (
          <label className="field" key={key}>
            <span>{block.title}</span>
            <select value={values[key]} onChange={(event) => onChange({ [key]: event.target.value })}>
              <option value="">{any}</option>
              {block.values.map((value) => <option value={value} key={value}>{value}</option>)}
              <option value={dictionaries.undefined}>{dictionaries.undefined}</option>
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
