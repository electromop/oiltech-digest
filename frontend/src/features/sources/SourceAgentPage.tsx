import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../../api/client";
import {
  createAgentMemory,
  enqueueSourceDiscoveryLoop,
  enqueueSourceDiscoveryPlan,
  getSourceDiscoveryPlan,
  getSourceDiscoveryReadiness,
  listAgentActions,
  listAgentMemory,
  listAgentRuns,
  listQueryMemory,
  listSourceDiscoveryQuality,
  updateAgentMemory,
} from "../../api/sources";
import type {
  AgentAction,
  AgentMemory,
  AgentPlan,
  AgentPlanAction,
  AgentRun,
  QueryMemoryRow,
  SourceDiscoveryPlanPayload,
  SourceDiscoveryQualityRow,
  SourceDiscoveryReadiness,
} from "../../api/types";

type Props = {
  onUnauthorized: () => void;
  showToast: (text: string, tone?: "default" | "error") => void;
};

type AgentSettings = Required<Pick<
  SourceDiscoveryPlanPayload,
  "days" | "target_per_topic" | "topic_limit" | "candidate_limit" | "max_actions"
>> & {
  offline: boolean;
  evaluate: boolean;
  persist_memory: boolean;
};

type NumericSettingKey = "days" | "target_per_topic" | "topic_limit" | "candidate_limit" | "max_actions";

const DEFAULT_SETTINGS: AgentSettings = {
  days: 30,
  target_per_topic: 10,
  topic_limit: 5,
  candidate_limit: 10,
  max_actions: 5,
  offline: true,
  evaluate: true,
  persist_memory: true,
};

const ACTION_LABELS: Record<AgentPlanAction["action_type"], string> = {
  discover_sources: "Найти источники",
  review_source_candidate: "Проверить кандидата",
  recheck_source: "Перепроверить источник",
  tune_source_frequency: "Настроить частоту",
};

const POLICY_LABELS: Record<NonNullable<AgentPlanAction["policy_decision"]>, string> = {
  auto: "Авто",
  human_review: "Решает человек",
  blocked: "Запрещено",
};

function actionTarget(action: AgentPlanAction) {
  const frequency = action.recommended_frequency ? `: ${action.recommended_frequency}` : "";
  return action.topic || (action.source_name ? `${action.source_name}${frequency}` : "") || action.url || (action.candidate_id ? `Кандидат #${action.candidate_id}` : "Без цели");
}

function memoryTypeLabel(type: string) {
  if (type === "topic") return "Тема";
  if (type === "domain") return "Домен";
  if (type === "source") return "Источник";
  if (type === "plan") return "План";
  if (type === "rule") return "Правило";
  if (type === "query") return "Запрос";
  return type;
}

function factSummary(memory: AgentMemory) {
  const facts = memory.facts_json || {};
  const parts = [
    typeof facts.gap === "number" ? `дефицит ${facts.gap}` : null,
    typeof facts.signals === "number" ? `сигналов ${facts.signals}` : null,
    typeof facts.relevant_articles === "number" ? `релевантно ${facts.relevant_articles}` : null,
    typeof facts.avg_score === "number" ? `score ${facts.avg_score}` : null,
    typeof facts.recommended_action === "string" ? String(facts.recommended_action) : null,
    typeof facts.found_candidates === "number" ? `кандидатов ${facts.found_candidates}` : null,
    typeof facts.topic === "string" ? String(facts.topic) : null,
  ].filter(Boolean);
  return parts.join(" · ") || "Факты пока не накоплены";
}

function actionSummary(action: AgentAction) {
  const output = action.output_json || {};
  const candidates = output.candidates;
  const evaluated = output.evaluated;
  const queued = output.queued;
  const score = output.score;
  const memoryIds = output.memory_ids;
  const duration = action.duration_ms ? `${action.duration_ms} мс` : null;
  const parts = [
    typeof candidates === "number" ? `кандидатов ${candidates}` : null,
    typeof evaluated === "number" ? `оценено ${evaluated}` : null,
    typeof queued === "object" && queued && "queued" in queued ? `задач ${String((queued as { queued?: unknown }).queued)}` : null,
    typeof score === "number" ? `оценка памяти ${score}` : null,
    Array.isArray(memoryIds) ? `записей памяти ${memoryIds.length}` : null,
    duration,
  ].filter(Boolean);
  return parts.join(" · ") || action.task_topic || "Детали записаны в журнал действий";
}

