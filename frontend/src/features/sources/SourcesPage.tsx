import { useEffect, useMemo, useState } from "react";
import {
  createSource,
  diagnoseSource,
  listSourceHealth,
  listSources,
  scrapeSource,
  updateSource,
} from "../../api/sources";
import type { Source, SourceDiagnostics, SourceHealth, SourcePatch } from "../../api/types";
import { SourceCard } from "./SourceCard";
import { SourceFilters } from "./SourceFilters";
import { normalizePatch } from "./sourceUtils";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

type DraftMap = Record<number, SourcePatch>;

export function SourcesPage({ onUnauthorized, showToast }: Props) {
  const [sources, setSources] = useState<Source[]>([]);
  const [health, setHealth] = useState<SourceHealth[]>([]);
  const [diagnostics, setDiagnostics] = useState<Record<number, SourceDiagnostics>>({});
  const [drafts, setDrafts] = useState<DraftMap>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState("");
  const [strategy, setStrategy] = useState("");
  const [enabled, setEnabled] = useState("");
  const [healthVerdict, setHealthVerdict] = useState("");
  const [newSourceName, setNewSourceName] = useState("");
  const [newSourceRss, setNewSourceRss] = useState("");
  const [newSourceFrequency, setNewSourceFrequency] = useState("");

  useEffect(() => {
    void reload();
  }, []);

  async function reload() {
    try {
      setLoading(true);
      const [sourcesPayload, healthPayload] = await Promise.all([listSources(), listSourceHealth()]);
      setSources(sourcesPayload);
      setHealth(healthPayload);
    } catch (error) {
      handleError(error, "Не удалось загрузить источники");
    } finally {
      setLoading(false);
    }
  }

  function handleError(error: unknown, fallback: string) {
    const status = typeof error === "object" && error && "status" in error ? Number(error.status) : 0;
    const message = error instanceof Error ? error.message : fallback;
    if (status === 401) {
      onUnauthorized();
      return;
    }
    showToast(message || fallback, "error");
  }

  function getSourceHealth(sourceId: number) {
    return health.find((item) => Number(item.id) === Number(sourceId));
  }

  const healthCounts = useMemo(() => {
    const counts = { ok: 0, stale: 0, no_articles: 0, disabled: 0 };
    health.forEach((item) => {
      if (item.verdict in counts) {
        counts[item.verdict as keyof typeof counts] += 1;
      }
    });
    return counts;
  }, [health]);

  const filteredSources = useMemo(() => {
    const q = search.trim().toLowerCase();
    const verdictRank: Record<string, number> = { no_articles: 0, stale: 1, disabled: 2, ok: 3 };

    return sources
      .filter((source) => {
        const sourceHealth = getSourceHealth(source.id);
        const hay = [
          source.name,
          source.url,
          source.rss_url,
          source.listing_url,
          source.source_type,
        ]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();

        return (
          (!q || hay.includes(q)) &&
          (!strategy || source.parse_strategy === strategy) &&
          (!enabled || (enabled === "on" ? source.enabled : !source.enabled)) &&
          (!healthVerdict || sourceHealth?.verdict === healthVerdict)
        );
      })
      .sort((left, right) => {
        const leftHealth = getSourceHealth(left.id);
        const rightHealth = getSourceHealth(right.id);
        const verdictDelta =
          (verdictRank[leftHealth?.verdict || "disabled"] ?? 9) -
          (verdictRank[rightHealth?.verdict || "disabled"] ?? 9);
        if (verdictDelta !== 0) return verdictDelta;
        const articleDelta = Number(leftHealth?.articles || 0) - Number(rightHealth?.articles || 0);
        if (articleDelta !== 0) return articleDelta;
        return left.name.localeCompare(right.name, "ru");
      });
  }, [enabled, health, healthVerdict, search, sources, strategy]);

  function currentPatch(source: Source) {
    return drafts[source.id] ?? {};
  }

  function currentField(source: Source, field: keyof SourcePatch) {
    const draft = currentPatch(source)[field];
    if (draft === undefined) {
      return source[field] ?? "";
    }
    return draft ?? "";
  }

  function updateDraft(sourceId: number, field: keyof SourcePatch, value: string | boolean | null) {
    setDrafts((prev) => ({
      ...prev,
      [sourceId]: {
        ...(prev[sourceId] ?? {}),
        [field]: value,
      },
    }));
  }

  async function handleCreateSource() {
    if (!newSourceName.trim() || !newSourceRss.trim()) {
      showToast("Введите название и RSS URL", "error");
      return;
    }
    try {
      setBusy(true);
      await createSource({
        name: newSourceName.trim(),
        rss_url: newSourceRss.trim(),
        url: newSourceRss.trim(),
        update_frequency: newSourceFrequency.trim() || null,
        category: "manual",
        priority: 1,
      });
      setNewSourceName("");
      setNewSourceRss("");
      setNewSourceFrequency("");
      showToast("RSS-источник добавлен");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось добавить источник");
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveSource(source: Source) {
    try {
      setBusy(true);
      const payload = normalizePatch(currentPatch(source));
      await updateSource(source.id, payload);
      setDrafts((prev) => {
        const next = { ...prev };
        delete next[source.id];
        return next;
      });
      showToast("Источник сохранён");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось сохранить источник");
    } finally {
      setBusy(false);
    }
  }

  async function handleToggleSource(source: Source, nextEnabled: boolean) {
    try {
      setBusy(true);
      await updateSource(source.id, { enabled: nextEnabled });
      showToast(nextEnabled ? "Источник включён" : "Источник выключен");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось изменить статус источника");
    } finally {
      setBusy(false);
    }
  }

  async function handleDiagnoseSource(source: Source) {
    try {
      setBusy(true);
      const payload = normalizePatch(currentPatch(source));
      const result = await diagnoseSource(source.id, payload);
      setDiagnostics((prev) => ({ ...prev, [source.id]: result }));
      showToast("Диагностика источника готова");
    } catch (error) {
      handleError(error, "Не удалось выполнить диагностику");
    } finally {
      setBusy(false);
    }
  }

  async function handleScrapeSource(source: Source) {
    try {
      setBusy(true);
      const result = await scrapeSource(source.id);
      showToast(`Скрапинг: добавлено ${result.stats.added}, дублей ${result.stats.attempted - result.stats.added}`);
      await reload();
    } catch (error) {
      handleError(error, "Не удалось проверить listing");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Источники</h1>
        </div>
        <div className="statusPill">{filteredSources.length} источников</div>
      </header>

      <section className="panel">
        {busy ? <InlineLoader label="Обновляем источники…" /> : null}
        <div className="panelHeader">
          <h2>Добавить RSS</h2>
          <span className="badge">manual seed</span>
        </div>
        <div className="sourceCreateGrid">
          <label className="field">
            <span>Название</span>
            <input value={newSourceName} onChange={(event) => setNewSourceName(event.target.value)} placeholder="Название источника" />
          </label>
          <label className="field">
            <span>RSS URL</span>
            <input value={newSourceRss} onChange={(event) => setNewSourceRss(event.target.value)} placeholder="https://example.com/rss.xml" />
          </label>
          <label className="field">
            <span>Частота</span>
            <input value={newSourceFrequency} onChange={(event) => setNewSourceFrequency(event.target.value)} placeholder="ежедневно" />
          </label>
          <button type="button" className="primaryButton" onClick={() => void handleCreateSource()}>
            Добавить RSS
          </button>
        </div>
      </section>

      <section className="panel">
        {busy ? <InlineLoader label="Подгружаем данные…" /> : null}
        <div className="panelHeader">
          <h2>Каталог источников</h2>
          <button type="button" className="ghostButton" onClick={() => void reload()}>
            Обновить
          </button>
        </div>

        <div className="sourceHealthStats">
          <button
            type="button"
            className={healthVerdict === "" ? "sourceStatCard active" : "sourceStatCard"}
            onClick={() => setHealthVerdict("")}
          >
            <span className="sourceStatValue">{health.length}</span>
            <span className="sourceStatLabel">Все</span>
          </button>
          <button
            type="button"
            className={healthVerdict === "no_articles" ? "sourceStatCard active problem" : "sourceStatCard problem"}
            onClick={() => setHealthVerdict("no_articles")}
          >
            <span className="sourceStatValue">{healthCounts.no_articles}</span>
            <span className="sourceStatLabel">0 статей</span>
          </button>
          <button
            type="button"
            className={healthVerdict === "stale" ? "sourceStatCard active warning" : "sourceStatCard warning"}
            onClick={() => setHealthVerdict("stale")}
          >
            <span className="sourceStatValue">{healthCounts.stale}</span>
            <span className="sourceStatLabel">Застой</span>
          </button>
          <button
            type="button"
            className={healthVerdict === "ok" ? "sourceStatCard active success" : "sourceStatCard success"}
            onClick={() => setHealthVerdict("ok")}
          >
            <span className="sourceStatValue">{healthCounts.ok}</span>
            <span className="sourceStatLabel">ОК</span>
          </button>
          <button
            type="button"
            className={healthVerdict === "disabled" ? "sourceStatCard active" : "sourceStatCard"}
            onClick={() => setHealthVerdict("disabled")}
          >
            <span className="sourceStatValue">{healthCounts.disabled}</span>
            <span className="sourceStatLabel">Выкл</span>
          </button>
        </div>

        <SourceFilters
          search={search}
          strategy={strategy}
          enabled={enabled}
          healthVerdict={healthVerdict}
          onSearchChange={setSearch}
          onStrategyChange={setStrategy}
          onEnabledChange={setEnabled}
          onHealthChange={setHealthVerdict}
          onReset={() => {
            setSearch("");
            setStrategy("");
            setEnabled("");
            setHealthVerdict("");
          }}
        />

        {loading ? (
          <div className="emptyState"><LoadingState label="Загружаем источники…" /></div>
        ) : filteredSources.length ? (
          <div className="sourceCardGrid">
            {filteredSources.map((source) => {
              const sourceHealth = getSourceHealth(source.id);
              const diagnostic = diagnostics[source.id];
              const hasDraft = Object.keys(currentPatch(source)).length > 0;
              return (
                <SourceCard
                  key={source.id}
                  source={source}
                  health={sourceHealth}
                  diagnostic={diagnostic}
                  hasDraft={hasDraft}
                  currentField={(field) => String(currentField(source, field))}
                  onDraftChange={(field, value) => updateDraft(source.id, field, value)}
                  onToggle={(nextEnabled) => void handleToggleSource(source, nextEnabled)}
                  onSave={() => void handleSaveSource(source)}
                  onDiagnose={() => void handleDiagnoseSource(source)}
                  onScrape={() => void handleScrapeSource(source)}
                />
              );
            })}
          </div>
        ) : (
          <div className="emptyState">Источники не найдены.</div>
        )}
      </section>
    </section>
  );
}

function InlineLoader(props: { label: string }) {
  return (
    <div className="loadingOverlay">
      <div className="spinnerReact" />
      <span>{props.label}</span>
    </div>
  );
}

function LoadingState(props: { label: string }) {
  return (
    <div className="loadingStateReact">
      <div className="spinnerReact" />
      <span>{props.label}</span>
    </div>
  );
}
