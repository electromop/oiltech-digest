import { useId } from "react";
import type { SignalDictionaries, SignalFeedbackFilter } from "../../api/signals";
import { CHOICE_FIELDS, COUNTRY_FILTER, UNDEFINED } from "./SignalCardChips";

// Фильтры панели по тому, что показывает карточка, на обоих экранах. Атрибуты карточки (#97): значения — из
// справочников агента «Карточка сигнала» (/api/signals/dictionaries), «не определено» — тоже значение (так видны
// пробелы). Компания — под любым из её имён: алиасы сводит сервер. Страна (#137) — имя справочника стран: сервер ищет её
// и в странах события, и у участников; «не определено» у неё нет — пробел виден чипом на карточке.
// Дата поступления (задача владельца 03.11): то же, что «Поступил» в карточке, день первой находки по Москве (у
// статьи ленты день сбора, у материала день внесения); «от» и «до» включительно, ГГГГ-ММ-ДД, одной ячейкой.

export type CardFilterValues = {
  eventType: string;
  region: string;
  technologyClass: string;
  technologyMaturity: string;
  country: string;
  company: string;
  arrivalFrom: string;
  arrivalTo: string;
  feedback: SignalFeedbackFilter | "";
};

export const EMPTY_CARD_FILTERS: CardFilterValues = {
  eventType: "",
  region: "",
  technologyClass: "",
  technologyMaturity: "",
  country: "",
  company: "",
  arrivalFrom: "",
  arrivalTo: "",
  feedback: "",
};

export const ARRIVAL_FILTER_LABEL = "Дата поступления";

// Наличие ОС (решение владельца 03.11): записи «Оценок людей» в карточке; по тому же правилу сервер считает плашку
// «С обратной связью» и счётчик «Обратная связь: N». Чип показывает сам выбор: «Есть ОС», «Нет ОС».
export const FEEDBACK_FILTER = {
  label: "Наличие ОС",
  any: "Любая",
  choices: { with: "Есть ОС", without: "Нет ОС" } as Record<SignalFeedbackFilter, string>,
};

// Период чипа «Включённые фильтры»: «от 01.10.2026 до 15.10.2026», без одной из границ только другая.
export function arrivalPeriod({ arrivalFrom, arrivalTo }: Pick<CardFilterValues, "arrivalFrom" | "arrivalTo">): string {
  return [arrivalFrom && `от ${dayText(arrivalFrom)}`, arrivalTo && `до ${dayText(arrivalTo)}`].filter(Boolean).join(" ");
}

// ГГГГ-ММ-ДД поля даты → ДД.ММ.ГГГГ, как дата «Поступил» в карточке; строкой, без часового пояса.
function dayText(value: string) {
  return value.split("-").reverse().join(".");
}

type Props = {
  // Справочники не пришли — нет и списков: фильтр по компании работает и без них.
  dictionaries: SignalDictionaries | null;
  values: CardFilterValues;
  onChange: (patch: Partial<CardFilterValues>) => void;
};

export function SignalCardFilters({ dictionaries, values, onChange }: Props) {
  const companiesId = useId();
  const arrivalId = useId();
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
      {Array.isArray(dictionaries?.country) ? (
        <label className="field">
          <span>{COUNTRY_FILTER.label}</span>
          <select value={values.country} onChange={(event) => onChange({ country: event.target.value })}>
            <option value="">{COUNTRY_FILTER.any}</option>
            {dictionaries.country.map((value) => <option value={value} key={value}>{value}</option>)}
          </select>
        </label>
      ) : null}
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
      {/* Границы держат друг друга: «от» позже «до» в календаре не выбрать. */}
      <div className="field signalArrivalField" role="group" aria-labelledby={arrivalId}>
        <span id={arrivalId}>{ARRIVAL_FILTER_LABEL}</span>
        <div className="signalArrivalRange">
          <label>
            <span>от</span>
            <input
              type="date"
              value={values.arrivalFrom}
              max={values.arrivalTo || undefined}
              onChange={(event) => onChange({ arrivalFrom: event.target.value })}
            />
          </label>
          <label>
            <span>до</span>
            <input
              type="date"
              value={values.arrivalTo}
              min={values.arrivalFrom || undefined}
              onChange={(event) => onChange({ arrivalTo: event.target.value })}
            />
          </label>
        </div>
      </div>
      <label className="field">
        <span>{FEEDBACK_FILTER.label}</span>
        <select
          value={values.feedback}
          onChange={(event) => onChange({ feedback: event.target.value as CardFilterValues["feedback"] })}
        >
          <option value="">{FEEDBACK_FILTER.any}</option>
          {Object.entries(FEEDBACK_FILTER.choices).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
        </select>
      </label>
    </div>
  );
}

// Подсказка у поля «Компания» (решение владельца 20.10): фильтр «всё про компанию» показывает и дочерние
// общества компании по списку «дочка → мать» (экран «Дочерние общества»). Кого он добавил, говорит сервер
// (company_group в сводке: сама компания первой, затем дочки); дочек нет, подсказки нет.
type CompanyGroupHintProps = {
  // Компания в поле сейчас: сводка могла прийти ещё по прежней, её подсказку не показываем.
  company: string;
  group: string[] | undefined;
};

export function CompanyGroupHint({ company, group }: CompanyGroupHintProps) {
  if (!group || group.length < 2 || group[0] !== spaced(company)) return null;
  const [name, ...daughters] = group;
  return <p className="panelHint">{`${name} и дочерние: ${daughters.join(", ")}`}</p>;
}

// Пробелы как у сервера в company_group: по краям нет, внутри по одному.
function spaced(text: string) {
  return text.split(/\s+/).filter(Boolean).join(" ");
}
