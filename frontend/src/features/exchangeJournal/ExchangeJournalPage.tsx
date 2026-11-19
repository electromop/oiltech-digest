import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError } from "../../api/client";
import {
  buildAddition,
  getExchangeJournal,
  type AdditionResult,
  type ExchangeCard,
  type ExchangeJournal,
  type ExchangePackage,
} from "../../api/exchangeJournal";
import { formatMoment } from "../feedbackJournal/JournalEntryCard";
import { SkeletonList } from "../shared/Skeleton";

// Вкладка «Журнал обмена» (#198, спека #197): пакеты для потребителя выпуска, их состав и состояние (собран, забран) и
// дозагрузка по номерам. Только администратору: пункта меню у участника нет, по прямой ссылке сервер отвечает 403.

type Props = {
  onUnauthorized: () => void;
  showToast: (text: string, tone?: "default" | "error") => void;
};

const KIND_LABELS: Record<string, string> = { release: "Выпуск", addition: "Дозагрузка" };
const MONTH_NAMES = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
  "ноябрь", "декабрь"];
const MONTH_PATTERN = /^\d{4}-(0[1-9]|1[0-2])$/;

// «2026-09» → «сентябрь 2026».
export function monthLabel(month: string | null | undefined): string {
  if (!month || !MONTH_PATTERN.test(month)) return month ?? "";
  return `${MONTH_NAMES[Number(month.slice(5, 7)) - 1]} ${month.slice(0, 4)}`;
}

// Карточка в витрине: бизнес на бизнес-ленте, остальное на технологическом радаре; витрина ищет её по номеру.
export function cardHref(card: ExchangeCard): string {
  const screen = card.category === "business" ? "business-feed" : "signal-radar";
  return `?screen=${screen}&q=${encodeURIComponent(`#${card.signal_id}`)}`;
}

// Номера через запятую, точку с запятой или пробел; не число: null.
function parseNumbers(text: string): number[] | null {
  const parts = text.split(/[\s,;]+/).filter(Boolean);
  if (!parts.every((part) => /^\d{1,18}$/.test(part))) return null;
  return parts.map(Number).filter((one) => one > 0);
}

function packageTitle(item: ExchangePackage): string {
  const kind = KIND_LABELS[item.kind] ?? item.kind;
  return item.month ? `${kind} №${item.number} · ${monthLabel(item.month)}` : `${kind} №${item.number}`;
}

// Что сборка сделала сверх состава, строками для журнала.
function buildNotes(item: ExchangePackage): string[] {
  const details = item.details ?? {};
  return [
    ...(details.moved ?? []).map((one) => `Дубль №${one.requested} ушёл корнем склейки №${one.root}`),
    ...(details.skipped ?? []).map((one) => `№${one.signal_id} уже отправлен: пакет №${one.package}`),
    ...(details.without_signal?.length
      ? [`Статьи выпуска без сигнала ленты не ушли: №${details.without_signal.join(", №")}`] : []),
  ];
}

function PackageEntry({ item }: { item: ExchangePackage }) {
  const taken = Boolean(item.first_taken_at);
  const notes = buildNotes(item);
  return (
    <article className="journalEntry" aria-label={packageTitle(item)}>
      <header className="journalEntryHead">
        <span className="journalKind">{packageTitle(item)}</span>
        <span className={`miniPill ${taken ? "ok" : "warn"}`}>{taken ? "забран" : "собран"}</span>
        <span className="metaText">Собран {formatMoment(item.built_at)}{item.built_by ? ` · ${item.built_by}` : ""}</span>
        <span className="metaText">
          {taken
            ? `Забран ${formatMoment(item.first_taken_at)}, последний раз ${formatMoment(item.last_taken_at)}`
            : "Потребитель ещё не забирал"}
        </span>
      </header>
      <div className="journalEntryBody">
        <section aria-label={`Состав пакета №${item.number}`}>
          <p className="journalLabel">Карточек: {item.cards.length}</p>
          {item.cards.length ? (
            <ul className="journalLines">
              {item.cards.map((card) => (
                <li key={card.signal_id}>
                  <a href={cardHref(card)}>№{card.signal_id}{card.title ? ` ${card.title}` : ""}</a>
                </li>
              ))}
            </ul>
          ) : (
            <p className="metaText">Пустой выпуск: в месяце не было отметок «в дайджест».</p>
          )}
        </section>
        {notes.length ? (
          <section aria-label={`Сборка пакета №${item.number}`}>
            <p className="journalLabel">Сборка</p>
            <ul className="journalLines">{notes.map((note) => <li key={note}>{note}</li>)}</ul>
          </section>
        ) : null}
      </div>
    </article>
  );
}

