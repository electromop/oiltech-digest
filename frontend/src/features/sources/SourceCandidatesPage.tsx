import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../../api/client";
import {
  approveSourceCandidate,
  evaluateSourceCandidate,
  listSourceCandidateArticles,
  listSourceCandidates,
  listSourceCandidateTriage,
  updateSourceCandidate,
} from "../../api/sources";
import type { SourceCandidate, SourceCandidateArticle, SourceCandidateTriageRow } from "../../api/types";

type Props = {
  onUnauthorized: () => void;
  showToast: (text: string, tone?: "default" | "error") => void;
};

const STATUS_OPTIONS = [
  { value: "", label: "Все статусы" },
  { value: "needs_human_review", label: "На проверке" },
  { value: "new", label: "Новые" },
  { value: "test_parsing", label: "Тестировать еще" },
  { value: "approved", label: "Одобрены" },
  { value: "rejected", label: "Отклонены" },
  { value: "paused", label: "Пауза" },
];

function statusLabel(status: SourceCandidate["status"]) {
  return STATUS_OPTIONS.find((item) => item.value === status)?.label ?? status;
}

function actionLabel(action: SourceCandidate["recommended_action"]) {
  if (action === "add") return "Добавить";
  if (action === "test_more") return "Тестировать еще";
  if (action === "reject") return "Отклонить";
  if (action === "human_review") return "Ручная проверка";
  return "Нет рекомендации";
}

function candidateTitle(candidate: SourceCandidate) {
  return candidate.name || candidate.normalized_domain || candidate.url;
}

function initialStringParam(name: string) {
  return new URLSearchParams(window.location.search).get(name) || "";
}

function initialNumberParam(name: string) {
  const value = Number(new URLSearchParams(window.location.search).get(name) || 0);
  return Number.isFinite(value) && value > 0 ? value : null;
}

