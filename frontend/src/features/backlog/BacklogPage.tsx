import { useEffect, useMemo, useState, type DragEvent } from "react";
import { addBacklogTaskComment, createBacklogTask, getBacklog, updateBacklogTask, updateBacklogTaskStatus } from "../../api/backlog";
import type { BacklogPayload, BacklogTask, BacklogTaskStatus } from "../../api/types";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

const statusColumns: Array<{ id: BacklogTaskStatus; label: string; hint: string; short: string }> = [
  { id: "new", label: "Новое", hint: "Ждет разбора", short: "Новые" },
  { id: "in_progress", label: "В работе", hint: "Активный этап", short: "В работе" },
  { id: "paused", label: "Отложено", hint: "Пауза или блокер", short: "Пауза" },
  { id: "done", label: "Готово", hint: "Закрытые задачи", short: "Готово" },
  { id: "rejected", label: "Отклонено", hint: "Не берем в работу", short: "Отклонено" },
];

const priorities = ["P1", "P2", "P3", "P4"];

const sectionLabels: Record<BacklogTask["section"], string> = {
  plan: "План",
  tech: "Техдолг",
  inbox: "Входящие",
};

const statusLabels = Object.fromEntries(statusColumns.map((column) => [column.id, column.label])) as Record<BacklogTaskStatus, string>;

