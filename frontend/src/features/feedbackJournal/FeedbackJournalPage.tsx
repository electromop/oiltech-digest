import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError } from "../../api/client";
import {
  closeTask,
  collectNow,
  decideProposal,
  journalOverview,
  listExpertRules,
  listJournal,
  recheckRule,
  rollbackRule,
  undoAction,
  type ExpertRulesOverview,
  type JournalEntry,
  type JournalOverview,
  type JournalPage,
  type ProposalDecision,
  type RuleDecisionExtra,
} from "../../api/feedbackJournal";
import { SkeletonList } from "../shared/Skeleton";
import { ContourSettingsPanel } from "./ContourSettingsPanel";
import { ExpertRulesPanel } from "./ExpertRulesPanel";
import { AGENT_LABELS, formatMoment, JournalEntryCard, SHOWCASE_LABELS, STATUS_LABELS } from "./JournalEntryCard";

// Вкладка «Обратная связь» (#189, спека #187): журнал ОС по обеим витринам, недельный срез и настройки контура.
// Ссылка со значка карточки: ?screen=feedback-journal&signal=<номер карточки>&action=<строка журнала>.

type Props = {
  onUnauthorized: () => void;
  showToast: (text: string, tone?: "default" | "error") => void;
  isAdmin?: boolean;
};

type Filters = { status: string; showcase: string; agent: string; author: string; signal: string };

function filtersFromUrl(): Filters {
  const params = new URLSearchParams(window.location.search);
  return { status: "", showcase: "", agent: "", author: "", signal: params.get("signal") ?? "" };
}

function actionFromUrl(): string | null {
  const action = new URLSearchParams(window.location.search).get("action");
  return action ? `action:${action}` : null;
}

