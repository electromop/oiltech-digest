import { useEffect, useState } from "react";
import { getMaintenanceStatus, getReadinessBenchmark, runMaintenanceCleanup } from "../../api/maintenance";
import type { MaintenanceCleanupResult, MaintenanceStatus, ReadinessBenchmarkReport } from "../../api/types";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

export function MaintenancePage({ onUnauthorized, showToast }: Props) {
  const [status, setStatus] = useState<MaintenanceStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [backgroundJobDays, setBackgroundJobDays] = useState("");
  const [exportJobDays, setExportJobDays] = useState("");
  const [lastResult, setLastResult] = useState<MaintenanceCleanupResult | null>(null);
  const [benchmark, setBenchmark] = useState<ReadinessBenchmarkReport | null>(null);
  const [benchmarkLoading, setBenchmarkLoading] = useState(false);

  useEffect(() => {
    void reload();
  }, []);

  async function reload() {
    try {
      setLoading(true);
      setStatus(await getMaintenanceStatus());
    } catch (error) {
      handleError(error, "Не удалось загрузить maintenance-статус");
    } finally {
      setLoading(false);
    }
  }

  function handleError(error: unknown, fallback: string) {
    const statusCode = typeof error === "object" && error && "status" in error ? Number(error.status) : 0;
    const message = error instanceof Error ? error.message : fallback;
    if (statusCode === 401) {
      onUnauthorized();
      return;
    }
    showToast(message || fallback, "error");
  }

  async function handleCleanup() {
    try {
      setRunning(true);
      const payload = {
        background_job_days: backgroundJobDays.trim() ? Number(backgroundJobDays) : undefined,
        export_job_days: exportJobDays.trim() ? Number(exportJobDays) : undefined,
      };
      const response = await runMaintenanceCleanup(payload);
      setLastResult(response.result);
      showToast("Service cleanup завершен");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось выполнить service cleanup");
    } finally {
      setRunning(false);
    }
  }

  async function handleBenchmarkRun() {
    try {
      setBenchmarkLoading(true);
      setBenchmark(await getReadinessBenchmark());
      showToast("Read-only benchmark завершен");
    } catch (error) {
      handleError(error, "Не удалось выполнить benchmark");
    } finally {
      setBenchmarkLoading(false);
    }
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Service maintenance</h1>
        </div>
        <div className="panelActions">
          <a className="ghostButton compactButton" href="?screen=jobs">
            Задачи
          </a>
          <div className="statusPill">Скрытая страница</div>
        </div>
      </header>

      <section className="statsGridReact jobsStats maintenanceStats">
        <div className="statCardReact">
          <strong>{status?.expired_sessions ?? "—"}</strong>
          <span>Истекшие сессии</span>
        </div>
        <div className="statCardReact">
          <strong>{status?.stale_running_jobs ?? "—"}</strong>
          <span>Зависшие running jobs</span>
        </div>
        <div className="statCardReact">
          <strong>{status?.cleanup_candidates.background_jobs ?? "—"}</strong>
          <span>Background jobs к cleanup</span>
        </div>
        <div className="statCardReact">
          <strong>{status?.cleanup_candidates.export_jobs ?? "—"}</strong>
          <span>Export jobs к cleanup</span>
        </div>
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>External contour</h2>
          <a className="ghostButton compactButton" href="?screen=jobs">
            Все задачи
          </a>
        </div>
        <section className="statsGridReact jobsStats externalStats">
          <div className="statCardReact">
            <strong>{status?.external_queues.totals.queued ?? "—"}</strong>
            <span>External queued</span>
          </div>
          <div className="statCardReact">
            <strong>{status?.external_queues.totals.running ?? "—"}</strong>
            <span>External running</span>
          </div>
          <div className="statCardReact">
            <strong>{status?.external_queues.totals.failed ?? "—"}</strong>
            <span>External failed</span>
          </div>
          <div className="statCardReact">
            <strong>{status?.external_queues.totals.expired_leases ?? "—"}</strong>
            <span>Expired leases</span>
          </div>
        </section>
        <div className="externalMeta">
          <span>Oldest queued: {formatDate(status?.external_queues.totals.oldest_queued_at)}</span>
          <span>Last heartbeat: {formatDate(status?.external_queues.totals.last_heartbeat_at)}</span>
        </div>
        {status?.external_queues.queues.length ? (
          <div className="externalQueueList">
            {status.external_queues.queues.map((queue) => (
              <div key={queue.queue_name} className="externalQueueRow">
                <strong>{queue.queue_name}</strong>
                <span>queued {queue.queued}</span>
                <span>running {queue.running}</span>
                <span>failed {queue.failed}</span>
                <span>heartbeat {formatDate(queue.last_heartbeat_at)}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="emptyState compactEmptyState">
            <strong>External queues пустые</strong>
            <span>Задачи появятся после включения `external-*` routing.</span>
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Service cleanup</h2>
          <button type="button" className="ghostButton" onClick={() => void reload()} disabled={loading || running}>
            Обновить
          </button>
        </div>

        <div className="maintenanceGrid">
          <label className="field">
            <span>Background jobs retention, days</span>
            <input
              value={backgroundJobDays}
              onChange={(event) => setBackgroundJobDays(event.target.value)}
              placeholder={String(status?.retention.background_job_days ?? 30)}
              inputMode="numeric"
            />
          </label>
          <label className="field">
            <span>Export jobs retention, days</span>
            <input
              value={exportJobDays}
              onChange={(event) => setExportJobDays(event.target.value)}
              placeholder={String(status?.retention.export_job_days ?? 30)}
              inputMode="numeric"
            />
          </label>
          <label className="field maintenanceReadonly">
            <span>Stale minutes</span>
            <input value={String(status?.retention.stale_minutes ?? "—")} readOnly />
          </label>
          <div className="maintenanceActions">
            <button type="button" className="primaryButton" onClick={() => void handleCleanup()} disabled={running}>
              {running ? "Выполняем..." : "Запустить cleanup"}
            </button>
          </div>
        </div>

        {loading ? (
          <div className="inlineLoader jobsLoader">
            <span className="loaderDot" />
            <span>Загружаем maintenance-статус…</span>
          </div>
        ) : null}

        {lastResult ? (
          <div className="maintenanceResult">
            <div className="metaLabel">Последний запуск</div>
            <div className="maintenanceResultGrid">
              <span>Истекшие сессии: {lastResult.expired_sessions}</span>
              <span>Background jobs: {lastResult.background_jobs}</span>
              <span>Export jobs: {lastResult.export_jobs}</span>
            </div>
          </div>
        ) : null}
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Read-only benchmark</h2>
          <button
            type="button"
            className="ghostButton"
            onClick={() => void handleBenchmarkRun()}
            disabled={benchmarkLoading}
          >
            {benchmarkLoading ? "Замеряем..." : "Запустить benchmark"}
          </button>
        </div>

        <p className="panelHint">
          Прогоняет безопасные read-only запросы по основным экранным данным: каталог сигналов, digest candidates,
          health источников и очереди задач.
        </p>

        {benchmark ? (
          <div className="benchmarkStack">
            <div className="maintenanceResult">
              <div className="metaLabel">Снимок датасета</div>
              <div className="maintenanceResultGrid">
                {Object.entries(benchmark.counts).map(([key, value]) => (
                  <span key={key}>
                    {key}: {value}
                  </span>
                ))}
              </div>
            </div>

            <div className="benchmarkTable">
              <div className="benchmarkRow benchmarkHead">
                <span>Check</span>
                <span>Status</span>
                <span>P50</span>
                <span>P95</span>
                <span>Max</span>
                <span>Rows</span>
              </div>
              {benchmark.benchmarks.map((item) => (
                <div key={item.name} className="benchmarkRow">
                  <span>{item.name}</span>
                  <span className={item.status === "warn" ? "benchmarkWarn" : "benchmarkOk"}>{item.status}</span>
                  <span>{item.p50_ms} ms</span>
                  <span>{item.p95_ms} ms</span>
                  <span>{item.max_ms} ms</span>
                  <span>{item.rows}</span>
                </div>
              ))}
            </div>

            {benchmark.warnings.length ? (
              <div className="maintenanceResult">
                <div className="metaLabel">Warnings</div>
                <div className="maintenanceResultGrid">
                  {benchmark.warnings.map((item) => (
                    <span key={item}>{item}</span>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : (
          <div className="emptyState">
            <strong>Benchmark еще не запускался</strong>
            <span>Запуск идет через backend API и не создает новых задач или AI-вызовов.</span>
          </div>
        )}
      </section>
    </section>
  );
}

function formatDate(value?: string | null) {
  if (!value) return "—";
  return new Date(value).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