function formatDate(value: string | null) {
  if (!value) return "—";
  return new Date(value).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function readinessBudget(readiness: SourceDiscoveryReadiness | null) {
  const budget = readiness?.checks?.budget;
  const usage = budget?.usage && typeof budget.usage === "object" ? budget.usage as Record<string, unknown> : {};
  const limits = budget?.limits && typeof budget.limits === "object" ? budget.limits as Record<string, unknown> : {};
  const label = (used: unknown, limit: unknown) => `${Number(used || 0)} / ${Number(limit || 0) || "∞"}`;
  return {
    loopRuns: label(usage.loop_runs, limits.loop_runs),
    candidates: label(usage.candidates_created, limits.candidates_created),
    evaluations: label(usage.candidate_evaluations, limits.candidate_evaluations),
  };
}

function runSummary(run: AgentRun) {
  const result = run.result_json || {};
  const queued = result.queued;
  const queuedCount = typeof queued === "object" && queued && "queued" in queued ? String((queued as { queued?: unknown }).queued) : "0";
  const actions = Array.isArray(result.actions) ? result.actions.length : run.action_count;
  return `действий ${actions} · задач ${queuedCount} · ok ${run.ok_job_count} · failed ${run.failed_job_count}`;
}

function percent(value: number | null | undefined) {
  return `${Math.round(Number(value || 0) * 100)}%`;
}

function factNumber(memory: AgentMemory, key: string) {
  const value = memory.facts_json?.[key];
  return typeof value === "number" ? value : 0;
}

function factString(memory: AgentMemory, key: string) {
  const value = memory.facts_json?.[key];
  return typeof value === "string" ? value : "";
}

type LoopIteration = {
  iteration?: number;
  action_count?: number;
  auto_action_count?: number;
  human_review_count?: number;
  observations?: Array<Record<string, unknown>>;
};

export function SourceAgentPage({ onUnauthorized, showToast }: Props) {
  const [settings, setSettings] = useState<AgentSettings>(DEFAULT_SETTINGS);
  const [plan, setPlan] = useState<AgentPlan | null>(null);
  const [memory, setMemory] = useState<AgentMemory[]>([]);
  const [actions, setActions] = useState<AgentAction[]>([]);
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [queryMemory, setQueryMemory] = useState<QueryMemoryRow[]>([]);
  const [mutedQueryMemory, setMutedQueryMemory] = useState<QueryMemoryRow[]>([]);
  const [strategyMemory, setStrategyMemory] = useState<AgentMemory[]>([]);
  const [readiness, setReadiness] = useState<SourceDiscoveryReadiness | null>(null);
  const [topicQuality, setTopicQuality] = useState<SourceDiscoveryQualityRow[]>([]);
  const [domainQuality, setDomainQuality] = useState<SourceDiscoveryQualityRow[]>([]);
  const [memoryType, setMemoryType] = useState("");
  const [ruleType, setRuleType] = useState<"domain" | "topic">("domain");
  const [ruleSubject, setRuleSubject] = useState("");
  const [ruleStatus, setRuleStatus] = useState<"active" | "rejected">("rejected");
  const [creatingRule, setCreatingRule] = useState(false);
  const [actionType, setActionType] = useState("");
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [loadingPlan, setLoadingPlan] = useState(false);
  const [loadingMemory, setLoadingMemory] = useState(false);
  const [loadingActions, setLoadingActions] = useState(false);
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [loadingQuality, setLoadingQuality] = useState(false);
  const [enqueueing, setEnqueueing] = useState(false);
  const [loopEnqueueing, setLoopEnqueueing] = useState(false);
  const [updatingMemoryId, setUpdatingMemoryId] = useState<number | null>(null);

  useEffect(() => {
    void Promise.all([loadPlan(), loadMemory(), loadRuns(), loadActions(), loadQuality()]);
  }, []);

  const counters = useMemo(() => {
    const actions = plan?.actions ?? [];
    return {
      actions: actions.length,
      auto: plan?.policy?.auto ?? actions.filter((item) => item.policy_decision === "auto").length,
      review: plan?.policy?.human_review ?? actions.filter((item) => item.requires_human_approval).length,
      memory: memory.length,
      history: actions.length,
      runs: runs.length,
      candidates: plan?.learning?.candidates ?? 0,
      approved: plan?.learning?.approved ?? 0,
      rejected: plan?.learning?.rejected ?? 0,
      approvalRate: Math.round((plan?.learning?.approval_rate ?? 0) * 100),
    };
  }, [actions.length, memory.length, plan, runs.length]);

  const latestLoopRun = useMemo(() => runs.find((run) => run.kind === "source_discovery_loop") ?? null, [runs]);
  const latestLoopResult = latestLoopRun?.result_json as {
    iterations?: LoopIteration[];
    total_candidates?: number;
    empty_iterations?: number;
    terminal_reason?: string;
  } | undefined;
  const latestLoopIterations = Array.isArray(latestLoopResult?.iterations) ? latestLoopResult.iterations : [];
  const budget = readinessBudget(readiness);

  function handleError(error: unknown, fallback: string) {
    if (error instanceof ApiError && error.status === 401) {
      onUnauthorized();
      return;
    }
    showToast(error instanceof Error ? error.message : fallback, "error");
  }

  async function loadPlan() {
    try {
      setLoadingPlan(true);
      setPlan(await getSourceDiscoveryPlan(settings));
    } catch (error) {
      handleError(error, "Не удалось построить план агента");
    } finally {
      setLoadingPlan(false);
    }
  }

  async function loadMemory(nextType = memoryType) {
    try {
      setLoadingMemory(true);
      setMemory(await listAgentMemory({
        memory_type: nextType || undefined,
        status: "active",
        limit: 100,
      }));
    } catch (error) {
      handleError(error, "Не удалось загрузить память агента");
    } finally {
      setLoadingMemory(false);
    }
  }

  async function loadActions(nextType = actionType, nextRunId = selectedRunId) {
    try {
      setLoadingActions(true);
      setActions(await listAgentActions({
        action_type: nextType || undefined,
        run_id: nextRunId || undefined,
        limit: 50,
      }));
    } catch (error) {
      handleError(error, "Не удалось загрузить историю агента");
    } finally {
      setLoadingActions(false);
    }
  }

  async function loadRuns() {
    try {
      setLoadingRuns(true);
      setRuns(await listAgentRuns({ limit: 20 }));
    } catch (error) {
      handleError(error, "Не удалось загрузить циклы агента");
    } finally {
      setLoadingRuns(false);
    }
  }

  async function loadQuality() {
    try {
      setLoadingQuality(true);
      const [topics, domains, queries, mutedQueries, strategies, readinessReport] = await Promise.all([
        listSourceDiscoveryQuality({ group_by: "topic", limit: 8 }),
        listSourceDiscoveryQuality({ group_by: "domain", limit: 8 }),
        listQueryMemory({ limit: 8, status: "active" }),
        listQueryMemory({ limit: 8, status: "muted" }),
        listAgentMemory({ memory_type: "strategy", status: "", limit: 50 }),
        getSourceDiscoveryReadiness(),
      ]);
      setTopicQuality(topics);
      setDomainQuality(domains);
      setQueryMemory(queries);
      setMutedQueryMemory(mutedQueries);
      setStrategyMemory(strategies);
      setReadiness(readinessReport);
    } catch (error) {
      handleError(error, "Не удалось загрузить качество агента");
    } finally {
      setLoadingQuality(false);
    }
  }

  async function handleEnqueue() {
    try {
      setEnqueueing(true);
      const result = await enqueueSourceDiscoveryPlan(settings);
      showToast(`План поставлен в очередь: job #${result.job.id}`);
      await Promise.all([loadRuns(), loadActions()]);
    } catch (error) {
      handleError(error, "Не удалось поставить план в очередь");
    } finally {
      setEnqueueing(false);
    }
  }

  async function handleLoopEnqueue() {
    try {
      setLoopEnqueueing(true);
      const result = await enqueueSourceDiscoveryLoop({
        ...settings,
        goal: "Найти новые полезные источники сигналов",
        max_iterations: 3,
        fetch_inspection: false,
        dry_run: false,
        auto_evaluate: true,
        article_limit: 5,
        max_daily_loop_runs: 4,
        max_daily_candidates: 100,
        max_daily_evaluations: 100,
      });
      showToast(`Agent loop поставлен в очередь: job #${result.job.id}`);
      await Promise.all([loadRuns(), loadActions()]);
    } catch (error) {
      handleError(error, "Не удалось поставить agent loop в очередь");
    } finally {
      setLoopEnqueueing(false);
    }
  }

  async function handleMemoryStatus(memoryId: number, status: "active" | "muted" | "rejected") {
    try {
      setUpdatingMemoryId(memoryId);
      await updateAgentMemory(memoryId, { status });
      showToast("Статус памяти обновлен");
      await Promise.all([loadQuality(), loadMemory()]);
    } catch (error) {
      handleError(error, "Не удалось обновить память агента");
    } finally {
      setUpdatingMemoryId(null);
    }
  }

  async function handleCreateRule() {
    const subject = ruleSubject.trim();
    if (!subject) {
      showToast("Укажите домен или тему", "error");
      return;
    }
    try {
      setCreatingRule(true);
      await createAgentMemory({
        memory_type: ruleType,
        subject,
        status: ruleStatus,
        score: ruleStatus === "active" ? 85 : 0,
        facts: { control_plane: true },
      });
      setRuleSubject("");
      showToast(ruleStatus === "rejected" ? "Запрет добавлен" : "Приоритет добавлен");
      await Promise.all([loadMemory(ruleType), loadQuality(), loadActions()]);
      setMemoryType(ruleType);
    } catch (error) {
      handleError(error, "Не удалось добавить правило");
    } finally {
      setCreatingRule(false);
    }
  }

  function setNumber(key: NumericSettingKey, value: string) {
    const parsed = Number(value);
    setSettings((prev) => ({ ...prev, [key]: Number.isFinite(parsed) ? parsed : prev[key] }));
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Агент источников</h1>
        </div>
        <div className="panelActions">
          <button type="button" className="ghostButton compactButton" onClick={() => void loadPlan()} disabled={loadingPlan}>
            {loadingPlan ? "Строим…" : "Построить план"}
          </button>
          <button type="button" className="primaryButton compactButton" onClick={() => void handleEnqueue()} disabled={enqueueing}>
            {enqueueing ? "Ставим…" : "Поставить в очередь"}
          </button>
          <button type="button" className="primaryButton compactButton" onClick={() => void handleLoopEnqueue()} disabled={loopEnqueueing}>
            {loopEnqueueing ? "Ставим…" : "Запустить loop"}
          </button>
        </div>
      </header>

      <section className="sourceCandidateStats">
        <div className="sourceStatCard">
          <span className="sourceStatValue">{counters.actions}</span>
          <span className="sourceStatLabel">Действий в плане</span>
        </div>
        <div className="sourceStatCard success">
          <span className="sourceStatValue">{counters.auto}</span>
          <span className="sourceStatLabel">Автоматически</span>
        </div>
        <div className="sourceStatCard warning">
          <span className="sourceStatValue">{counters.review}</span>
          <span className="sourceStatLabel">Нужно решение</span>
        </div>
        <div className="sourceStatCard">
          <span className="sourceStatValue">{counters.runs}</span>
          <span className="sourceStatLabel">Циклов</span>
        </div>
      </section>

      <section className="agentLearningStrip" aria-label="Метрики обучения агента">
        <div>
          <span>{counters.candidates}</span>
          <p>кандидатов в памяти</p>
        </div>
        <div>
          <span>{counters.approved}</span>
          <p>одобрено человеком</p>
        </div>
        <div>
          <span>{counters.rejected}</span>
          <p>отклонено человеком</p>
        </div>
        <div>
          <span>{counters.approvalRate}%</span>
          <p>доля одобрений</p>
        </div>
      </section>

      <section className="panel agentSettingsPanel">
        <div className="panelHeader">
          <h2>Параметры цикла</h2>
          <span className="badge">{settings.days} дней</span>
        </div>
        <div className="agentSettingsGrid">
          <label className="field">
            <span>Период</span>
            <input type="number" min={1} max={365} value={settings.days} onChange={(event) => setNumber("days", event.target.value)} />
          </label>
          <label className="field">
            <span>Цель на тему</span>
            <input type="number" min={1} max={100} value={settings.target_per_topic} onChange={(event) => setNumber("target_per_topic", event.target.value)} />
          </label>
          <label className="field">
            <span>Тем</span>
            <input type="number" min={1} max={50} value={settings.topic_limit} onChange={(event) => setNumber("topic_limit", event.target.value)} />
          </label>
          <label className="field">
            <span>Кандидатов</span>
            <input type="number" min={1} max={50} value={settings.candidate_limit} onChange={(event) => setNumber("candidate_limit", event.target.value)} />
          </label>
          <label className="field">
            <span>Действий</span>
            <input type="number" min={1} max={50} value={settings.max_actions} onChange={(event) => setNumber("max_actions", event.target.value)} />
          </label>
          <label className="checkField">
            <input type="checkbox" checked={settings.offline} onChange={(event) => setSettings((prev) => ({ ...prev, offline: event.target.checked }))} />
            <span>Без ИИ для поисковых запросов</span>
          </label>
          <label className="checkField">
            <input type="checkbox" checked={settings.evaluate} onChange={(event) => setSettings((prev) => ({ ...prev, evaluate: event.target.checked }))} />
            <span>Оценивать кандидатов</span>
          </label>
          <label className="checkField">
            <input type="checkbox" checked={settings.persist_memory} onChange={(event) => setSettings((prev) => ({ ...prev, persist_memory: event.target.checked }))} />
            <span>Обновлять память</span>
          </label>
        </div>
      </section>

      <section className="agentLayout">
        <article className="panel">
          <div className="panelHeader">
            <h2>План</h2>
            <span className="badge">{plan ? `${plan.duration_ms} мс` : "нет данных"}</span>
          </div>
          <div className="agentActionList">
            {loadingPlan && !plan ? <div className="emptyState">Строим план…</div> : null}
            {!loadingPlan && !plan ? <div className="emptyState">План еще не построен.</div> : null}
            {plan?.actions.map((action, index) => (
              <div className="agentActionCard" key={`${action.action_type}-${index}-${actionTarget(action)}`}>
                <div className="agentActionScore">{Math.round(action.priority)}</div>
                <div>
                  <div className="agentActionTitleRow">
                    <div className="agentActionTitle">{ACTION_LABELS[action.action_type] ?? action.action_type}</div>
                    <span className={`agentPolicyBadge ${action.policy_decision ?? "blocked"}`}>
                      {POLICY_LABELS[action.policy_decision ?? "blocked"]}
                    </span>
                  </div>
                  <div className="agentActionTarget">{actionTarget(action)}</div>
                  {action.query_hints?.length ? (
                    <div className="agentQueryHints">
                      {action.query_hints.map((query) => (
                        <span key={query}>{query}</span>
                      ))}
                    </div>
                  ) : null}
                  <p>{action.reason}</p>
                  {action.policy_reason ? <p className="agentPolicyReason">{action.policy_reason}</p> : null}
                  {action.operator_url ? (
                    <a className="agentOperatorLink" href={action.operator_url}>
                      {action.operator_label || "Открыть"}
                    </a>
                  ) : null}
                </div>
              </div>
            ))}
            {plan && !plan.actions.length ? <div className="emptyState">Агент не нашел полезных действий по текущим параметрам.</div> : null}
          </div>
        </article>

        <article className="panel">
          <div className="panelHeader">
            <h2>Память</h2>
            <button type="button" className="ghostButton compactButton" onClick={() => void loadMemory()} disabled={loadingMemory}>
              {loadingMemory ? "Обновляем…" : "Обновить"}
            </button>
          </div>
          <div className="sourceCandidateFilters compactFilters">
            <label className="field">
              <span>Тип</span>
              <select
                value={memoryType}
                onChange={(event) => {
                  setMemoryType(event.target.value);
                  void loadMemory(event.target.value);
                }}
              >
                <option value="">Все</option>
                <option value="topic">Темы</option>
                <option value="domain">Домены</option>
                <option value="source">Источники</option>
                <option value="query">Запросы</option>
                <option value="strategy">Стратегии</option>
              </select>
            </label>
          </div>
          <div className="agentRuleForm">
            <label className="field">
              <span>Правило</span>
              <select value={ruleType} onChange={(event) => setRuleType(event.target.value as "domain" | "topic")}>
                <option value="domain">Домен</option>
                <option value="topic">Тема</option>
              </select>
            </label>
            <label className="field">
              <span>Значение</span>
              <input value={ruleSubject} onChange={(event) => setRuleSubject(event.target.value)} placeholder={ruleType === "domain" ? "example.com" : "бурение"} />
            </label>
            <label className="field">
              <span>Режим</span>
              <select value={ruleStatus} onChange={(event) => setRuleStatus(event.target.value as "active" | "rejected")}>
                <option value="rejected">Запретить</option>
                <option value="active">Приоритет</option>
              </select>
            </label>
            <button type="button" className="primaryButton compactButton" onClick={() => void handleCreateRule()} disabled={creatingRule}>
              {creatingRule ? "Добавляем…" : "Добавить"}
            </button>
          </div>
          <div className="agentMemoryList">
            {loadingMemory && !memory.length ? <div className="emptyState">Загружаем память…</div> : null}
            {!loadingMemory && !memory.length ? <div className="emptyState">Память пока пустая. Запустите планирование.</div> : null}
            {memory.map((item) => (
              <div className="agentMemoryRow" key={item.id}>
                <div className="agentMemoryMeta">
                  <span className="badge">{memoryTypeLabel(item.memory_type)}</span>
                  <span>{Math.round(Number(item.score || 0))}</span>
                </div>
                <div>
                  <strong>{item.subject}</strong>
                  <p>{factSummary(item)}</p>
                </div>
              </div>
            ))}
          </div>
        </article>
        <article className="panel">
          <div className="panelHeader">
            <h2>Пустые запросы</h2>
            <span className="badge">{mutedQueryMemory.length} отброшено</span>
          </div>
          <div className="agentQueryMemoryList">
            {!loadingQuality && !mutedQueryMemory.length ? <div className="emptyState">Пока нет формулировок без результата.</div> : null}
            {mutedQueryMemory.map((row) => (
              <div className="agentQueryMemoryRow muted" key={`${row.topic}-${row.query}`}>
                <strong>{row.query}</strong>
                <span>{row.topic || "без темы"}</span>
                <span>0</span>
                <span>{row.found_candidates} канд.</span>
                <span>не повторять</span>
              </div>
            ))}
          </div>
        </article>
      </section>

      <section className="agentWideLayout">
        <article className="panel">
          <div className="panelHeader">
            <h2>Циклы</h2>
            <button type="button" className="ghostButton compactButton" onClick={() => void loadRuns()} disabled={loadingRuns}>
              {loadingRuns ? "Обновляем…" : "Обновить"}
            </button>
          </div>
          <div className="agentRunList">
            {loadingRuns && !runs.length ? <div className="emptyState">Загружаем циклы…</div> : null}
            {!loadingRuns && !runs.length ? <div className="emptyState">Циклов пока нет. Поставьте план в очередь.</div> : null}
            {runs.map((run) => (
              <button
                type="button"
                className={selectedRunId === run.id ? "agentRunRow active" : "agentRunRow"}
                key={run.id}
                onClick={() => {
                  const next = selectedRunId === run.id ? null : run.id;
                  setSelectedRunId(next);
                  void loadActions(actionType, next);
                }}
              >
                <span className={`agentRunStatus ${run.status}`}>{run.status}</span>
                <span>
                  <strong>Цикл #{run.id}</strong>
                  <p>{runSummary(run)}</p>
                </span>
                <span className="agentRunDate">{formatDate(run.started_at || run.created_at)}</span>
              </button>
            ))}
          </div>
        </article>
      </section>

      <section className="agentWideLayout">
        <article className="panel">
          <div className="panelHeader">
            <h2>Последний loop</h2>
            <span className="badge">{latestLoopRun ? `#${latestLoopRun.id}` : "нет запусков"}</span>
          </div>
          {!latestLoopRun ? <div className="emptyState">Loop еще не запускался.</div> : null}
          {latestLoopRun ? (
            <div className="agentLoopReport">
              <div className="agentLoopSummary">
                <div>
                  <span>{latestLoopRun.status}</span>
                  <p>статус</p>
                </div>
                <div>
                  <span>{latestLoopResult?.total_candidates ?? 0}</span>
                  <p>кандидатов</p>
                </div>
                <div>
                  <span>{latestLoopIterations.length}</span>
                  <p>итераций</p>
                </div>
                <div>
                  <span>{latestLoopResult?.terminal_reason || "—"}</span>
                  <p>причина остановки</p>
                </div>
              </div>
              <div className="agentLoopIterationList">
                {!latestLoopIterations.length ? <div className="emptyState">Итерации пока не записаны.</div> : null}
                {latestLoopIterations.map((iteration, index) => (
                  <div className="agentLoopIteration" key={`${iteration.iteration ?? index}`}>
                    <div className="agentLoopIterationHead">
                      <strong>Итерация {iteration.iteration ?? index + 1}</strong>
                      <span>авто {iteration.auto_action_count ?? 0} · вручную {iteration.human_review_count ?? 0}</span>
                    </div>
                    <div className="agentLoopObservationList">
                      {(iteration.observations || []).map((observation, obsIndex) => (
                        <div className="agentLoopObservation" key={`${String(observation.topic || "")}-${obsIndex}`}>
                          <strong>{String(observation.topic || "без темы")}</strong>
                          <span>{String(observation.query_strategy || "—")}</span>
                          <span>{Number(observation.candidate_count || 0)} канд.</span>
                          <span>{Number(observation.evaluated_count || 0)} оцен.</span>
                          <span>{Number(observation.evaluation_jobs || 0)} в очереди</span>
                          <span>{Number(observation.relevant_articles || 0)} релев.</span>
                          <span>score {observation.avg_score === undefined || observation.avg_score === null ? "—" : String(observation.avg_score)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </article>
      </section>

      <section className="agentWideLayout">
        <article className="panel">
          <div className="panelHeader">
            <h2>Память стратегий</h2>
            <span className="badge">{strategyMemory.length} записей</span>
          </div>
          <div className="agentStrategyMemoryList">
            {!loadingQuality && !strategyMemory.length ? <div className="emptyState">Стратегии пока не накоплены.</div> : null}
            {strategyMemory.map((item) => (
              <div className={`agentStrategyMemoryRow ${item.status}`} key={item.id}>
                <div>
                  <strong>{factString(item, "topic") || "без темы"}</strong>
                  <p>{factString(item, "strategy") || item.subject} · {item.status} · score {Math.round(Number(item.score || 0))}</p>
                </div>
                <span>{factNumber(item, "candidate_count")} канд.</span>
                <span>{factNumber(item, "evaluated_count")} оцен.</span>
                <span>{factNumber(item, "evaluation_jobs")} в очереди</span>
                <span>{factNumber(item, "relevant_articles")} релев.</span>
                <span>score {factNumber(item, "avg_score") || "—"}</span>
                <div className="agentStrategyActions">
                  {item.status !== "active" ? (
                    <button type="button" className="ghostButton compactButton" onClick={() => void handleMemoryStatus(item.id, "active")} disabled={updatingMemoryId === item.id}>
                      Вернуть
                    </button>
                  ) : (
                    <button type="button" className="ghostButton compactButton" onClick={() => void handleMemoryStatus(item.id, "muted")} disabled={updatingMemoryId === item.id}>
                      Приглушить
                    </button>
                  )}
                  <button type="button" className="dangerGhostButton compactButton" onClick={() => void handleMemoryStatus(item.id, "rejected")} disabled={updatingMemoryId === item.id}>
                    Запретить
                  </button>
                </div>
              </div>
            ))}
          </div>
        </article>
      </section>

      <section className="agentQualityLayout">
        <article className="panel">
          <div className="panelHeader">
            <h2>Качество по темам</h2>
            <button type="button" className="ghostButton compactButton" onClick={() => void loadQuality()} disabled={loadingQuality}>
              {loadingQuality ? "Обновляем…" : "Обновить"}
            </button>
          </div>
          <div className="agentQualityList">
            {!loadingQuality && !topicQuality.length ? <div className="emptyState">Нет данных по темам.</div> : null}
            {topicQuality.map((row) => (
              <div className="agentQualityRow" key={row.subject}>
                <strong>{row.subject}</strong>
                <span>{row.candidates} канд.</span>
                <span>{percent(row.approval_rate)} одобр.</span>
                <span>{percent(row.relevance_rate)} релев.</span>
                <span>{row.approved}/{row.rejected}</span>
              </div>
            ))}
          </div>
        </article>

        <article className="panel">
          <div className="panelHeader">
            <h2>Качество по доменам</h2>
            <span className="badge">top {domainQuality.length}</span>
          </div>
          <div className="agentQualityList">
            {!loadingQuality && !domainQuality.length ? <div className="emptyState">Нет данных по доменам.</div> : null}
            {domainQuality.map((row) => (
              <div className="agentQualityRow" key={row.subject}>
                <strong>{row.subject}</strong>
                <span>{row.candidates} канд.</span>
                <span>{percent(row.approval_rate)} одобр.</span>
                <span>{percent(row.relevance_rate)} релев.</span>
                <span>{row.approved}/{row.rejected}</span>
              </div>
            ))}
          </div>
        </article>
      </section>

      <section className="agentWideLayout">
        <article className="panel">
          <div className="panelHeader">
            <h2>Лучшие запросы</h2>
            <span className="badge">{queryMemory.length} формулировок</span>
          </div>
          <div className="agentQueryMemoryList">
            {!loadingQuality && !queryMemory.length ? <div className="emptyState">Память запросов пока пустая.</div> : null}
            {queryMemory.map((row) => (
              <div className="agentQueryMemoryRow" key={`${row.topic}-${row.query}`}>
                <strong>{row.query}</strong>
                <span>{row.topic || "без темы"}</span>
                <span>{Math.round(row.score)}</span>
                <span>{row.found_candidates} канд.</span>
                <span>{percent(row.relevance_rate)} релев.</span>
              </div>
            ))}
          </div>
        </article>
      </section>

      <section className="agentWideLayout">
        <article className="panel">
          <div className="panelHeader">
            <h2>История</h2>
            <button type="button" className="ghostButton compactButton" onClick={() => void loadActions()} disabled={loadingActions}>
              {loadingActions ? "Обновляем…" : "Обновить"}
            </button>
          </div>
          <div className="sourceCandidateFilters compactFilters">
            <label className="field">
              <span>Действие</span>
              <select
                value={actionType}
                onChange={(event) => {
                  setActionType(event.target.value);
                  void loadActions(event.target.value, selectedRunId);
                }}
              >
                <option value="">Все</option>
                <option value="source_discovery_plan_built">План построен</option>
                <option value="discover_sources_finished">Поиск завершен</option>
                <option value="evaluate_source_candidate_finished">Кандидат оценен</option>
                <option value="approve_source_candidate">Кандидат одобрен</option>
                <option value="source_candidate_learning">Агент обучился</option>
                <option value="source_discovery_loop_iteration">Loop-итерация</option>
                <option value="update_agent_memory">Память изменена</option>
              </select>
            </label>
            {selectedRunId ? (
              <button
                type="button"
                className="ghostButton compactButton"
                onClick={() => {
                  setSelectedRunId(null);
                  void loadActions(actionType, null);
                }}
              >
                Все циклы
              </button>
            ) : null}
          </div>
          <div className="agentHistoryList">
            {loadingActions && !actions.length ? <div className="emptyState">Загружаем историю…</div> : null}
            {!loadingActions && !actions.length ? <div className="emptyState">История пока пустая.</div> : null}
            {actions.map((item) => (
              <div className={`agentHistoryRow ${item.decision_tone || "neutral"}`} key={item.id}>
                <div className="agentHistoryTime">{formatDate(item.created_at)}</div>
                <div>
                  <strong>{item.decision_title || item.action_type}</strong>
                  <p>{item.decision_summary || actionSummary(item)}</p>
                  <span className="agentHistoryActionType">{item.action_type}</span>
                </div>
                <div className="agentHistoryMeta">
                  <span>{item.task_kind || "manual"}</span>
                  <span>{item.task_status || "—"}</span>
                </div>
              </div>
            ))}
          </div>
        </article>
      </section>

      <section className="panel agentOpsPanel">
        <div>
          <h2>Операционный контур</h2>
          <p>Для автономного запуска scheduler должен ставить `enqueue-agent-plan`, а `jobs-worker` выполнять план и дочерние discovery-задачи.</p>
        </div>
        <div className={readiness?.status === "blocked" ? "agentReadinessStatus blocked" : readiness?.status === "ready" ? "agentReadinessStatus ready" : "agentReadinessStatus degraded"}>
          <strong>{readiness ? readiness.status : "loading"}</strong>
          <span>{readiness?.ok ? "Критичных блокеров нет" : "Есть блокеры или предупреждения"}</span>
        </div>
        <div className="agentBudgetStrip">
          <div>
            <strong>{budget.loopRuns}</strong>
            <span>loop за день</span>
          </div>
          <div>
            <strong>{budget.candidates}</strong>
            <span>кандидатов</span>
          </div>
          <div>
            <strong>{budget.evaluations}</strong>
            <span>AI-оценок</span>
          </div>
        </div>
        {readiness?.issues.length ? (
          <div className="agentReadinessList">
            {readiness.issues.slice(0, 4).map((issue) => (
              <div className="agentReadinessIssue" key={issue.code}>
                <span>{issue.severity}</span>
                <strong>{issue.message}</strong>
              </div>
            ))}
          </div>
        ) : null}
        <div className="agentOpsGrid">
          {(readiness?.recommendations.length ? readiness.recommendations.slice(0, 4) : [
            "SOURCE_DISCOVERY_ENABLED=1",
            "SOURCE_DISCOVERY_PLANNER_ENABLED=1",
            "SOURCE_DISCOVERY_SEARCH_PROVIDER=brave",
            "EXTERNAL_WORKERS_ENABLED=1",
          ]).map((item) => <span key={item}>{item}</span>)}
        </div>
      </section>
    </section>
  );
}
