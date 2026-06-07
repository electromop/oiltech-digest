import { useEffect, useMemo, useState } from "react";
import { listArticles } from "../../api/articles";
import { enqueueDigestExport, getDigestContent, getMonthlyDigest, saveDigestDraft } from "../../api/digest";
import { downloadJobResult, getJob } from "../../api/jobs";
import type { Article, BackgroundJob } from "../../api/types";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

export function DigestPage({ onUnauthorized, showToast }: Props) {
  const [articles, setArticles] = useState<Article[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState("");
  const [tag, setTag] = useState("");
  const [tagQuery, setTagQuery] = useState("");
  const [showTagOptions, setShowTagOptions] = useState(false);
  const [month, setMonth] = useState("");
  const [scoreMin, setScoreMin] = useState(0);
  const [scoreMax, setScoreMax] = useState(100);
  const [draftInfo, setDraftInfo] = useState<string>("");
  const [exportJobId, setExportJobId] = useState<number | null>(null);

  useEffect(() => {
    void reload();
  }, []);

  async function reload() {
    try {
      setLoading(true);
      setArticles(await listArticles());
    } catch (error) {
      handleError(error, "Не удалось загрузить статьи для дайджеста");
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

  const digestCandidates = useMemo(() => {
    const q = search.trim().toLowerCase();
    return articles.filter((article) => {
      const hay = [article.title, article.tag, article.summary].join(" ").toLowerCase();
      return (
        article.digest &&
        (!q || hay.includes(q)) &&
        (!tag || article.tag === tag || article.tag.startsWith(`${tag} /`)) &&
        Number(article.score || 0) >= scoreMin &&
        Number(article.score || 0) <= scoreMax &&
        (!month || String(article.date || "").startsWith(month))
      );
    });
  }, [articles, month, scoreMax, scoreMin, search, tag]);

  const months = useMemo(() => {
    return [...new Set(articles.filter((article) => article.digest).map((article) => String(article.date || "").slice(0, 7)).filter(Boolean))]
      .sort()
      .reverse();
  }, [articles]);

  const topTags = useMemo(() => {
    const names = [...new Set(articles.map((article) => article.tag).filter(Boolean))].sort();
    return [...new Set(names.map((name) => name.split(" / ")[0]))];
  }, [articles]);
  const filteredTagOptions = useMemo(() => {
    const q = tagQuery.trim().toLowerCase();
    return topTags.filter((option) => !q || option.toLowerCase().includes(q));
  }, [tagQuery, topTags]);

  function currentDigestParams() {
    const params = new URLSearchParams({
      month,
      limit: "200",
      min_score: String(scoreMin),
    });
    return params.toString();
  }

  function currentDigestPayload() {
    return {
      month: month || String(new Date().toISOString()).slice(0, 7),
      limit: 50,
      min_score: scoreMin,
    };
  }

  async function handleCopyJson() {
    try {
      setBusy(true);
      const content = await getDigestContent(month, 200, scoreMin);
      await navigator.clipboard.writeText(JSON.stringify(content, null, 2));
      showToast("JSON дайджеста скопирован");
    } catch (error) {
      handleError(error, "Не удалось собрать JSON дайджеста");
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveDraft() {
    try {
      setBusy(true);
      const result = await saveDigestDraft(currentDigestPayload());
      setDraftInfo(`draft ${result.month}: ${result.items} статей`);
      showToast(`Draft ${result.month} сохранён: ${result.items} статей`);
    } catch (error) {
      handleError(error, "Не удалось сохранить draft");
    } finally {
      setBusy(false);
    }
  }

  async function handleLoadDraft() {
    const targetMonth = month || String(new Date().toISOString()).slice(0, 7);
    try {
      setBusy(true);
      const draft = await getMonthlyDigest(targetMonth);
      setDraftInfo(`${draft.month} · ${draft.status} · ${draft.items.length} статей`);
      showToast(`Загружен draft ${draft.month}`);
    } catch (error) {
      handleError(error, "Сохранённый draft не найден");
    } finally {
      setBusy(false);
    }
  }

  function openDigestEmail() {
    window.open(`/api/digest-email?${currentDigestParams()}`, "_blank", "noopener,noreferrer");
  }

  async function handleDigestExport(format: "pdf" | "doc" | "html") {
    try {
      setBusy(true);
      const queued = await enqueueDigestExport(month, 200, scoreMin, format);
      setExportJobId(queued.job.id);
      setDraftInfo(`export job #${queued.job.id}: ${statusLabel(queued.job.status)}`);
      showToast(`Экспорт поставлен в очередь: job #${queued.job.id}`);

      const finished = await waitForExportJob(queued.job.id);
      setDraftInfo(`export job #${finished.id}: ${statusLabel(finished.status)}`);
      if (finished.status !== "ok") {
        throw new Error(finished.error || "Экспорт завершился ошибкой");
      }

      const file = await downloadJobResult(finished.id);
      const url = window.URL.createObjectURL(file.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = file.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
      showToast(`Экспорт готов: ${file.filename}`);
    } catch (error) {
      handleError(error, "Не удалось скачать экспорт");
    } finally {
      setBusy(false);
    }
  }

  async function waitForExportJob(jobId: number) {
    for (let attempt = 0; attempt < 80; attempt += 1) {
      const job = await getJob(jobId);
      setDraftInfo(`export job #${job.id}: ${statusLabel(job.status)} · ${Math.round(job.progress)}%`);
      if (job.status === "ok" || job.status === "failed") {
        return job;
      }
      await sleep(1500);
    }
    throw new Error(`Job #${jobId} не завершилась за отведённое время`);
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Месячный дайджест</h1>
        </div>
        <div className="statusPill">{digestCandidates.length} статей</div>
      </header>

      <section className="panel">
        {busy ? <InlineLoader label="Готовим экспорт в фоне…" /> : null}
        <div className="panelHeader">
          <h2>Выборка дайджеста</h2>
        </div>

        <div className="digestFiltersRow">
          <label className="field">
            <span>Поиск</span>
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Найти статью" />
          </label>
          <div className="field sourceComboReact">
            <span>Тег</span>
            <input
              value={tag || tagQuery}
              onChange={(event) => {
                setTag("");
                setTagQuery(event.target.value);
                setShowTagOptions(true);
              }}
              onFocus={() => setShowTagOptions(true)}
              onBlur={() => {
                window.setTimeout(() => setShowTagOptions(false), 120);
              }}
              placeholder="Все теги"
            />
            {showTagOptions ? (
              <div className="sourcePopoverReact">
                <button
                  type="button"
                  className="sourceOptionReact"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    setTag("");
                    setTagQuery("");
                    setShowTagOptions(false);
                  }}
                >
                  <span>Все теги</span>
                  <span className="metaText">сброс</span>
                </button>
                {filteredTagOptions.map((option) => (
                  <button
                    key={option}
                    type="button"
                    className="sourceOptionReact"
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => {
                      setTag(option);
                      setTagQuery(option);
                      setShowTagOptions(false);
                    }}
                  >
                    <span>{option}</span>
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </div>

        <div className="digestFiltersRow digestFiltersRowSecondary">
          <label className="field">
            <span>Месяц</span>
            <select value={month} onChange={(event) => setMonth(event.target.value)}>
              <option value="">Все месяцы</option>
              {months.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Score от</span>
            <input type="number" value={scoreMin} onChange={(event) => setScoreMin(Number(event.target.value || 0))} />
          </label>
          <label className="field">
            <span>Score до</span>
            <input type="number" value={scoreMax} onChange={(event) => setScoreMax(Number(event.target.value || 100))} />
          </label>
        </div>

        <div className="digestToolbar">
          <div className="digestActionGroup">
            <span className="digestGroupLabel">Просмотр</span>
            <button type="button" className="ghostButton" onClick={openDigestEmail}>
              Открыть HTML
            </button>
            <button type="button" className="ghostButton" onClick={() => void handleCopyJson()}>
              Скопировать JSON
            </button>
          </div>

          <div className="digestActionGroup">
            <span className="digestGroupLabel">Draft</span>
            <button type="button" className="ghostButton" onClick={() => void handleSaveDraft()}>
              Сохранить
            </button>
            <button type="button" className="ghostButton" onClick={() => void handleLoadDraft()}>
              Проверить
            </button>
          </div>

          <div className="digestActionGroup digestActionGroupPrimary">
            <span className="digestGroupLabel">Экспорт</span>
            <button type="button" className="primaryButton" onClick={() => void handleDigestExport("pdf")}>
              PDF
            </button>
            <button type="button" className="ghostButton" onClick={() => void handleDigestExport("doc")}>
              DOCX
            </button>
            <button type="button" className="ghostButton" onClick={() => void handleDigestExport("html")}>
              HTML
            </button>
          </div>
        </div>

        {draftInfo ? (
          <div className="digestDraftInfo metaText">
            {draftInfo}
            {exportJobId ? (
              <>
                {" · "}
                <a href="?screen=jobs">Открыть задачи</a>
              </>
            ) : null}
          </div>
        ) : null}

        {loading ? (
          <div className="emptyState"><LoadingState label="Загружаем сигналы…" /></div>
        ) : digestCandidates.length ? (
          <div className="digestLayout">
            <div className="digestPickListReact">
              {digestCandidates.map((article) => (
                <div className="digestPickRow" key={article.id}>
                  <a href={article.url} target="_blank" rel="noreferrer">
                    <strong>{article.title}</strong>
                  </a>
                  <div className="metaText">{article.tag} · score {Math.round(Number(article.score || 0))} · {formatDate(article.date)}</div>
                </div>
              ))}
            </div>
            <div className="digestPreviewGrid">
              {digestCandidates.map((article, index) => (
                <article className="digestPreviewCard" key={article.id}>
                  <strong>{index + 1}. {article.tag}</strong>
                  <div>{article.title}</div>
                  <div className="metaText">{article.summary || "Суть ещё не сформирована."}</div>
                </article>
              ))}
            </div>
          </div>
        ) : (
          <div className="emptyState">Нет статей со статусом «В дайджест» по текущим фильтрам.</div>
        )}
      </section>
    </section>
  );
}

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function statusLabel(status: BackgroundJob["status"]) {
  if (status === "queued") return "в очереди";
  if (status === "running") return "в работе";
  if (status === "ok") return "готово";
  return "ошибка";
}

function formatDate(value: string | null) {
  return value ? new Date(`${value}T00:00:00`).toLocaleDateString("ru-RU") : "—";
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