export function SourceCandidatesPage({ onUnauthorized, showToast }: Props) {
  const [candidates, setCandidates] = useState<SourceCandidate[]>([]);
  const [triage, setTriage] = useState<SourceCandidateTriageRow[]>([]);
  const [articles, setArticles] = useState<Record<number, SourceCandidateArticle[]>>({});
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [status, setStatus] = useState(() => initialStringParam("status"));
  const [topic, setTopic] = useState(() => initialStringParam("topic"));
  const [busy, setBusy] = useState<Record<number, string>>({});
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const candidateId = initialNumberParam("candidate_id");
    void refresh().then(() => {
      if (candidateId) void openCandidate(candidateId);
    });
  }, []);

  const totals = useMemo(() => {
    return candidates.reduce(
      (acc, item) => {
        acc.total += 1;
        if (item.status === "needs_human_review") acc.review += 1;
        if (item.recommended_action === "add") acc.add += 1;
        if (item.status === "approved") acc.approved += 1;
        return acc;
      },
      { total: 0, review: 0, add: 0, approved: 0 },
    );
  }, [candidates]);

  async function refresh(nextFilters?: { status?: string; topic?: string }) {
    try {
      setLoading(true);
      const nextStatus = nextFilters?.status ?? status;
      const nextTopic = nextFilters?.topic ?? topic;
      const payload = await listSourceCandidates({
        status: nextStatus || undefined,
        topic: nextTopic.trim() || undefined,
        limit: 100,
      });
      setTriage(await listSourceCandidateTriage({ limit: 6 }));
      setCandidates(payload);
      return payload;
    } catch (error) {
      handleError(error, "Не удалось загрузить кандидатов");
      return [];
    } finally {
      setLoading(false);
    }
  }

  function handleError(error: unknown, fallback: string) {
    if (error instanceof ApiError && error.status === 401) {
      onUnauthorized();
      return;
    }
    showToast(error instanceof Error ? error.message : fallback, "error");
  }

  async function openCandidate(candidateId: number, forceOpen = false) {
    setExpandedId((current) => (forceOpen ? candidateId : current === candidateId ? null : candidateId));
    if (articles[candidateId]) return;
    try {
      setBusy((prev) => ({ ...prev, [candidateId]: "Загружаем материалы…" }));
      const payload = await listSourceCandidateArticles(candidateId);
      setArticles((prev) => ({ ...prev, [candidateId]: payload }));
    } catch (error) {
      handleError(error, "Не удалось загрузить материалы кандидата");
    } finally {
      setBusy((prev) => {
        const next = { ...prev };
        delete next[candidateId];
        return next;
      });
    }
  }

  async function focusCandidate(candidateId: number) {
    setStatus("");
    setTopic("");
    await refresh({ status: "", topic: "" });
    await openCandidate(candidateId, true);
  }

  async function handleEvaluate(candidate: SourceCandidate) {
    try {
      setBusy((prev) => ({ ...prev, [candidate.id]: "Проверяем в песочнице…" }));
      const result = await evaluateSourceCandidate(candidate.id, 5, true);
      showToast(`Проверено: ${result.metrics.relevant_articles}/${result.metrics.tested_articles} релевантных`);
      const [updatedCandidates, updatedArticles] = await Promise.all([
        listSourceCandidates({ status: status || undefined, topic: topic.trim() || undefined, limit: 100 }),
        listSourceCandidateArticles(candidate.id),
      ]);
      setCandidates(updatedCandidates);
      setArticles((prev) => ({ ...prev, [candidate.id]: updatedArticles }));
      setExpandedId(candidate.id);
    } catch (error) {
      handleError(error, "Не удалось проверить кандидата");
    } finally {
      setBusy((prev) => {
        const next = { ...prev };
        delete next[candidate.id];
        return next;
      });
    }
  }

  async function handleApprove(candidate: SourceCandidate) {
    try {
      setBusy((prev) => ({ ...prev, [candidate.id]: "Создаем источник…" }));
      const result = await approveSourceCandidate(candidate.id, {
        enabled: false,
        parse_strategy: candidate.candidate_type === "rss" ? "rss" : "request",
        network_region: "auto",
        scrape_after_approve: true,
      });
      showToast(result.initial_job ? `Источник создан: #${result.source_id}, первый сбор job #${result.initial_job.id}` : `Источник создан: #${result.source_id}`);
      await refresh();
    } catch (error) {
      handleError(error, "Не удалось одобрить кандидата");
    } finally {
      setBusy((prev) => {
        const next = { ...prev };
        delete next[candidate.id];
        return next;
      });
    }
  }

  async function handleDecision(
    candidate: SourceCandidate,
    decision: "test_more" | "reject" | "pause",
  ) {
    const payload = decision === "test_more"
      ? {
          status: "test_parsing" as const,
          recommended_action: "test_more" as const,
          review_comment: "Оператор отправил кандидата на дополнительную проверку.",
        }
      : decision === "reject"
        ? {
            status: "rejected" as const,
            recommended_action: "reject" as const,
            review_comment: "Оператор отклонил кандидата как неподходящий источник.",
          }
        : {
            status: "paused" as const,
            recommended_action: "human_review" as const,
            review_comment: "Оператор отложил решение по кандидату.",
          };
    try {
      setBusy((prev) => ({ ...prev, [candidate.id]: "Сохраняем решение…" }));
      await updateSourceCandidate(candidate.id, payload);
      showToast("Решение по кандидату сохранено");
      await refresh();
    } catch (error) {
      handleError(error, "Не удалось сохранить решение");
    } finally {
      setBusy((prev) => {
        const next = { ...prev };
        delete next[candidate.id];
        return next;
      });
    }
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Кандидаты источников</h1>
        </div>
        <button type="button" className="ghostButton compactButton" onClick={() => void refresh()} disabled={loading}>
          {loading ? "Обновляем…" : "Обновить"}
        </button>
      </header>

      <section className="sourceCandidateStats">
        <div className="sourceStatCard">
          <span className="sourceStatValue">{totals.total}</span>
          <span className="sourceStatLabel">Кандидатов</span>
        </div>
        <div className="sourceStatCard warning">
          <span className="sourceStatValue">{totals.review}</span>
          <span className="sourceStatLabel">На проверке</span>
        </div>
        <div className="sourceStatCard success">
          <span className="sourceStatValue">{totals.add}</span>
          <span className="sourceStatLabel">Можно добавить</span>
        </div>
        <div className="sourceStatCard">
          <span className="sourceStatValue">{totals.approved}</span>
          <span className="sourceStatLabel">Одобрены</span>
        </div>
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Ближайшие решения</h2>
          <span className="badge">{triage.length} в очереди</span>
        </div>
        <div className="sourceCandidateTriageList">
          {loading && !triage.length ? <div className="emptyState">Считаем приоритеты…</div> : null}
          {!loading && !triage.length ? <div className="emptyState">Очередь решений пустая.</div> : null}
          {triage.map((item) => (
            <button
              type="button"
              className="sourceCandidateTriageItem"
              key={item.id}
              onClick={() => void focusCandidate(item.id)}
            >
              <span>{Math.round(item.triage_priority)}</span>
              <strong>{candidateTitle(item)}</strong>
              <em>{item.triage_reason}</em>
              <small>{actionLabel(item.recommended_action)} · {item.relevant_articles}/{item.tested_articles} релев.</small>
            </button>
          ))}
        </div>
      </section>

      <section className="panel">
        <div className="sourceCandidateFilters">
          <label className="field">
            <span>Статус</span>
            <select value={status} onChange={(event) => setStatus(event.target.value)}>
              {STATUS_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Тема</span>
            <input value={topic} onChange={(event) => setTopic(event.target.value)} placeholder="Например: бурение" />
          </label>
          <div className="settingsActions">
            <button type="button" className="primaryButton compactButton" onClick={() => void refresh()} disabled={loading}>
              Применить
            </button>
            <button
              type="button"
              className="ghostButton compactButton"
              onClick={() => {
                setStatus("");
                setTopic("");
                void refresh({ status: "", topic: "" });
              }}
            >
              Сбросить
            </button>
          </div>
        </div>
      </section>

      <section className="sourceCandidateList">
        {loading && !candidates.length ? <div className="emptyState">Загружаем кандидатов…</div> : null}
        {!loading && !candidates.length ? <div className="emptyState">Кандидатов пока нет.</div> : null}
        {candidates.map((candidate) => {
          const isExpanded = expandedId === candidate.id;
          const pending = busy[candidate.id];
          return (
            <article className="sourceCandidateCard" key={candidate.id}>
              <div className="sourceCandidateTop">
                <button type="button" className="expandButton" onClick={() => void openCandidate(candidate.id)} aria-label="Раскрыть кандидата">
                  <svg className={isExpanded ? "groupChevron open" : "groupChevron"} width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                    <path d="M5 3.5 9.5 8 5 12.5" />
                  </svg>
                </button>
                <div className="sourceCandidateMain">
                  <div className="sourceName">{candidateTitle(candidate)}</div>
                  <div className="sourceLink"><a href={candidate.url} target="_blank" rel="noreferrer">{candidate.url}</a></div>
                </div>
                <div className="sourceCandidateBadges">
                  <span className="badge">{statusLabel(candidate.status)}</span>
                  <span className="badge">{actionLabel(candidate.recommended_action)}</span>
                </div>
              </div>

              <div className="sourceCandidateMetrics">
                <span>Тема: {candidate.topic || "не задана"}</span>
                <span>Проверено: {candidate.tested_articles}</span>
                <span>Релевантно: {candidate.relevant_articles}</span>
                <span>Средний score: {candidate.avg_score ?? "—"}</span>
                <span>Шум: {candidate.noise_count}</span>
              </div>

              {candidate.review_comment ? <div className="sourceCandidateComment">{candidate.review_comment}</div> : null}

              <div className="sourceActions">
                <button type="button" className="ghostButton compactButton" onClick={() => void handleEvaluate(candidate)} disabled={Boolean(pending)}>
                  {pending === "Проверяем в песочнице…" ? pending : "Проверить"}
                </button>
                <button type="button" className="ghostButton compactButton" onClick={() => void handleDecision(candidate, "test_more")} disabled={Boolean(pending)}>
                  Тестировать ещё
                </button>
                <button type="button" className="ghostButton compactButton" onClick={() => void handleDecision(candidate, "pause")} disabled={Boolean(pending)}>
                  В паузу
                </button>
                <button
                  type="button"
                  className="dangerGhostButton compactButton"
                  onClick={() => void handleDecision(candidate, "reject")}
                  disabled={Boolean(pending) || candidate.status === "approved"}
                >
                  Отклонить
                </button>
                <button
                  type="button"
                  className="primaryButton compactButton"
                  onClick={() => void handleApprove(candidate)}
                  disabled={Boolean(pending) || candidate.status === "approved"}
                >
                  {pending === "Создаем источник…" ? pending : candidate.status === "approved" ? "Одобрен" : "Одобрить"}
                </button>
              </div>

              {isExpanded ? (
                <div className="sourceCandidateArticles">
                  {pending && pending !== "Проверяем в песочнице…" && pending !== "Создаем источник…" ? <div className="emptyState">{pending}</div> : null}
                  {(articles[candidate.id] || []).map((article) => (
                    <div className="sourceCandidateArticle" key={article.id}>
                      <div>
                        <a href={article.url} target="_blank" rel="noreferrer">{article.title}</a>
                        <p>{article.summary || article.relevance_reason || article.prefilter_reason || "Без результата обработки"}</p>
                      </div>
                      <div className="sourceCandidateArticleMeta">
                        <span>{article.processing_status}</span>
                        <span>{article.total_score ?? "—"}</span>
                      </div>
                    </div>
                  ))}
                  {!pending && !(articles[candidate.id] || []).length ? (
                    <div className="emptyState">Тестовых материалов пока нет. Запустите проверку.</div>
                  ) : null}
                </div>
              ) : null}
            </article>
          );
        })}
      </section>
    </section>
  );
}
