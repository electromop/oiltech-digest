import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../../api/client";
import {
  approveSourceCandidate,
  createAgentMemory,
  diagnoseSourceJob,
  discoverSourceCandidate,
  dryRunSourceDiscoveryLoop,
  enqueueSourceDiscoveryLoop,
  enqueueSourceDiscoveryPlan,
  getSourceDiscoveryEvaluation,
  getSourceDiscoveryPlan,
  getSourceDiscoveryReadiness,
  listSourceCandidateTriage,
  listAgentActions,
  listAgentMemory,
  listAgentRuns,
  listQueryMemory,
  listSourceDiscoveryQuality,
  updateSource,
  updateSourceCandidate,
  updateAgentMemory,
} from "../../api/sources";
import type {
  AgentAction,
  AgentMemory,
  AgentPlan,
  AgentPlanAction,
  AgentRun,
  QueryMemoryRow,
  SourceCandidateTriageRow,
  SourceDiscoveryEvaluation,
  SourceDiscoveryLoopResult,
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
  audit_existing_source: "Аудит источника",
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
  if (type === "strategy") return "Стратегия";
  if (type === "topic_query_domain") return "Связка";
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

function readinessLabel(readiness: SourceDiscoveryReadiness | null) {
  if (!readiness) {
    return {
      title: "Проверяем контур",
      detail: "Загружаем состояние поиска, очередей и внешнего AI.",
      tone: "neutral",
    };
  }
  if (readiness.status === "ready") {
    return {
      title: "Готов к автономной работе",
      detail: "Поиск, очереди и лимиты настроены. Агент может запускаться по расписанию.",
      tone: "good",
    };
  }
  if (readiness.status === "blocked") {
    return {
      title: "Есть блокер",
      detail: readiness.issues.find((issue) => issue.severity === "blocker")?.message || "Нужно исправить настройки перед запуском.",
      tone: "bad",
    };
  }
  return {
    title: "Работает в ограниченном режиме",
    detail: readiness.issues[0]?.message || "Часть автономных возможностей выключена.",
    tone: "warning",
  };
}

function runSummary(run: AgentRun) {
  const result = run.result_json || {};
  const queued = result.queued;
  const queuedCount = typeof queued === "object" && queued && "queued" in queued ? String((queued as { queued?: unknown }).queued) : "0";
  const actions = Array.isArray(result.actions) ? result.actions.length : run.action_count;
  return `действий ${actions} · задач ${queuedCount} · ok ${run.ok_job_count} · failed ${run.failed_job_count}`;
}

function latestLoopHumanSummary(run: AgentRun | null) {
  if (!run) {
    return {
      title: "Loop еще не запускался",
      detail: "Запустите цикл, чтобы агент построил план, выбрал темы и попробовал найти источники.",
      metric: "0",
    };
  }
  const result = run.result_json || {};
  const iterations = Array.isArray(result.iterations) ? result.iterations.length : 0;
  const totalCandidates = typeof result.total_candidates === "number" ? result.total_candidates : 0;
  const reason = typeof result.terminal_reason === "string" && result.terminal_reason ? result.terminal_reason : "завершен штатно";
  return {
    title: `Последний запуск #${run.id}: ${run.status}`,
    detail: `${iterations} итераций · ${totalCandidates} кандидатов · ${reason}`,
    metric: String(totalCandidates),
  };
}

function candidateActionLabel(candidate: SourceCandidateTriageRow) {
  if (candidate.recommended_action === "add") return "Можно добавить";
  if (candidate.recommended_action === "test_more") return "Нужно проверить";
  if (candidate.recommended_action === "reject") return "Отклонить";
  return "Нужно решение";
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

function nestedFactNumber(memory: AgentMemory, group: string, key: string) {
  const facts = memory.facts_json?.[group];
  if (!facts || typeof facts !== "object") return 0;
  const value = (facts as Record<string, unknown>)[key];
  return typeof value === "number" ? value : 0;
}

function memoryReason(memory: AgentMemory) {
  const reason = memory.facts_json?.last_reason;
  if (typeof reason === "string" && reason.trim()) return reason;
  const kind = memory.facts_json?.decision_kind;
  if (typeof kind === "string" && kind.trim()) return kind;
  return factSummary(memory);
}

function comboTitle(memory: AgentMemory) {
  const topic = factString(memory, "topic");
  const query = factString(memory, "query");
  const domain = factString(memory, "domain");
  if (topic || query || domain) {
    return `${topic || "без темы"} · ${domain || memory.subject}`;
  }
  return memory.subject;
}

function comboDetail(memory: AgentMemory) {
  const query = factString(memory, "query");
  const strategy = factString(memory, "query_strategy");
  return [query ? `запрос: ${query}` : null, strategy ? `стратегия: ${strategy}` : null].filter(Boolean).join(" · ") || memoryReason(memory);
}

function sourceMemoryId(memory: AgentMemory) {
  const value = memory.facts_json?.source_id;
  return typeof value === "number" ? value : null;
}

function sourceRecommendation(memory: AgentMemory) {
  const label = memory.facts_json?.recommendation_label;
  if (typeof label === "string" && label.trim()) return label;
  const recommendation = memory.facts_json?.recommendation;
  return typeof recommendation === "string" && recommendation.trim() ? recommendation : "Проверить вручную";
}

function sourceProblem(memory: AgentMemory) {
  const problemType = memory.facts_json?.problem_type;
  if (typeof problemType === "string" && problemType.trim() && problemType !== "stable") return problemType;
  const status = memory.facts_json?.status;
  if (typeof status === "string" && status.trim() && status !== "stable") return status;
  return memory.status === "muted" ? "нужна проверка" : "можно усилить";
}

function sourceSeverity(memory: AgentMemory) {
  const severity = memory.facts_json?.severity;
  return typeof severity === "string" && severity.trim() ? severity : "low";
}

function sourceConfidence(memory: AgentMemory) {
  const confidence = memory.facts_json?.confidence;
  return typeof confidence === "string" && confidence.trim() ? confidence : "medium";
}

function sourceReason(memory: AgentMemory) {
  const reasons = memory.facts_json?.reasons;
  if (Array.isArray(reasons) && reasons.length) {
    return reasons.map(String).join(" ");
  }
  const quality = memory.facts_json?.quality_score;
  const found = memory.facts_json?.articles_found;
  const relevant = memory.facts_json?.relevant_count;
  return [`quality ${typeof quality === "number" ? Math.round(quality) : "—"}`, `сигналов ${typeof found === "number" ? found : 0}`, `релевантно ${typeof relevant === "number" ? relevant : 0}`].join(" · ");
}

function valueString(value: unknown) {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return value ? "да" : "нет";
  return "";
}

function actionTopic(action: AgentAction) {
  return valueString(action.input_json.topic) || valueString(action.output_json.topic) || action.task_topic || "";
}

function actionUrl(action: AgentAction) {
  return valueString(action.input_json.url) || valueString(action.input_json.seed_url) || valueString(action.output_json.url);
}

function actionAuditItems(action: AgentAction) {
  const output = action.output_json || {};
  const input = action.input_json || {};
  const items = [
    actionTopic(action) ? `Тема: ${actionTopic(action)}` : null,
    actionUrl(action) ? `URL: ${actionUrl(action)}` : null,
    valueString(input.query) ? `Запрос: ${valueString(input.query)}` : null,
    valueString(output.recommended_action) ? `Рекомендация: ${valueString(output.recommended_action)}` : null,
    valueString(output.next_status) ? `Следующий статус: ${valueString(output.next_status)}` : null,
    valueString(output.reason) ? `Причина: ${valueString(output.reason)}` : null,
    valueString(output.review_comment) ? `Комментарий: ${valueString(output.review_comment)}` : null,
    valueString(output.candidates) ? `Кандидатов: ${valueString(output.candidates)}` : null,
    valueString(output.evaluated) ? `Оценено: ${valueString(output.evaluated)}` : null,
    Array.isArray(output.memory_ids) ? `Память: ${output.memory_ids.length} записей` : null,
  ].filter(Boolean);
  return items.slice(0, 6) as string[];
}

function operatorActionLabel(action: AgentPlanAction) {
  if (action.requires_human_approval) return "Решение человека";
  if (action.policy_decision === "blocked") return "Заблокировано";
  return "Можно выполнить";
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function listValue(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter((item) => item && typeof item === "object").map((item) => item as Record<string, unknown>) : [];
}

function reflectionFromResult(result: Record<string, unknown> | null | undefined) {
  const reflection = objectValue(result?.reflection);
  if (!Object.keys(reflection).length) return null;
  return {
    summary: objectValue(reflection.summary),
    workedTopics: listValue(reflection.worked_topics),
    emptyTopics: listValue(reflection.empty_topics),
    strongStrategies: listValue(reflection.strong_strategies),
    weakStrategies: listValue(reflection.weak_strategies),
    nextHints: listValue(reflection.next_hints),
  };
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
  const [goodDomainMemory, setGoodDomainMemory] = useState<AgentMemory[]>([]);
  const [badDomainMemory, setBadDomainMemory] = useState<AgentMemory[]>([]);
  const [comboMemory, setComboMemory] = useState<AgentMemory[]>([]);
  const [temporaryDomainMemory, setTemporaryDomainMemory] = useState<AgentMemory[]>([]);
  const [watchedSourceMemory, setWatchedSourceMemory] = useState<AgentMemory[]>([]);
  const [triageCandidates, setTriageCandidates] = useState<SourceCandidateTriageRow[]>([]);
  const [readiness, setReadiness] = useState<SourceDiscoveryReadiness | null>(null);
  const [evaluation, setEvaluation] = useState<SourceDiscoveryEvaluation | null>(null);
  const [dryRunReport, setDryRunReport] = useState<SourceDiscoveryLoopResult | null>(null);
  const [topicQuality, setTopicQuality] = useState<SourceDiscoveryQualityRow[]>([]);
  const [domainQuality, setDomainQuality] = useState<SourceDiscoveryQualityRow[]>([]);
  const [memoryType, setMemoryType] = useState("");
  const [ruleType, setRuleType] = useState<"domain" | "topic">("domain");
  const [ruleSubject, setRuleSubject] = useState("");
  const [ruleStatus, setRuleStatus] = useState<"active" | "rejected">("rejected");
  const [seedTopic, setSeedTopic] = useState("цифровые технологии нефтегаз");
  const [seedUrl, setSeedUrl] = useState("");
  const [seedFetchInspection, setSeedFetchInspection] = useState(true);
  const [seedTestParse, setSeedTestParse] = useState(false);
  const [creatingRule, setCreatingRule] = useState(false);
  const [discoveringSeed, setDiscoveringSeed] = useState(false);
  const [actionType, setActionType] = useState("");
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [loadingPlan, setLoadingPlan] = useState(false);
  const [loadingMemory, setLoadingMemory] = useState(false);
  const [loadingActions, setLoadingActions] = useState(false);
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [loadingQuality, setLoadingQuality] = useState(false);
  const [enqueueing, setEnqueueing] = useState(false);
  const [loopEnqueueing, setLoopEnqueueing] = useState(false);
  const [dryRunning, setDryRunning] = useState(false);
  const [updatingMemoryId, setUpdatingMemoryId] = useState<number | null>(null);
  const [quickActionKey, setQuickActionKey] = useState("");

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
  const readinessState = readinessLabel(readiness);
  const loopState = latestLoopHumanSummary(latestLoopRun);
  const pendingCandidateCount = triageCandidates.length;
  const readyToAddCount = triageCandidates.filter((item) => item.recommended_action === "add").length;
  const nextOperatorStep = pendingCandidateCount
    ? `Разобрать ${pendingCandidateCount} кандидатов: ${readyToAddCount} можно добавить, остальные требуют проверки.`
    : readiness?.status === "ready"
      ? "Запустите loop или включите расписание, чтобы агент начал искать новые источники."
      : "Сначала включите недостающие части контура из блока готовности.";
  const planActions = plan?.actions ?? [];
  const humanReviewPlanActions = planActions.filter((item) => item.requires_human_approval || item.policy_decision === "human_review").slice(0, 5);
  const autoPlanActions = planActions.filter((item) => item.policy_decision === "auto").slice(0, 5);
  const blockedPlanActions = planActions.filter((item) => item.policy_decision === "blocked").slice(0, 4);
  const learnedMemory = [...comboMemory, ...goodDomainMemory, ...badDomainMemory, ...temporaryDomainMemory]
    .sort((a, b) => new Date(b.updated_at || b.created_at || "").getTime() - new Date(a.updated_at || a.created_at || "").getTime())
    .slice(0, 6);
  const recentAuditActions = actions.slice(0, 6);
  const dryRunIterations = dryRunReport?.iterations ?? [];
  const dryRunObservations = dryRunIterations.flatMap((iteration) => iteration.observations || []);
  const dryRunTopics = Array.from(new Set(dryRunObservations.map((item) => item.topic).filter(Boolean).map(String))).slice(0, 8);
  const dryRunQueries = dryRunObservations.reduce((sum, item) => sum + Number(item.query_count || 0), 0);
  const dryRunAutoActions = dryRunIterations.reduce((sum, item) => sum + Number(item.auto_action_count || 0), 0);
  const dryRunHumanActions = dryRunIterations.reduce((sum, item) => sum + Number(item.human_review_count || 0), 0);
  const latestReflection = reflectionFromResult(latestLoopRun?.result_json);
  const dryRunReflection = reflectionFromResult(dryRunReport as unknown as Record<string, unknown> | null);
  const activeReflection = dryRunReflection || latestReflection;

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
      const [
        topics,
        domains,
        queries,
        mutedQueries,
        strategies,
        goodDomains,
        mutedDomains,
        rejectedDomains,
        combos,
        temporaryDomains,
        watchedSources,
        evaluationReport,
        readinessReport,
      ] = await Promise.all([
        listSourceDiscoveryQuality({ group_by: "topic", limit: 8 }),
        listSourceDiscoveryQuality({ group_by: "domain", limit: 8 }),
        listQueryMemory({ limit: 8, status: "active" }),
        listQueryMemory({ limit: 8, status: "muted" }),
        listAgentMemory({ memory_type: "strategy", status: "", limit: 50 }),
        listAgentMemory({ memory_type: "domain", status: "active", limit: 8 }),
        listAgentMemory({ memory_type: "domain", status: "muted", limit: 8 }),
        listAgentMemory({ memory_type: "domain", status: "rejected", limit: 8 }),
        listAgentMemory({ memory_type: "topic_query_domain", status: "", limit: 12 }),
        listAgentMemory({ memory_type: "domain", status: "temporary_unavailable", limit: 8 }),
        listAgentMemory({ memory_type: "source", status: "", limit: 24 }),
        getSourceDiscoveryEvaluation(500),
        getSourceDiscoveryReadiness(),
      ]);
      const triage = await listSourceCandidateTriage({ limit: 8 });
      setTopicQuality(topics);
      setDomainQuality(domains);
      setQueryMemory(queries);
      setMutedQueryMemory(mutedQueries);
      setStrategyMemory(strategies);
      setGoodDomainMemory(goodDomains);
      setBadDomainMemory([...mutedDomains, ...rejectedDomains].sort((a, b) => Number(a.score || 0) - Number(b.score || 0)).slice(0, 8));
      setComboMemory(combos);
      setTemporaryDomainMemory(temporaryDomains);
      setWatchedSourceMemory(
        watchedSources
          .filter((item) => String(item.facts_json?.recommendation || "keep") !== "keep")
          .sort((a, b) => Number(a.score || 0) - Number(b.score || 0))
          .slice(0, 8),
      );
      setEvaluation(evaluationReport);
      setTriageCandidates(triage);
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
        fetch_inspection: true,
        test_parse: true,
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

  async function handleDryRun() {
    try {
      setDryRunning(true);
      const result = await dryRunSourceDiscoveryLoop({
        ...settings,
        goal: "Пробно проверить, что агент сделал бы при поиске источников",
        max_iterations: 2,
        fetch_inspection: settings.offline ? false : true,
        test_parse: false,
        dry_run: true,
        auto_evaluate: false,
        article_limit: 5,
        max_daily_loop_runs: 0,
        max_daily_candidates: 0,
        max_daily_evaluations: 0,
      });
      setDryRunReport(result.result);
      showToast(`Пробный запуск готов: ${result.result.total_candidates} кандидатов`);
    } catch (error) {
      handleError(error, "Не удалось выполнить пробный запуск");
    } finally {
      setDryRunning(false);
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

  async function refreshAgentWorksurface() {
    await Promise.all([loadQuality(), loadActions(), loadPlan()]);
  }

  async function handleCandidateQuickAction(
    candidate: SourceCandidateTriageRow,
    action: "approve" | "noise" | "duplicate" | "test_more",
  ) {
    const actionKey = `candidate:${candidate.id}:${action}`;
    try {
      setQuickActionKey(actionKey);
      if (action === "approve") {
        const result = await approveSourceCandidate(candidate.id, {
          enabled: false,
          parse_strategy: candidate.candidate_type === "rss" ? "rss" : "request",
          network_region: "auto",
          scrape_after_approve: true,
        });
        showToast(result.initial_job ? `Источник создан: #${result.source_id}, сбор job #${result.initial_job.id}` : `Источник создан: #${result.source_id}`);
      } else if (action === "test_more") {
        await updateSourceCandidate(candidate.id, {
          status: "test_parsing",
          recommended_action: "test_more",
          review_comment: "Оператор со страницы агента отправил кандидата на дополнительную проверку.",
        });
        showToast("Кандидат отправлен на дополнительную проверку");
      } else {
        await updateSourceCandidate(candidate.id, {
          status: "rejected",
          recommended_action: "reject",
          review_comment: action === "duplicate"
            ? "Оператор со страницы агента отклонил кандидата как дубликат."
            : "Оператор со страницы агента отклонил кандидата как шум.",
        });
        showToast(action === "duplicate" ? "Кандидат отклонен как дубликат" : "Кандидат отклонен как шум");
      }
      await refreshAgentWorksurface();
    } catch (error) {
      handleError(error, "Не удалось сохранить решение по кандидату");
    } finally {
      setQuickActionKey("");
    }
  }

  async function handleSourceQuickAction(
    memoryItem: AgentMemory,
    action: "increase" | "decrease" | "pause" | "request" | "playwright" | "diagnose" | "external",
  ) {
    const sourceId = sourceMemoryId(memoryItem);
    if (!sourceId) {
      showToast("У записи памяти нет source_id", "error");
      return;
    }
    const actionKey = `source:${sourceId}:${action}`;
    try {
      setQuickActionKey(actionKey);
      if (action === "diagnose") {
        const result = await diagnoseSourceJob(sourceId, {});
        showToast(`Диагностика поставлена в очередь: job #${result.job.id}`);
      } else {
        const payload = action === "increase"
          ? { update_frequency: "daily" as const, enabled: true }
          : action === "decrease"
            ? { update_frequency: "weekly" as const }
            : action === "pause"
              ? { enabled: false }
              : action === "request"
                ? { parse_strategy: "request" as const }
                : action === "playwright"
                  ? { parse_strategy: "playwright" as const, network_profile: "browser" as const }
                  : { network_region: "external" as const, network_profile: "proxy" as const };
        await updateSource(sourceId, payload);
        showToast("Источник обновлен");
      }
      await refreshAgentWorksurface();
    } catch (error) {
      handleError(error, "Не удалось выполнить действие по источнику");
    } finally {
      setQuickActionKey("");
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

  async function handleSeedDiscover() {
    const topic = seedTopic.trim();
    const url = seedUrl.trim();
    if (!topic || !url) {
      showToast("Укажите тему и URL кандидата", "error");
      return;
    }
    try {
      setDiscoveringSeed(true);
      const result = await discoverSourceCandidate({
        topic,
        seed_url: url,
        limit: settings.candidate_limit,
        offline: settings.offline,
        fetch_inspection: seedFetchInspection,
        test_parse: seedTestParse,
      });
      const count = result.result.candidates.length;
      const existing = result.result.existing_sources_skipped?.length ?? 0;
      const cooldown = result.result.cooldown_sources_skipped?.length ?? 0;
      const qualityGate = result.result.quality_gate_sources_skipped?.length ?? 0;
      const unavailable = result.result.unavailable_sources_skipped?.length ?? 0;
      const parseFailed = result.result.parse_failed_sources_skipped?.length ?? 0;
      const skipped = [
        existing ? `уже есть: ${existing}` : "",
        cooldown ? `на паузе: ${cooldown}` : "",
        qualityGate ? `отсечены фильтром: ${qualityGate}` : "",
        unavailable ? `недоступны: ${unavailable}` : "",
        parseFailed ? `без статей: ${parseFailed}` : "",
      ].filter(Boolean).join(", ");
      setSeedUrl("");
      showToast(count ? `Кандидат добавлен: ${count}${skipped ? `; пропущено: ${skipped}` : ""}` : `Кандидаты не созданы${skipped ? `: ${skipped}` : ""}`);
      await Promise.all([loadQuality(), loadActions()]);
    } catch (error) {
      handleError(error, "Не удалось добавить seed-url");
    } finally {
      setDiscoveringSeed(false);
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
          <button type="button" className="ghostButton compactButton" onClick={() => void handleDryRun()} disabled={dryRunning}>
            {dryRunning ? "Проверяем…" : "Пробный запуск"}
          </button>
        </div>
      </header>

      <section className="agentCommandCenter">
        <article className={`agentCommandPrimary ${readinessState.tone}`}>
          <div>
            <span className="agentCommandKicker">Состояние агента</span>
            <h2>{readinessState.title}</h2>
            <p>{readinessState.detail}</p>
          </div>
          <div className="agentCommandActions">
            <button type="button" className="primaryButton compactButton" onClick={() => void handleLoopEnqueue()} disabled={loopEnqueueing}>
              {loopEnqueueing ? "Запускаем…" : "Запустить цикл"}
            </button>
            <button type="button" className="ghostButton compactButton" onClick={() => void handleDryRun()} disabled={dryRunning}>
              {dryRunning ? "Проверяем…" : "Пробный запуск"}
            </button>
            <a className="ghostButton compactButton" href="?screen=source-candidates">
              Кандидаты
            </a>
          </div>
        </article>

        <article className="agentCommandCard">
          <span className="agentCommandKicker">Последний запуск</span>
          <strong>{loopState.title}</strong>
          <p>{loopState.detail}</p>
          <div className="agentCommandMetric">
            <span>{loopState.metric}</span>
            <small>новых кандидатов</small>
          </div>
        </article>

        <article className="agentCommandCard">
          <span className="agentCommandKicker">Что делать сейчас</span>
          <strong>{pendingCandidateCount ? "Нужно решение оператора" : "Нет срочных решений"}</strong>
          <p>{nextOperatorStep}</p>
          <div className="agentCommandMetric">
            <span>{pendingCandidateCount}</span>
            <small>в очереди проверки</small>
          </div>
        </article>
      </section>

      <section className="agentReflectionPanel">
        <article className="panel agentReflectionCard">
          <div className="panelHeader">
            <h2>Вывод агента</h2>
            <span className="badge">{activeReflection ? "есть" : "нет данных"}</span>
          </div>
          {!activeReflection ? (
            <div className="agentDryRunIntro">
              <strong>Агент еще не сформулировал вывод</strong>
              <p>Запустите цикл или пробный запуск. После этого здесь появится, что сработало, что не сработало и что агент поменяет дальше.</p>
            </div>
          ) : (
            <div className="agentReflectionGrid">
              <div className="agentReflectionColumn good">
                <h3>Что получилось</h3>
                {!activeReflection.workedTopics.length ? <p>Тем с результатом пока нет.</p> : null}
                {activeReflection.workedTopics.slice(0, 4).map((item) => (
                  <div className="agentReflectionRow" key={`worked-${String(item.topic)}`}>
                    <strong>{String(item.topic || "без темы")}</strong>
                    <span>{Number(item.candidate_count || 0)} кандидатов · {Number(item.relevant_articles || 0)} релев.</span>
                  </div>
                ))}
                {activeReflection.strongStrategies.slice(0, 2).map((item) => (
                  <div className="agentReflectionRow" key={`strategy-${String(item.strategy)}`}>
                    <strong>{String(item.strategy || "стратегия")}</strong>
                    <span>стратегия дала {Number(item.candidate_count || 0)} кандидатов</span>
                  </div>
                ))}
              </div>
              <div className="agentReflectionColumn warning">
                <h3>Что не получилось</h3>
                {!activeReflection.emptyTopics.length && !activeReflection.weakStrategies.length ? <p>Явных слабых мест в последнем запуске нет.</p> : null}
                {activeReflection.emptyTopics.slice(0, 4).map((item) => (
                  <div className="agentReflectionRow" key={`empty-${String(item.topic)}`}>
                    <strong>{String(item.topic || "без темы")}</strong>
                    <span>не найдено кандидатов</span>
                  </div>
                ))}
                {activeReflection.weakStrategies.slice(0, 2).map((item) => (
                  <div className="agentReflectionRow" key={`weak-${String(item.strategy)}`}>
                    <strong>{String(item.strategy || "стратегия")}</strong>
                    <span>пустой или ошибочный результат</span>
                  </div>
                ))}
              </div>
              <div className="agentReflectionColumn">
                <h3>Что поменяет дальше</h3>
                {!activeReflection.nextHints.length ? <p>Подсказок на следующий запуск пока нет.</p> : null}
                {activeReflection.nextHints.slice(0, 6).map((item, index) => (
                  <div className="agentReflectionRow" key={`hint-${index}-${String(item.kind)}`}>
                    <strong>{String(item.topic || item.strategy || item.kind || "действие")}</strong>
                    <span>{String(item.reason || "использовать в следующем плане")}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </article>
      </section>

      <section className="agentSeedPanel">
        <div>
          <span className="agentCommandKicker">Быстрый seed-url</span>
          <h2>Дать агенту источник руками</h2>
          <p>Используйте, пока внешний поиск не подключен: вставьте URL раздела новостей, пресс-центра, RSS или страницы источника.</p>
        </div>
        <div className="agentSeedForm">
          <label className="field">
            <span>Тема</span>
            <input value={seedTopic} onChange={(event) => setSeedTopic(event.target.value)} placeholder="бурение, роботизация, ГРП" />
          </label>
          <label className="field">
            <span>URL</span>
            <input value={seedUrl} onChange={(event) => setSeedUrl(event.target.value)} placeholder="https://example.com/news" />
          </label>
          <label className="checkField">
            <input type="checkbox" checked={seedFetchInspection} onChange={(event) => setSeedFetchInspection(event.target.checked)} />
            <span>Проверить доступность</span>
          </label>
          <label className="checkField">
            <input type="checkbox" checked={seedTestParse} onChange={(event) => setSeedTestParse(event.target.checked)} />
            <span>Пробный парсинг</span>
          </label>
          <button type="button" className="primaryButton compactButton" onClick={() => void handleSeedDiscover()} disabled={discoveringSeed}>
            {discoveringSeed ? "Добавляем…" : "Добавить кандидата"}
          </button>
        </div>
      </section>

      <section className="agentOperatorBoard">
        <article className="panel agentOperatorColumn">
          <div className="panelHeader">
            <h2>Что агент сделал</h2>
            <span className="badge">{recentAuditActions.length} событий</span>
          </div>
          <div className="agentOperatorList">
            {!recentAuditActions.length ? <div className="emptyState">Истории пока нет. Запустите цикл или план.</div> : null}
            {recentAuditActions.map((item) => (
              <div className={`agentOperatorRow ${item.decision_tone || "neutral"}`} key={item.id}>
                <span>{formatDate(item.created_at)}</span>
                <strong>{item.decision_title || item.action_type}</strong>
                <p>{item.decision_summary || actionSummary(item)}</p>
              </div>
            ))}
          </div>
        </article>

        <article className="panel agentOperatorColumn">
          <div className="panelHeader">
            <h2>Что агент рекомендует</h2>
            <span className="badge">{autoPlanActions.length} авто</span>
          </div>
          <div className="agentOperatorList">
            {!autoPlanActions.length ? <div className="emptyState">Автоматических рекомендаций пока нет.</div> : null}
            {autoPlanActions.map((item, index) => (
              <div className="agentOperatorRow good" key={`${item.action_type}-${index}-${actionTarget(item)}`}>
                <span>{operatorActionLabel(item)}</span>
                <strong>{ACTION_LABELS[item.action_type] ?? item.action_type}</strong>
                <p>{actionTarget(item)} · {item.reason}</p>
              </div>
            ))}
          </div>
        </article>

        <article className="panel agentOperatorColumn">
          <div className="panelHeader">
            <h2>Где нужен человек</h2>
            <span className="badge">{humanReviewPlanActions.length + triageCandidates.length} решений</span>
          </div>
          <div className="agentOperatorList">
            {!humanReviewPlanActions.length && !triageCandidates.length ? <div className="emptyState">Срочных ручных решений нет.</div> : null}
            {triageCandidates.slice(0, 3).map((candidate) => (
              <div className="agentOperatorRow warning" key={`candidate-${candidate.id}`}>
                <span>{candidateActionLabel(candidate)}</span>
                <strong>{candidate.name || candidate.normalized_domain}</strong>
                <p>{candidate.triage_reason}</p>
                <div className="agentQuickActions">
                  <button type="button" className="miniActionButton good" onClick={() => void handleCandidateQuickAction(candidate, "approve")} disabled={Boolean(quickActionKey)}>
                    {quickActionKey === `candidate:${candidate.id}:approve` ? "..." : "Одобрить"}
                  </button>
                  <button type="button" className="miniActionButton" onClick={() => void handleCandidateQuickAction(candidate, "test_more")} disabled={Boolean(quickActionKey)}>
                    Еще проверить
                  </button>
                  <button type="button" className="miniActionButton bad" onClick={() => void handleCandidateQuickAction(candidate, "noise")} disabled={Boolean(quickActionKey)}>
                    Шум
                  </button>
                  <button type="button" className="miniActionButton bad" onClick={() => void handleCandidateQuickAction(candidate, "duplicate")} disabled={Boolean(quickActionKey)}>
                    Дубликат
                  </button>
                </div>
              </div>
            ))}
            {humanReviewPlanActions.map((item, index) => (
              <div className="agentOperatorRow warning" key={`${item.action_type}-${index}-${actionTarget(item)}`}>
                <span>{operatorActionLabel(item)}</span>
                <strong>{ACTION_LABELS[item.action_type] ?? item.action_type}</strong>
                <p>{actionTarget(item)} · {item.reason}</p>
              </div>
            ))}
            {blockedPlanActions.map((item, index) => (
              <div className="agentOperatorRow bad" key={`${item.action_type}-${index}-${actionTarget(item)}`}>
                <span>Блок</span>
                <strong>{ACTION_LABELS[item.action_type] ?? item.action_type}</strong>
                <p>{item.policy_reason || item.reason}</p>
              </div>
            ))}
          </div>
        </article>

        <article className="panel agentOperatorColumn">
          <div className="panelHeader">
            <h2>Что агент запомнил</h2>
            <span className="badge">{learnedMemory.length} записей</span>
          </div>
          <div className="agentOperatorList">
            {!learnedMemory.length ? <div className="emptyState">Новая память появится после решений и оценок кандидатов.</div> : null}
            {learnedMemory.map((item) => (
              <div className={`agentOperatorRow ${item.status === "active" ? "good" : item.status === "temporary_unavailable" ? "warning" : "bad"}`} key={item.id}>
                <span>{memoryTypeLabel(item.memory_type)} · score {Math.round(Number(item.score || 0))}</span>
                <strong>{item.memory_type === "topic_query_domain" ? comboTitle(item) : item.subject}</strong>
                <p>{item.memory_type === "topic_query_domain" ? comboDetail(item) : memoryReason(item)}</p>
              </div>
            ))}
          </div>
        </article>
      </section>

      <section className="agentDryRunPanel">
        <article className="panel agentDryRunCard">
          <div className="panelHeader">
            <h2>Пробный запуск</h2>
            <button type="button" className="ghostButton compactButton" onClick={() => void handleDryRun()} disabled={dryRunning}>
              {dryRunning ? "Проверяем…" : "Запустить без записи"}
            </button>
          </div>
          {!dryRunReport ? (
            <div className="agentDryRunIntro">
              <strong>Безопасная проверка перед автозапуском</strong>
              <p>Агент построит план, попробует поиск и вернет отчет. Кандидаты, память, задачи и журнал действий не создаются.</p>
            </div>
          ) : (
            <div className="agentDryRunReport">
              <div className="agentDryRunMetrics">
                <div><span>{dryRunReport.total_candidates}</span><p>кандидатов нашел бы</p></div>
                <div><span>{dryRunQueries}</span><p>поисковых запросов</p></div>
                <div><span>{dryRunAutoActions}</span><p>авто-действий</p></div>
                <div><span>{dryRunHumanActions}</span><p>ручных решений</p></div>
                <div><span>{dryRunReport.duration_ms}</span><p>мс</p></div>
              </div>
              <div className="agentDryRunMeta">
                <span>Остановка: {dryRunReport.terminal_reason}</span>
                <span>Итераций: {dryRunIterations.length}</span>
                <span>Темы: {dryRunTopics.length ? dryRunTopics.join(", ") : "нет"}</span>
              </div>
              <div className="agentDryRunObservationList">
                {!dryRunObservations.length ? <div className="emptyState">Пробный запуск не нашел действий для проверки.</div> : null}
                {dryRunObservations.map((item, index) => (
                  <div className="agentDryRunObservation" key={`${item.topic || "topic"}-${index}`}>
                    <strong>{item.topic || "без темы"}</strong>
                    <span>{item.query_strategy || "стратегия не указана"}</span>
                    <span>{item.search_status || "поиск"}</span>
                    <span>{item.query_count ?? 0} запросов</span>
                    <span>{item.candidate_count ?? 0} кандидатов</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </article>
      </section>

      <section className="agentCandidateQueue">
        <div className="panelHeader">
          <h2>Кандидаты на решение</h2>
          <a className="ghostButton compactButton" href="?screen=source-candidates">Открыть все</a>
        </div>
        {!loadingQuality && !triageCandidates.length ? (
          <div className="emptyState">Сейчас нет кандидатов, по которым нужно решение. Запустите цикл или добавьте seed-url.</div>
        ) : null}
        {triageCandidates.length ? (
          <div className="agentCandidateQueueList">
            {triageCandidates.map((candidate) => (
              <a className="agentCandidateQueueRow" href="?screen=source-candidates" key={candidate.id}>
                <span className="agentCandidatePriority">{Math.round(candidate.triage_priority)}</span>
                <span>
                  <strong>{candidate.name || candidate.normalized_domain}</strong>
                  <small>{candidate.topic || "без темы"} · {candidate.normalized_domain}</small>
                </span>
                <span>{candidateActionLabel(candidate)}</span>
                <span>{candidate.relevant_articles}/{candidate.tested_articles} релев.</span>
                <span>score {candidate.avg_score ?? "—"}</span>
              </a>
            ))}
          </div>
        ) : null}
      </section>

      <section className="agentWatchedSources">
        <div className="panelHeader">
          <h2>Источники под наблюдением</h2>
          <a className="ghostButton compactButton" href="?screen=sources">Открыть источники</a>
        </div>
        {!loadingQuality && !watchedSourceMemory.length ? (
          <div className="emptyState">Пока нет источников с рекомендациями. После следующего плана агент заполнит этот блок.</div>
        ) : null}
        {watchedSourceMemory.length ? (
          <div className="agentWatchedSourceList">
            {watchedSourceMemory.map((item) => {
              const sourceId = sourceMemoryId(item);
              return (
                <div className={`agentWatchedSourceRow ${item.status}`} key={item.id}>
                  <div>
                    <strong>{item.subject}</strong>
                    <small>{sourceProblem(item)} · {sourceSeverity(item)} · confidence {sourceConfidence(item)} · score {Math.round(Number(item.score || 0))}</small>
                  </div>
                  <span>{sourceRecommendation(item)}</span>
                  <p>{sourceReason(item)}</p>
                  <div className="agentQuickActions">
                    <button type="button" className="miniActionButton good" onClick={() => void handleSourceQuickAction(item, "increase")} disabled={Boolean(quickActionKey)}>
                      Чаще
                    </button>
                    <button type="button" className="miniActionButton" onClick={() => void handleSourceQuickAction(item, "decrease")} disabled={Boolean(quickActionKey)}>
                      Реже
                    </button>
                    <button type="button" className="miniActionButton bad" onClick={() => void handleSourceQuickAction(item, "pause")} disabled={Boolean(quickActionKey)}>
                      Пауза
                    </button>
                    <button type="button" className="miniActionButton" onClick={() => void handleSourceQuickAction(item, "request")} disabled={Boolean(quickActionKey)}>
                      Обычный
                    </button>
                    <button type="button" className="miniActionButton" onClick={() => void handleSourceQuickAction(item, "playwright")} disabled={Boolean(quickActionKey)}>
                      Браузер
                    </button>
                    <button type="button" className="miniActionButton" onClick={() => void handleSourceQuickAction(item, "external")} disabled={Boolean(quickActionKey)}>
                      Внешний контур
                    </button>
                    <button type="button" className="miniActionButton" onClick={() => void handleSourceQuickAction(item, "diagnose")} disabled={Boolean(quickActionKey)}>
                      Диагностика
                    </button>
                  </div>
                  <a className="ghostButton compactButton" href={sourceId ? `?screen=sources&source_id=${sourceId}` : "?screen=sources"}>
                    Открыть источник
                  </a>
                </div>
              );
            })}
          </div>
        ) : null}
      </section>

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

      <section className="agentEvaluationPanel">
        <article className="panel agentEvaluationSummary">
          <div className="panelHeader">
            <h2>Качество агента</h2>
            <span className="badge">{evaluation ? `${Math.round(evaluation.summary.candidate_agreement_rate * 100)}% согласия` : "нет данных"}</span>
          </div>
          {!evaluation ? <div className="emptyState">Отчёт качества пока не загружен.</div> : null}
          {evaluation ? (
            <>
              <div className="agentEvaluationMetrics">
                <div><span>{evaluation.summary.candidate_decisions}</span><p>решений по кандидатам</p></div>
                <div><span>{Math.round(evaluation.summary.candidate_agreement_rate * 100)}%</span><p>совпало с оператором</p></div>
                <div><span>{evaluation.summary.sources_under_watch}</span><p>источников под наблюдением</p></div>
                <div><span>{evaluation.summary.weak_rules}</span><p>слабых правил</p></div>
              </div>
              <div className="agentEvaluationGrid">
                <div>
                  <h3>Рекомендации</h3>
                  {evaluation.source_audit.recommendations.slice(0, 5).map((item) => (
                    <div className="agentEvaluationRow" key={item.recommendation}>
                      <strong>{item.label}</strong>
                      <span>{item.count}</span>
                      <small>score {Math.round(item.avg_source_score)}</small>
                    </div>
                  ))}
                </div>
                <div>
                  <h3>Проблемы</h3>
                  {evaluation.source_audit.problems.slice(0, 5).map((item) => (
                    <div className="agentEvaluationRow" key={item.problem_type}>
                      <strong>{item.problem_type}</strong>
                      <span>{item.count}</span>
                      <small>score {Math.round(item.avg_source_score)}</small>
                    </div>
                  ))}
                </div>
                <div>
                  <h3>Слабые правила</h3>
                  {!evaluation.weak_rules.length ? <p className="agentEvaluationNote">Явных слабых правил пока нет.</p> : null}
                  {evaluation.weak_rules.slice(0, 5).map((item) => (
                    <div className="agentEvaluationRow" key={item.rule}>
                      <strong>{item.rule}</strong>
                      <span>{item.suppressed} под.</span>
                      <small>low conf {item.confidence_low}</small>
                    </div>
                  ))}
                </div>
              </div>
            </>
          ) : null}
        </article>
      </section>

      <section className="agentInsightPanel">
        <article className="panel agentInsightCard wide">
          <div className="panelHeader">
            <h2>Чему агент научился</h2>
            <button type="button" className="ghostButton compactButton" onClick={() => void loadQuality()} disabled={loadingQuality}>
              {loadingQuality ? "Обновляем…" : "Обновить"}
            </button>
          </div>
          <div className="agentInsightGrid">
            <div className="agentInsightColumn good">
              <h3>Сильные домены</h3>
              {!loadingQuality && !goodDomainMemory.length ? <div className="emptyState">Пока нет усиленных доменов.</div> : null}
              {goodDomainMemory.map((item) => (
                <div className="agentInsightRow" key={item.id}>
                  <strong>{item.subject}</strong>
                  <span>score {Math.round(Number(item.score || 0))}</span>
                  <p>{memoryReason(item)}</p>
                </div>
              ))}
            </div>
            <div className="agentInsightColumn bad">
              <h3>Плохие домены</h3>
              {!loadingQuality && !badDomainMemory.length ? <div className="emptyState">Пока нет оштрафованных доменов.</div> : null}
              {badDomainMemory.map((item) => (
                <div className="agentInsightRow" key={item.id}>
                  <strong>{item.subject}</strong>
                  <span>score {Math.round(Number(item.score || 0))}</span>
                  <p>{memoryReason(item)}</p>
                </div>
              ))}
            </div>
            <div className="agentInsightColumn warning">
              <h3>Временно недоступны</h3>
              {!loadingQuality && !temporaryDomainMemory.length ? <div className="emptyState">Нет доменов на паузе.</div> : null}
              {temporaryDomainMemory.map((item) => (
                <div className="agentInsightRow" key={item.id}>
                  <strong>{item.subject}</strong>
                  <span>{factString(item, "last_reason") || "retry later"}</span>
                  <p>{factString(item, "retry_after") ? `Повтор после ${factString(item, "retry_after")}` : factSummary(item)}</p>
                </div>
              ))}
            </div>
          </div>
        </article>

        <article className="panel agentInsightCard">
          <div className="panelHeader">
            <h2>Лучшие связки</h2>
            <span className="badge">{comboMemory.length} записей</span>
          </div>
          <div className="agentComboList">
            {!loadingQuality && !comboMemory.length ? <div className="emptyState">Связки темы, запроса и домена пока не накоплены.</div> : null}
            {comboMemory.map((item) => (
              <div className={`agentComboRow ${item.status}`} key={item.id}>
                <div>
                  <strong>{comboTitle(item)}</strong>
                  <p>{comboDetail(item)}</p>
                </div>
                <div className="agentComboScore">
                  <span>{Math.round(Number(item.score || 0))}</span>
                  <small>{item.status}</small>
                </div>
                <div className="agentFunnel">
                  <span title="Найдено">Н {nestedFactNumber(item, "quality_funnel", "found")}</span>
                  <span title="Распарсено">Р {nestedFactNumber(item, "quality_funnel", "parsed")}</span>
                  <span title="Обработано">О {nestedFactNumber(item, "quality_funnel", "processed")}</span>
                  <span title="Релевантно">✓ {nestedFactNumber(item, "quality_funnel", "relevant")}</span>
                  <span title="Score 50+">50+ {nestedFactNumber(item, "quality_funnel", "score_50_plus")}</span>
                </div>
              </div>
            ))}
          </div>
        </article>
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
                  {action.memory_explanation ? (
                    <div className="agentPlanMemoryExplain">
                      {action.memory_explanation.promoted_combos?.length ? (
                        <div>
                          <strong>Усилено памятью</strong>
                          {action.memory_explanation.promoted_combos.map((item) => (
                            <span key={`${item.query}-${item.domain}`}>{item.query} · {item.domain} · {Math.round(item.score)}</span>
                          ))}
                        </div>
                      ) : null}
                      {action.memory_explanation.muted_combos?.length ? (
                        <div>
                          <strong>Исключить</strong>
                          {action.memory_explanation.muted_combos.map((item) => (
                            <span key={`${item.query}-${item.domain}`}>{item.query} · {item.domain} · {Math.round(item.score)}</span>
                          ))}
                        </div>
                      ) : null}
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
                <option value="topic_query_domain">Связки</option>
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
                  {actionAuditItems(item).length ? (
                    <div className="agentHistoryAuditTrail">
                      {actionAuditItems(item).map((auditItem) => <span key={auditItem}>{auditItem}</span>)}
                    </div>
                  ) : null}
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