export function BacklogPage({ onUnauthorized, showToast }: Props) {
  const [payload, setPayload] = useState<BacklogPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [savingTaskId, setSavingTaskId] = useState<string | null>(null);
  const [draggedTaskId, setDraggedTaskId] = useState<string | null>(null);
  const [dropTargetStatus, setDropTargetStatus] = useState<BacklogTaskStatus | null>(null);
  const [title, setTitle] = useState("");
  const [details, setDetails] = useState("");
  const [dueDate, setDueDate] = useState("");
  const [priority, setPriority] = useState("P3");
  const [createStatus, setCreateStatus] = useState<BacklogTaskStatus>("new");
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<BacklogTaskStatus | "">("");
  const [priorityFilter, setPriorityFilter] = useState("");
  const [sectionFilter, setSectionFilter] = useState<BacklogTask["section"] | "">("");
  const [commentDrafts, setCommentDrafts] = useState<Record<string, string>>({});

  useEffect(() => {
    void reload();
  }, []);

  const tasks = payload?.tasks ?? [];
  const filteredTasks = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return tasks.filter((task) => {
      const searchable = [
        task.id,
        task.priority,
        task.title,
        task.details,
        task.area,
        task.due_date,
        ...(task.comments ?? []).map((comment) => `${comment.author} ${comment.text}`),
        sectionLabels[task.section],
        statusLabels[task.status],
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return (
        (!needle || searchable.includes(needle)) &&
        (!statusFilter || task.status === statusFilter) &&
        (!priorityFilter || task.priority === priorityFilter) &&
        (!sectionFilter || task.section === sectionFilter)
      );
    });
  }, [priorityFilter, query, sectionFilter, statusFilter, tasks]);

  const metrics = useMemo(() => {
    const total = tasks.length;
    const done = countByStatus(tasks, "done");
    const rejected = countByStatus(tasks, "rejected");
    const active = countByStatus(tasks, "in_progress");
    const paused = countByStatus(tasks, "paused");
    const overdue = tasks.filter((task) => task.status !== "done" && task.status !== "rejected" && dueState(task.due_date).tone === "overdue").length;
    const dueSoon = tasks.filter((task) => task.status !== "done" && task.status !== "rejected" && dueState(task.due_date).tone === "soon").length;
    const open = Math.max(total - done - rejected, 0);
    const planned = Math.max(total - rejected, 0);
    const progress = planned ? Math.round((done / planned) * 100) : 0;
    return { total, done, rejected, active, paused, overdue, dueSoon, open, progress };
  }, [tasks]);

  async function reload() {
    try {
      setLoading(true);
      setPayload(await getBacklog());
    } catch (error) {
      handleError(error, "Не удалось загрузить бэклог");
    } finally {
      setLoading(false);
    }
  }

  function handleError(error: unknown, fallback: string) {
    const statusCode = typeof error === "object" && error && "status" in error ? Number(error.status) : 0;
    if (statusCode === 401) {
      onUnauthorized();
      return;
    }
    showToast(error instanceof Error ? error.message : fallback, "error");
  }

  async function submitTask() {
    if (!title.trim()) {
      showToast("Напишите название задачи", "error");
      return;
    }
    try {
      setSavingTaskId("new");
      await createBacklogTask({ title: title.trim(), details: details.trim() || undefined, due_date: dueDate || undefined, priority, status: createStatus });
      setTitle("");
      setDetails("");
      setDueDate("");
      setCreateStatus("new");
      showToast("Задача добавлена и синхронизирована с BACKLOG.md");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось создать задачу");
    } finally {
      setSavingTaskId(null);
    }
  }

  async function changeStatus(task: BacklogTask, nextStatus: BacklogTaskStatus) {
    if (task.status === nextStatus) return;
    const previousPayload = payload;
    setSavingTaskId(task.id);
    setPayload((current) => updatePayloadTask(current, { ...task, status: nextStatus, updated: "синхронизация" }));
    try {
      const updated = await updateBacklogTaskStatus(task.id, nextStatus);
      setPayload((current) => updatePayloadTask(current, updated));
      showToast(`«${task.title}» → ${statusLabels[nextStatus]}`);
    } catch (error) {
      setPayload(previousPayload);
      handleError(error, "Не удалось обновить статус");
    } finally {
      setSavingTaskId(null);
    }
  }

  async function changeDueDate(task: BacklogTask, nextDueDate: string) {
    const previousPayload = payload;
    setSavingTaskId(task.id);
    setPayload((current) => updatePayloadTask(current, { ...task, due_date: nextDueDate || null }));
    try {
      const updated = await updateBacklogTask(task.id, { due_date: nextDueDate || null });
      setPayload((current) => updatePayloadTask(current, updated));
      showToast(nextDueDate ? "Дедлайн обновлен" : "Дедлайн очищен");
    } catch (error) {
      setPayload(previousPayload);
      handleError(error, "Не удалось обновить дедлайн");
    } finally {
      setSavingTaskId(null);
    }
  }

  async function submitComment(task: BacklogTask) {
    const text = (commentDrafts[task.id] ?? "").trim();
    if (!text) {
      showToast("Напишите комментарий", "error");
      return;
    }
    try {
      setSavingTaskId(task.id);
      const updated = await addBacklogTaskComment(task.id, text);
      setPayload((current) => updatePayloadTask(current, updated));
      setCommentDrafts((drafts) => ({ ...drafts, [task.id]: "" }));
      showToast("Комментарий добавлен");
    } catch (error) {
      handleError(error, "Не удалось добавить комментарий");
    } finally {
      setSavingTaskId(null);
    }
  }

  function startDragging(task: BacklogTask, event: DragEvent<HTMLElement>) {
    if (savingTaskId === task.id) {
      event.preventDefault();
      return;
    }
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", task.id);
    setDraggedTaskId(task.id);
  }

  function allowColumnDrop(status: BacklogTaskStatus, event: DragEvent<HTMLElement>) {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    setDropTargetStatus(status);
  }

  async function dropTask(status: BacklogTaskStatus, event: DragEvent<HTMLElement>) {
    event.preventDefault();
    const taskId = event.dataTransfer.getData("text/plain") || draggedTaskId;
    setDraggedTaskId(null);
    setDropTargetStatus(null);
    const task = tasks.find((item) => item.id === taskId);
    if (!task || task.status === status) return;
    await changeStatus(task, status);
  }

  function clearFilters() {
    setQuery("");
    setStatusFilter("");
    setPriorityFilter("");
    setSectionFilter("");
  }

  return (
    <section className="screenStack backlogWorkspace">
      <header className="screenHeader backlogHero">
        <div>
          <div className="eyebrow">Проектный поток</div>
          <h1>Задачи проекта</h1>
          <p>Канбан-доска поверх репозиторного бэклога. Создание, этапы и перетаскивание сразу синхронизируются с BACKLOG.md.</p>
        </div>
        <div className="panelActions">
          <button type="button" className="ghostButton compactButton" onClick={() => void reload()} disabled={loading}>
            Обновить
          </button>
          <div className="statusPill">{payload ? `${payload.tasks.length} задач` : "Загрузка"}</div>
        </div>
      </header>

      <section className="backlogOverview">
        <article className="panel backlogProgressPanel">
          <div className="panelKicker">Прогресс проекта</div>
          <div className="backlogProgressHead">
            <strong>{metrics.progress}%</strong>
            <span>{metrics.done} готово из {Math.max(metrics.total - metrics.rejected, 0)}</span>
          </div>
          <div className="backlogProgressTrack" aria-label={`Прогресс проекта ${metrics.progress}%`}>
            <div style={{ width: `${metrics.progress}%` }} />
          </div>
          <div className="backlogProgressMeta">
            <span>{metrics.open} открыто</span>
            <span>{metrics.active} в работе</span>
            <span>{metrics.paused} на паузе</span>
            <span>{metrics.overdue} просрочено</span>
          </div>
        </article>

        <section className="statsGridReact backlogStats" aria-label="Сводка по этапам">
          {statusColumns.map((column) => (
            <div className={`statCardReact backlogStatusStat ${column.id}`} key={column.id}>
              <strong>{payload?.counts[column.id] ?? 0}</strong>
              <span>{column.short}</span>
            </div>
          ))}
        </section>
      </section>

      <section className="panel backlogComposer">
        <div className="panelHeader">
          <h2>Создать задачу</h2>
          <span className="badge emphasis">Синхронизация: BACKLOG.md</span>
        </div>
        <div className="backlogCreateGrid">
          <label className="field backlogTitleField">
            <span>Название</span>
            <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Что нужно сделать по проекту" />
          </label>
          <label className="field">
            <span>Приоритет</span>
            <select value={priority} onChange={(event) => setPriority(event.target.value)}>
              {priorities.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Стартовый этап</span>
            <select value={createStatus} onChange={(event) => setCreateStatus(event.target.value as BacklogTaskStatus)}>
              {statusColumns.map((column) => (
                <option key={column.id} value={column.id}>
                  {column.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Дедлайн</span>
            <input type="date" value={dueDate} onChange={(event) => setDueDate(event.target.value)} />
          </label>
          <label className="field backlogDetailsField">
            <span>Описание</span>
            <textarea value={details} onChange={(event) => setDetails(event.target.value)} placeholder="Контекст, критерий готовности или ссылка" rows={2} />
          </label>
          <button type="button" className="primaryButton backlogCreateButton" onClick={() => void submitTask()} disabled={savingTaskId === "new"}>
            {savingTaskId === "new" ? "Создаем..." : "Создать"}
          </button>
        </div>
      </section>

      <section className="panel backlogBoardPanel">
        <div className="panelHeader backlogToolbar">
          <div>
            <h2>Этапы</h2>
            <span>{filteredTasks.length} из {tasks.length} задач в текущем срезе</span>
          </div>
          <div className="backlogFilters">
            <label className="field backlogSearch">
              <span>Поиск</span>
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Название, зона, #ID" />
            </label>
            <label className="field compactField">
              <span>Этап</span>
              <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as BacklogTaskStatus | "")}>
                <option value="">Все</option>
                {statusColumns.map((column) => (
                  <option key={column.id} value={column.id}>
                    {column.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field compactField">
              <span>Приоритет</span>
              <select value={priorityFilter} onChange={(event) => setPriorityFilter(event.target.value)}>
                <option value="">Все</option>
                {priorities.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </label>
            <label className="field compactField">
              <span>Тип</span>
              <select value={sectionFilter} onChange={(event) => setSectionFilter(event.target.value as BacklogTask["section"] | "")}>
                <option value="">Все</option>
                <option value="plan">План</option>
                <option value="tech">Техдолг</option>
                <option value="inbox">Входящие</option>
              </select>
            </label>
            <button type="button" className="ghostButton compactButton" onClick={clearFilters} disabled={!query && !statusFilter && !priorityFilter && !sectionFilter}>
              Сбросить
            </button>
          </div>
        </div>

        {loading ? <div className="emptyState">Загружаем задачи из репозитория...</div> : null}

        <div className="backlogBoard">
          {statusColumns.map((column) => {
            const columnTasks = filteredTasks.filter((task) => task.status === column.id);
            return (
              <section
                className={`backlogColumn ${dropTargetStatus === column.id ? "dropTarget" : ""}`}
                key={column.id}
                onDragOver={(event) => allowColumnDrop(column.id, event)}
                onDragLeave={() => setDropTargetStatus((status) => (status === column.id ? null : status))}
                onDrop={(event) => void dropTask(column.id, event)}
              >
                <div className="backlogColumnHeader">
                  <div>
                    <h3>{column.label}</h3>
                    <span>{column.hint}</span>
                  </div>
                  <strong>{columnTasks.length}</strong>
                </div>
                <div className="backlogColumnList">
                  {columnTasks.map((task) => (
                    <BacklogCard
                      key={`${task.section}-${task.id}`}
                      task={task}
                      disabled={savingTaskId === task.id}
                      dragging={draggedTaskId === task.id}
                      onChangeStatus={changeStatus}
                      onChangeDueDate={changeDueDate}
                      commentDraft={commentDrafts[task.id] ?? ""}
                      onCommentDraftChange={(value) => setCommentDrafts((drafts) => ({ ...drafts, [task.id]: value }))}
                      onAddComment={submitComment}
                      onDragStart={startDragging}
                      onDragEnd={() => {
                        setDraggedTaskId(null);
                        setDropTargetStatus(null);
                      }}
                    />
                  ))}
                  {!loading && columnTasks.length === 0 ? <div className="backlogEmpty">Перетащите сюда задачу</div> : null}
                </div>
              </section>
            );
          })}
        </div>
      </section>
    </section>
  );
}

function BacklogCard(props: {
  task: BacklogTask;
  disabled: boolean;
  dragging: boolean;
  onChangeStatus: (task: BacklogTask, status: BacklogTaskStatus) => Promise<void>;
  onChangeDueDate: (task: BacklogTask, dueDate: string) => Promise<void>;
  commentDraft: string;
  onCommentDraftChange: (value: string) => void;
  onAddComment: (task: BacklogTask) => Promise<void>;
  onDragStart: (task: BacklogTask, event: DragEvent<HTMLElement>) => void;
  onDragEnd: () => void;
}) {
  const quickStatus = nextUsefulStatus(props.task.status);
  const deadline = dueState(props.task.due_date);
  const recentComments = (props.task.comments ?? []).slice(-2);

  return (
    <article
      className={`backlogCard ${props.task.section} ${priorityClass(props.task.priority)} ${props.dragging ? "dragging" : ""}`}
      draggable={!props.disabled}
      onDragStart={(event) => props.onDragStart(props.task, event)}
      onDragEnd={props.onDragEnd}
      aria-label={`Задача ${props.task.title}. Перетащите в другой столбик, чтобы изменить этап.`}
      title="Перетащите карточку в другой столбик, чтобы изменить этап"
    >
      <div className="backlogCardTop">
        <span className="badge">{props.task.priority}</span>
        <span className="backlogSection">{sectionLabels[props.task.section]}</span>
      </div>
      <h4>{props.task.title}</h4>
      {props.task.details ? <p>{props.task.details}</p> : null}
      {props.task.area ? <div className="backlogArea">{props.task.area}</div> : null}
      <div className="backlogDueRow">
        <label className="field">
          <span>Дедлайн</span>
          <input type="date" value={props.task.due_date ?? ""} disabled={props.disabled} onChange={(event) => void props.onChangeDueDate(props.task, event.target.value)} />
        </label>
        <span className={`backlogDueBadge ${deadline.tone}`}>{deadline.label}</span>
      </div>
      <div className="backlogMeta">
        <span>#{props.task.id}</span>
        <span>{props.disabled ? "сохраняем..." : props.task.updated || "без даты"}</span>
      </div>
      {recentComments.length ? (
        <div className="backlogComments">
          {recentComments.map((comment) => (
            <div className="backlogComment" key={comment.id}>
              <strong>{comment.author}</strong>
              <span>{comment.created_at}</span>
              <p>{comment.text}</p>
            </div>
          ))}
        </div>
      ) : null}
      <div className="backlogCommentComposer">
        <textarea
          value={props.commentDraft}
          disabled={props.disabled}
          onChange={(event) => props.onCommentDraftChange(event.target.value)}
          placeholder="Комментарий"
          rows={2}
        />
        <button type="button" className="ghostButton compactButton" disabled={props.disabled || !props.commentDraft.trim()} onClick={() => void props.onAddComment(props.task)}>
          Добавить
        </button>
      </div>
      <div className="backlogCardControls">
        <label className="field">
          <span>Этап</span>
          <select
            value={props.task.status}
            disabled={props.disabled}
            onChange={(event) => void props.onChangeStatus(props.task, event.target.value as BacklogTaskStatus)}
          >
            {statusColumns.map((column) => (
              <option key={column.id} value={column.id}>
                {column.label}
              </option>
            ))}
          </select>
        </label>
        {quickStatus ? (
          <button type="button" className="ghostButton compactButton" disabled={props.disabled} onClick={() => void props.onChangeStatus(props.task, quickStatus)}>
            {quickStatus === "done" ? "Готово" : "В работу"}
          </button>
        ) : null}
      </div>
    </article>
  );
}

function nextUsefulStatus(status: BacklogTaskStatus): BacklogTaskStatus | null {
  if (status === "new" || status === "paused") return "in_progress";
  if (status === "in_progress") return "done";
  return null;
}

function priorityClass(priority: string): string {
  const match = priority.match(/^P([1-4])$/);
  return match ? `priority${match[1]}` : "priorityOther";
}

function dueState(value?: string | null): { label: string; tone: "none" | "ok" | "soon" | "today" | "overdue" } {
  if (!value) return { label: "Без дедлайна", tone: "none" };
  const today = startOfDay(new Date());
  const due = startOfDay(new Date(`${value}T00:00:00`));
  const days = Math.round((due.getTime() - today.getTime()) / 86_400_000);
  if (days < 0) return { label: `Просрочено ${Math.abs(days)} дн.`, tone: "overdue" };
  if (days === 0) return { label: "Сегодня", tone: "today" };
  if (days <= 3) return { label: `Через ${days} дн.`, tone: "soon" };
  return { label: formatDate(value), tone: "ok" };
}

function startOfDay(value: Date): Date {
  return new Date(value.getFullYear(), value.getMonth(), value.getDate());
}

function formatDate(value: string): string {
  const [year, month, day] = value.split("-");
  return year && month && day ? `${day}.${month}.${year}` : value;
}

function countByStatus(tasks: BacklogTask[], status: BacklogTaskStatus): number {
  return tasks.filter((task) => task.status === status).length;
}

function updatePayloadTask(payload: BacklogPayload | null, task: BacklogTask): BacklogPayload | null {
  if (!payload) return payload;
  const tasks = payload.tasks.map((item) => (item.id === task.id ? { ...item, ...task } : item));
  return { ...payload, tasks, counts: buildCounts(tasks) };
}

function buildCounts(tasks: BacklogTask[]): Record<BacklogTaskStatus, number> {
  return statusColumns.reduce(
    (counts, column) => ({ ...counts, [column.id]: countByStatus(tasks, column.id) }),
    { new: 0, in_progress: 0, paused: 0, done: 0, rejected: 0 } as Record<BacklogTaskStatus, number>,
  );
}