export function FeedbackJournalPage({ onUnauthorized, showToast, isAdmin = false }: Props) {
  const [filters, setFilters] = useState<Filters>(filtersFromUrl);
  const [page, setPage] = useState<JournalPage | null>(null);
  const [overview, setOverview] = useState<JournalOverview | null>(null);
  const [rules, setRules] = useState<ExpertRulesOverview | null>(null);
  const [closed, setClosed] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const highlighted = useMemo(actionFromUrl, []);

  function handleError(error: unknown) {
    if (error instanceof ApiError && error.status === 401) {
      onUnauthorized();
      return;
    }
    showToast(error instanceof Error ? error.message : "Не удалось выполнить запрос", "error");
  }

  // Перечитать журнал и срез: при смене фильтров и после действия; от обработчиков App не зависит, иначе каждая
  // перерисовка App (тост) перечитывала бы журнал.
  const reload = useCallback(async () => {
    try {
      const [loaded, state, agents] = await Promise.all([
        listJournal({ ...filters, signal_id: filters.signal ? Number(filters.signal) : undefined, limit: 200 }),
        journalOverview(),
        listExpertRules(),
      ]);
      setPage(loaded);
      setOverview(state);
      setRules(agents);
      setClosed(null);
    } catch (error) {
      if (error instanceof ApiError && error.status === 403) setClosed(error.message);
      else handleError(error);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters]);

  useEffect(() => { void reload(); }, [reload]);

  useEffect(() => {
    if (highlighted && page) document.getElementById(`journal-${highlighted}`)?.scrollIntoView?.({ block: "center" });
  }, [highlighted, page]);

  async function act(run: () => Promise<unknown>, done: string) {
    setBusy(true);
    try {
      await run();
      showToast(done, "default");
      await reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        showToast(error.message, "error");
        await reload();
      } else {
        handleError(error);
      }
    } finally {
      setBusy(false);
    }
  }

  function decide(entry: JournalEntry, decision: ProposalDecision, changes: Array<{ field: string; value: string }> = [],
                  extra: RuleDecisionExtra = {}) {
    const rule = entry.kind === "rule_proposal";
    const done = {
      approve: rule ? "Согласовано: система выпустила новую версию набора правил агента"
        : "Согласовано: система применила улучшение",
      modify: rule ? "Новая формулировка правила согласована" : "Новая формулировка применена",
      dismiss: "Предложение убрано, остаётся в журнале",
    }[decision];
    void act(() => decideProposal(entry.id, decision, changes, extra), done);
  }

  function rollback(proposalId: number) {
    void act(() => rollbackRule(proposalId), "Откат: действует прежняя версия набора правил");
  }

  const authors = page?.authors ?? [];
  const entries = page?.entries ?? [];
  const lastSlice = overview?.slices[0];

  if (closed) {
    return (
      <section className="screenStack">
        <header className="screenHeader"><div><h1>Обратная связь</h1></div></header>
        <section className="panel"><div className="emptyState">{closed}</div></section>
      </section>
    );
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Обратная связь</h1>
          <p>Журнал ОС обеих витрин: каждая отметка и что с ней сделала или предлагает система.</p>
        </div>
      </header>

      <section className="panel" aria-label="Недельный срез">
        <div className="panelHeader">
          <h2>Недельный срез</h2>
          {overview?.can_collect ? (
            <div className="panelActions">
              <button type="button" className="primaryButton compactButton" disabled={busy || overview.running}
                onClick={() => void act(collectNow, "Срез запущен: предложения появятся в журнале")}>
                {overview.running ? "Срез идёт…" : "Собрать сейчас"}
              </button>
            </div>
          ) : null}
        </div>
        {overview ? (
          <div className="journalOverview">
            <p>Следующий срез по расписанию: {formatMoment(overview.next_slice_at)} (МСК).</p>
            {lastSlice ? (
              <p className="metaText">
                Последний срез №{lastSlice.id}: {lastSlice.status === "done" ? "выполнен" : lastSlice.status === "running"
                  ? "идёт" : lastSlice.status === "postponed" ? "отложен" : "не выполнен"}, сигналов {lastSlice.signals},
                {" "}предложений {lastSlice.proposals}{lastSlice.note ? `; ${lastSlice.note}` : ""}.
              </p>
            ) : null}
            <p className="metaText">
              Расход контура ОС (разбор и автопроверка) в этом месяце: ${overview.month_spend_usd.toFixed(2)} из
              {" "}${overview.ceiling_usd.toFixed(2)}.
            </p>
            {overview.warning ? <div className="documentAlert warn" role="alert">{overview.warning}</div> : null}
          </div>
        ) : null}
      </section>

      <section className="panel" aria-label="Журнал ОС">
        <div className="jobsFilters">
          <label className="field">
            <span>Статус</span>
            <select value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value })}>
              <option value="">Все</option>
              {Object.entries(STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <label className="field">
            <span>Витрина</span>
            <select value={filters.showcase} onChange={(event) => setFilters({ ...filters, showcase: event.target.value })}>
              <option value="">Обе</option>
              <option value="business">{SHOWCASE_LABELS.business}</option>
              <option value="technology">{SHOWCASE_LABELS.technology}</option>
            </select>
          </label>
          <label className="field">
            <span>Агент</span>
            <select value={filters.agent} onChange={(event) => setFilters({ ...filters, agent: event.target.value })}>
              <option value="">Все</option>
              {Object.entries(AGENT_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <label className="field">
            <span>Автор</span>
            <select value={filters.author} onChange={(event) => setFilters({ ...filters, author: event.target.value })}>
              <option value="">Все</option>
              {authors.map((author) => <option key={author} value={author}>{author}</option>)}
            </select>
          </label>
          <label className="field">
            <span>Карточка №</span>
            <input inputMode="numeric" value={filters.signal}
              onChange={(event) => setFilters({ ...filters, signal: event.target.value.replace(/\D/g, "") })} />
          </label>
        </div>
        {page === null ? <SkeletonList rows={4} label="Загружаем журнал ОС" /> : null}
        {page !== null && entries.length === 0 ? <div className="emptyState">По этим фильтрам в журнале пусто.</div> : null}
        <div className="journalList">
          {entries.map((entry) => (
            <JournalEntryCard key={entry.key} entry={entry} highlighted={entry.key === highlighted} busy={busy}
              onDecide={decide}
              onUndo={(one) => void act(() => undoAction(one.kind === "mark" ? Number(one.action_id) : one.id),
                "Отменено: карточка в прежнем состоянии")}
              onClose={(one) => void act(() => closeTask(one.id), "Задача выборки закрыта")}
              onRollback={(one) => rollback(one.id)}
              onRecheck={(one) => void act(() => recheckRule(one.id), "Автопроверка поставлена: итог появится в журнале")} />
          ))}
        </div>
      </section>

      <ExpertRulesPanel rules={rules} busy={busy} onRollback={rollback} />

      {isAdmin && overview?.can_configure ? <ContourSettingsPanel onError={handleError} showToast={showToast} /> : null}
    </section>
  );
}
