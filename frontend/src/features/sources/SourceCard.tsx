import type { Source, SourceDiagnostics, SourceHealth, SourcePatch } from "../../api/types";
import { diagnosticText, healthClass, healthLabel } from "./sourceUtils";

type Props = {
  source: Source;
  health?: SourceHealth;
  diagnostic?: SourceDiagnostics;
  hasDraft: boolean;
  currentField: (field: keyof SourcePatch) => string;
  onDraftChange: (field: keyof SourcePatch, value: string | null) => void;
  onToggle: (enabled: boolean) => void;
  onSave: () => void;
  onDiagnose: () => void;
  onScrape: () => void;
};

export function SourceCard(props: Props) {
  const { source, health, diagnostic, hasDraft } = props;
  const primaryUrl = source.url || source.rss_url || source.listing_url || "";

  return (
    <article className="sourceCardReact">
      <div className="sourceTop">
        <div className="sourceSummary">
          <div className="sourceName">{source.name}</div>
          <div className="sourceLink">
            {primaryUrl ? (
              <a href={primaryUrl} target="_blank" rel="noreferrer">
                {primaryUrl}
              </a>
            ) : (
              "URL не задан"
            )}
          </div>
          <div className="sourceMeta">
            <span className={`miniPill ${healthClass(health?.verdict)}`}>{healthLabel(health?.verdict)}</span>
            <span className="miniPill muted">{Number(health?.articles || 0)} статей</span>
            <span className="metaText">{(source.parse_strategy || "—") + " · " + (source.source_type || "—")}</span>
            {health?.last_article_at ? <span className="metaText">последняя статья {String(health.last_article_at).slice(0, 10)}</span> : null}
            {hasDraft ? <span className="miniPill draft">есть правки</span> : null}
          </div>
        </div>
        <label className="toggleLabel">
          <input type="checkbox" checked={source.enabled} onChange={(event) => props.onToggle(event.target.checked)} />
          <span>{source.enabled ? "вкл" : "выкл"}</span>
        </label>
      </div>

      <div className="sourceActions">
        <button type="button" className="ghostButton" disabled={!hasDraft} onClick={props.onSave}>
          Сохранить
        </button>
        <button type="button" className="ghostButton" onClick={props.onDiagnose}>
          Диагностика
        </button>
        {source.parse_strategy === "request" ? (
          <button type="button" className="ghostButton" onClick={props.onScrape}>
            Проверить listing
          </button>
        ) : null}
        {source.last_seen_published_at ? (
          <span className="metaText">последний найденный материал {String(source.last_seen_published_at).slice(0, 10)}</span>
        ) : null}
      </div>

      <details className="sourceAdvancedReact">
        <summary>{`Настройка ${source.parse_strategy === "request" ? "и диагностика" : ""}`}</summary>
        <div className="sourceConfigGridReact">
          <InputField
            label="Основной URL"
            value={props.currentField("url")}
            onChange={(value) => props.onDraftChange("url", value || null)}
            placeholder="https://example.com"
          />
          <InputField
            label="RSS URL"
            value={props.currentField("rss_url")}
            onChange={(value) => props.onDraftChange("rss_url", value || null)}
            placeholder="RSS URL"
          />
          {source.parse_strategy === "request" ? (
            <InputField
              label="Listing URL"
              value={props.currentField("listing_url")}
              onChange={(value) => props.onDraftChange("listing_url", value || null)}
              placeholder="Страница новостей"
            />
          ) : null}
        </div>
      </details>
      {diagnostic ? <DiagnosticsPanel diagnostic={diagnostic} /> : null}
    </article>
  );
}

function InputField(props: {
  label: string;
  value: string;
  placeholder: string;
  disabled?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <label className="field">
      <span>{props.label}</span>
      <input
        value={props.value}
        disabled={props.disabled}
        placeholder={props.placeholder}
        onChange={(event) => props.onChange(event.target.value)}
      />
    </label>
  );
}

function DiagnosticsPanel({ diagnostic }: { diagnostic: SourceDiagnostics }) {
  return (
    <div className="diagnosticsPanel">
      <div className="diagnosticsHeader">
        <strong>Диагностика</strong>
        <span className={`miniPill ${diagnostic.verdict === "ok" ? "ok" : "muted"}`}>{diagnostic.verdict || "—"}</span>
      </div>
      <pre>{diagnosticText(diagnostic)}</pre>
    </div>
  );
}
