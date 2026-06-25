import { TRIAGE_FILTER_OPTIONS } from "./sourceUtils";

type Props = {
  search: string;
  strategy: string;
  enabled: string;
  healthVerdict: string;
  triageKey: string;
  onSearchChange: (value: string) => void;
  onStrategyChange: (value: string) => void;
  onEnabledChange: (value: string) => void;
  onHealthChange: (value: string) => void;
  onTriageChange: (value: string) => void;
  onReset: () => void;
};

const STRATEGY_OPTIONS = ["rss", "request", "telegram", "none"];

const STRATEGY_LABELS: Record<string, string> = {
  rss: "RSS",
  request: "Запрос",
  telegram: "Telegram",
  none: "Нет",
};

export function SourceFilters(props: Props) {
  return (
    <div className="sourceFiltersGrid">
      <label className="field">
        <span>Поиск</span>
        <input value={props.search} onChange={(event) => props.onSearchChange(event.target.value)} placeholder="Название или ссылка" />
      </label>
      <label className="field">
        <span>Стратегия</span>
        <select value={props.strategy} onChange={(event) => props.onStrategyChange(event.target.value)}>
          <option value="">Все</option>
          {STRATEGY_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {STRATEGY_LABELS[option] ?? option}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Статус</span>
        <select value={props.enabled} onChange={(event) => props.onEnabledChange(event.target.value)}>
          <option value="">Все</option>
          <option value="on">Включены</option>
          <option value="off">Выключены</option>
        </select>
      </label>
      <label className="field">
        <span>Покрытие</span>
        <select value={props.healthVerdict} onChange={(event) => props.onHealthChange(event.target.value)}>
          <option value="">Все</option>
          <option value="ok">ОК</option>
          <option value="stale">Застой</option>
          <option value="no_articles">0 статей</option>
          <option value="disabled">Выкл</option>
        </select>
      </label>
      <label className="field">
        <span>Проблема</span>
        <select value={props.triageKey} onChange={(event) => props.onTriageChange(event.target.value)}>
          {TRIAGE_FILTER_OPTIONS.map((option) => (
            <option key={option.value || "all"} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <button type="button" className="ghostButton" onClick={props.onReset}>
        Сбросить
      </button>
    </div>
  );
}
