import { Fragment } from "react";
import type { Signal } from "../../api/types";

// Атрибуты карточки сигнала (#97, спека #93) — чипами на обоих экранах: бизнес-ленте и радаре.
// Значения ставит агент «Карточка сигнала» (#96) из справочников; «не определено» — его ответ «по тексту
// понять нельзя». Это пробел карточки, и его видно, а не прячется.

export const UNDEFINED = "не определено";
const GAP_HINT = "Не определено: по тексту публикации значение понять нельзя";
const NOT_FILLED = "Карточка ещё не заполнена";

// Справочные поля карточки: колонка сигнала, параметр фильтра экрана и подписи — одни для чипов и
// фильтров (SignalCardFilters), чтобы подпись поля на экране была одна.
export const CHOICE_FIELDS = [
  { field: "event_type", filter: "eventType", label: "Тип события", any: "Любой тип" },
  { field: "region", filter: "region", label: "Регион", any: "Любой регион" },
  { field: "technology_class", filter: "technologyClass", label: "Класс технологии", any: "Любой класс" },
  { field: "technology_maturity", filter: "technologyMaturity", label: "Зрелость", any: "Любая зрелость" },
] as const;

const CATEGORY_LABELS: Record<string, string> = { business: "бизнес", technology: "технология", other: "другое" };
const RELEVANCE_LABELS: Record<string, string> = { direct: "прямая", transferable: "переносимая", none: "нет связи" };
// Кто поставил дату события, если у неё нет источника агента («из текста», «по дате публикации»).
const DATE_SET_BY_LABELS: Record<string, string> = { radar_judge: "по оценке судьи", human: "правка человека" };

type Chip = { key: string; label: string; value: string; gap: boolean };

// Пробел карточки — поле заполняли (агент или человек: card_set_by), а значения нет. Поле, которое ещё
// никто не заполнял, — не пробел: «не определено» сказало бы, что агент не смог, а он не начинал.
function isGap(signal: Signal, key: string, value: unknown): boolean {
  if (value === UNDEFINED) return true;
  const empty = value == null || value === "" || (Array.isArray(value) && value.length === 0);
  return empty && Boolean(signal.card_set_by?.[key]);
}

function cardChips(signal: Signal): Chip[] {
  // Категория — признак сигнала, она есть всегда; без неё — находка радара до 29.09, радар считает её технологией.
  const category = signal.signal_category || "technology";
  const chips: Chip[] = [{ key: "category", label: "Категория", value: CATEGORY_LABELS[category] || category, gap: false }];
  for (const { field, label } of CHOICE_FIELDS) {
    const value = signal[field];
    if (isGap(signal, field, value)) chips.push({ key: field, label, value: UNDEFINED, gap: true });
    else if (value) chips.push({ key: field, label, value, gap: false });
  }
  // «Не определено» у связи с нефтесервисом — пусто при пометке агента (код радара: direct / transferable / none).
  const relevance = signal.oilfield_relevance;
  const label = "Связь с нефтесервисом";
  if (isGap(signal, "oilfield_relevance", relevance)) chips.push({ key: "relevance", label, value: UNDEFINED, gap: true });
  else if (relevance) chips.push({ key: "relevance", label, value: RELEVANCE_LABELS[relevance] || relevance, gap: false });
  return chips;
}

function formatEventDate(value: string | null | undefined): string {
  // Дата без часового пояса (ГГГГ-ММ-ДД): строкой, без Date — в Date она сдвинулась бы на сутки.
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || "");
  return match ? `${match[3]}.${match[2]}.${match[1]}` : "";
}

// Дата события и откуда она взята: у агента (#96) — «из текста» или «по дате публикации», у судьи радара
// и человека — кто её поставил. Дата без пометки — карточка до #96 («нашёл очень старый сигнал», 29.09).
export function eventDateLabel(signal: Signal): { text: string; gap: boolean } | null {
  const day = formatEventDate(signal.event_date);
  const setBy = signal.card_set_by?.event_date ?? "";
  const source = signal.event_date_source;
  if (day) {
    const origin = DATE_SET_BY_LABELS[setBy] || (source && source !== UNDEFINED ? source : "");
    return { text: origin ? `Событие: ${day} · ${origin}` : `Событие: ${day}`, gap: false };
  }
  return isGap(signal, "event_date", source === UNDEFINED ? UNDEFINED : null) ? { text: `Событие: ${UNDEFINED}`, gap: true } : null;
}

// «Почему важно нефтесервису» агент пишет в прежнее поле судьи «почему интересно» (#96): подпись — по тому,
// кто текст написал. Агент или человек — «почему важно»; ревью пачки радара — «почему интересно» с его
// баллом. Пустое поле лента показывает пробелом (строка карточки), радар — не показывает, как раньше.
export function whyBlock(signal: Signal, business: boolean): { label: string; text: string } | null {
  const setBy = signal.card_set_by?.why_interesting;
  const byCard = setBy === "agent" || setBy === "human";
  if (signal.why_interesting && !byCard) {
    const score = signal.interest_score != null ? ` · ${Math.round(Number(signal.interest_score))}` : "";
    return { label: `Почему интересно${score}`, text: signal.why_interesting };
  }
  if (signal.why_interesting) return { label: "Почему важно", text: signal.why_interesting };
  if (!business) return null;
  return { label: "Почему важно", text: isGap(signal, "why_interesting", null) ? UNDEFINED : NOT_FILLED };
}

type Props = {
  signal: Signal;
  // Клик по компании — «всё про компанию»: фильтр по ней в этой витрине.
  onCompany: (name: string) => void;
};

export function SignalCardChips({ signal, onCompany }: Props) {
  const companies = (signal.participants_json || []).map((item) => item.name).filter(Boolean);
  return (
    <div className="signalCardChips" role="group" aria-label="Атрибуты сигнала">
      {cardChips(signal).map((chip) => (
        <span key={chip.key} className={chip.gap ? "signalChip gap" : "signalChip"} title={chip.gap ? GAP_HINT : undefined}>
          <span className="signalChipLabel">{chip.label}:</span> {chip.value}
        </span>
      ))}
      {companies.length ? (
        <span className="signalChip signalChipCompanies">
          <span className="signalChipLabel">Компании:</span>
          {companies.map((name) => (
            <Fragment key={name}>
              {" "}
              <button
                type="button"
                className="signalCompanyButton"
                title={`Все сигналы с участием ${name}`}
                onClick={() => onCompany(name)}
              >
                {name}
              </button>
            </Fragment>
          ))}
        </span>
      ) : isGap(signal, "participants", signal.participants_json) ? (
        <span className="signalChip gap" title={GAP_HINT}>
          <span className="signalChipLabel">Компании:</span> {UNDEFINED}
        </span>
      ) : null}
    </div>
  );
}