export function ExchangeJournalPage({ onUnauthorized, showToast }: Props) {
  const [journal, setJournal] = useState<ExchangeJournal | null>(null);
  const [closed, setClosed] = useState<string | null>(null);
  const [month, setMonth] = useState("");
  const [numbers, setNumbers] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ refused: boolean; text: string; answer?: AdditionResult } | null>(null);

  // Перечитать журнал при открытии и после дозагрузки; от обработчиков App не зависит, иначе каждая перерисовка App
  // (тост) перечитывала бы журнал.
  const reload = useCallback(async () => {
    try {
      setJournal(await getExchangeJournal());
      setClosed(null);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else if (error instanceof ApiError && error.status === 403) setClosed(error.message);
      else showToast(error instanceof Error ? error.message : "Не удалось загрузить журнал обмена", "error");
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const list = parseNumbers(numbers);
    if (!MONTH_PATTERN.test(month.trim())) {
      setResult({ refused: true, text: "Месяц выпуска задаётся как ГГГГ-ММ, например 2026-09." });
      return;
    }
    if (!list || !list.length) {
      setResult({ refused: true, text: "Номера сигналов: числа через запятую или пробел." });
      return;
    }
    setBusy(true);
    try {
      const answer = await buildAddition(month.trim(), list);
      setResult({ refused: false, text: answer.message, answer });
      if (answer.package) setNumbers("");
      await reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
      else if (error instanceof ApiError && error.status === 409) setResult({ refused: true, text: error.message });
      else showToast(error instanceof Error ? error.message : "Дозагрузка не собрана", "error");
    } finally {
      setBusy(false);
    }
  }

  if (closed) {
    return (
      <section className="screenStack">
        <header className="screenHeader"><div><h1>Журнал обмена</h1></div></header>
        <section className="panel"><div className="emptyState">{closed}</div></section>
      </section>
    );
  }

  const settings = journal?.settings;
  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Журнал обмена</h1>
          <p>Пакеты для потребителя выпуска: что собрано, что он забрал, и дозагрузка по номерам.</p>
        </div>
      </header>

      <section className="panel" aria-label="Настройки обмена">
        <div className="panelHeader"><h2>Настройки обмена</h2></div>
        {settings ? (
          <div className="journalOverview">
            <p>
              Выпуск месяца собирается сам: {settings.enabled ? "шаг включён" : "шаг выключен"}
              {settings.first_month ? `, начиная с выпуска за ${monthLabel(settings.first_month)}` : ""}.
            </p>
            <p className="metaText">
              Ключ подписи платформы: {settings.signing_keys.join(", ") || "нет"}; ключ потребителя:{" "}
              {settings.consumer_keys.join(", ") || "нет"}; адресов потребителя: {settings.addresses}; токен:{" "}
              {settings.token ? "задан" : "не задан"}.
            </p>
            {settings.problems.length ? (
              <div className="documentAlert warn" role="alert">Пакеты не соберутся: {settings.problems.join("; ")}.</div>
            ) : null}
          </div>
        ) : null}
      </section>

      <section className="panel" aria-label="Дозагрузка">
        <div className="panelHeader"><h2>Дозагрузка</h2></div>
        <form className="jobsFilters" onSubmit={(event) => void submit(event)}>
          <label className="field">
            <span>Месяц выпуска</span>
            <input value={month} placeholder="ГГГГ-ММ" inputMode="numeric" maxLength={7}
              onChange={(event) => setMonth(event.target.value)} />
          </label>
          <label className="field">
            <span>Номера сигналов</span>
            <input value={numbers} placeholder="Через запятую или пробел"
              onChange={(event) => setNumbers(event.target.value)} />
          </label>
          <div className="field">
            <span aria-hidden="true">&nbsp;</span>
            <button type="submit" className="primaryButton compactButton" disabled={busy}>
              {busy ? "Собираем…" : "Собрать дозагрузку"}
            </button>
          </div>
        </form>
        <p className="metaText">
          Номер дубля уходит корнем склейки. Номер, которого нет или который не действует, отменяет всю дозагрузку;
          уже отправленный пропускается.
        </p>
        {result ? (
          <div className={result.refused ? "documentAlert bad" : "documentAlert"} role={result.refused ? "alert" : "status"}>
            {result.text}
          </div>
        ) : null}
      </section>

      <section className="panel" aria-label="Пакеты">
        <div className="panelHeader"><h2>Пакеты</h2></div>
        {journal === null ? <SkeletonList rows={3} label="Загружаем журнал обмена" /> : null}
        {journal !== null && journal.packages.length === 0 ? (
          <div className="emptyState">Пакетов для потребителя ещё нет.</div>
        ) : null}
        <div className="journalList">
          {journal?.packages.map((item) => <PackageEntry key={item.number} item={item} />)}
        </div>
      </section>
    </section>
  );
}
