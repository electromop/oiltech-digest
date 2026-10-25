import { Fragment } from "react";
import type { Signal } from "../../api/types";

// Атрибуты карточки сигнала (#97, спека #93) на обоих экранах: бизнес-ленте и радаре. С пересмотра 23.10 — строкой
// под заголовком (тема, компании, регион, дата события) и таблицей в раскрытой карточке, а не рядом чипов.
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

function companiesOf(signal: Signal): string[] {
  return (signal.participants_json || []).map((item) => item.name).filter(Boolean);
}

function CompanyButtons({ names, onCompany }: { names: string[]; onCompany: (name: string) => void }) {
  return (
    <>
      {names.map((name, index) => (
        <Fragment key={name}>
          {index ? ", " : ""}
          <button type="button" className="signalCompanyButton" title={`Все сигналы с участием ${name}`} onClick={() => onCompany(name)}>
            {name}
          </button>
        </Fragment>
      ))}
    </>
  );
}

// Под заголовком свёрнутой карточки (пересмотр 23-25.10: «много флагов и плашек — непонятно, о чём речь»): тема
// строкой, ниже — кто, где и когда с подписями и без точек-разделителей: «SLB  Страна: …  Дата события: …».
// Пробелы и служебное — в раскрытой карточке. «Страна» — поле региона карточки (решение владельца 25.10).
export function SignalCardLine({ signal, onCompany }: Props) {
  const companies = companiesOf(signal);
  const region = signal.region && signal.region !== UNDEFINED ? signal.region : "";
  const day = formatEventDate(signal.event_date);
  return (
    <div className="signalCardLine">
      {signal.theme ? <div className="signalCardTheme">{signal.theme}</div> : null}
      {companies.length || region || day ? (
        <div className="signalCardFacts">
          {companies.length ? (
            <span className="signalCardLineCompanies">
              <CompanyButtons names={companies.slice(0, 3)} onCompany={onCompany} />
              {companies.length > 3 ? ` и ещё ${companies.length - 3}` : ""}
            </span>
          ) : null}
          {region ? (
            <span>
              <span className="signalCardLabel">Страна:</span> {region}
            </span>
          ) : null}
          {day ? (
            <span>
              <span className="signalCardLabel">Дата события:</span> {day}
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

// wide — значение во всю строку таблицы (длинная причина отклонения).
type Extra = { key: string; label: string; value: string; wide?: boolean };

// Атрибуты раскрытой карточки — таблица «подпись: значение» вместо ряда чипов. «Не определено» — видимый
// пробел карточки (пунктир), а не пропуск.
export function SignalAttributes({ signal, onCompany, extra = [] }: Props & { extra?: Extra[] }) {
  const companies = companiesOf(signal);
  const companiesGap = !companies.length && isGap(signal, "participants", signal.participants_json);
  const date = eventDateLabel(signal);
  return (
    <dl className="signalAttributes">
      {cardChips(signal).map((chip) => (
        <div key={chip.key} className={chip.gap ? "signalAttribute gap" : "signalAttribute"} title={chip.gap ? GAP_HINT : undefined}>
          <dt>{chip.label}</dt>
          <dd>{chip.value}</dd>
        </div>
      ))}
      {companies.length || companiesGap ? (
        <div className={companiesGap ? "signalAttribute gap" : "signalAttribute"} title={companiesGap ? GAP_HINT : undefined}>
          <dt>Компании</dt>
          <dd>{companiesGap ? UNDEFINED : <CompanyButtons names={companies} onCompany={onCompany} />}</dd>
        </div>
      ) : null}
      {date ? (
        <div className={date.gap ? "signalAttribute gap" : "signalAttribute"}>
          <dt>Событие</dt>
          <dd>{date.text.replace(/^Событие: /, "")}</dd>
        </div>
      ) : null}
      {extra.map((item) => (
        <div key={item.key} className={item.wide ? "signalAttribute wide" : "signalAttribute"}>
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}
