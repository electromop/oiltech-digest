import { useEffect, useMemo, useState } from "react";
import { createBacklogTask, getBacklog, updateBacklogTaskStatus } from "../../api/backlog";
import type { BacklogPayload, BacklogTask, BacklogTaskStatus } from "../../api/types";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

const statusColumns: Array<{ id: BacklogTaskStatus; label: string; hint: string }> = [
  { id: "new", label: "Новое", hint: "Ждет разбора" },
  { id: "in_progress", label: "В работе", hint: "Активный этап" },
  { id: "paused", label: "Отложено", hint: "Пауза или блокер" },
  { id: "done", label: "Готово", hint: "Закрытые задачи" },
  { id: "rejected", label: "Отклонено", hint: "Не берем в работу" },
];

const priorities = ["P1", "P2", "P3", "P4"];

const sectionLabels: Record<BacklogTask["section"], string> = {
  plan: "План",
  tech: "Техдолг",
  inbox: "Входящие",
};

export function BacklogPage({ onUnauthorized, showToast }: Props) {
  const [payload, setPayload] = useState<BacklogPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [savingTaskId, setSavingTaskId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [priority, setPriority] = useState("P3");
  const [statusFilter, setStatusFilter] = useState<BacklogTaskStatus | "">("");

  useEffect(() => {
    void reload();
  }, []);

  const tasks = payload?.tasks ?? [];
  const visibleTasks = useMemo(() => {
    return statusFilter ? tasks.filter((task) => task.status === statusFilter) : tasks;
  }, [statusFilter, tasks]);

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
      await createBacklogTask({ title: title.trim(), priority, status: "new" });
      setTitle("");
      showToast("Задача добавлена в BACKLOG.md");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось создать задачу");
    } finally {
      setSavingTaskId(null);
    }
  }

  async function changeStatus(task: BacklogTask, nextStatus: BacklogTaskStatus) {
    if (task.status === nextStatus) return;
    try {
      setSavingTaskId(task.id);
      await updateBacklogTaskStatus(task.id, nextStatus);
      showToast("Статус синхронизирован с BACKLOG.md");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось обновить статус");
    } finally {
      setSavingTaskId(null);
    }
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <div className="eyebrow">Проектный поток</div>
          <h1>Задачи проекта</h1>
          <p>Доска статусов поверх репозиторного бэклога. Изменения сразу записываются в BACKLOG.md.</p>
        </div>
        <div className="panelActions">
          <button type="button" className="ghostButton compactButton" onClick={() => void reload()} disabled={loading}>
            Обновить
          </button>
          <div className="statusPill">{payload ? `${payload.tasks.length} задач` : "Загрузка"}</div>
        </div>
      </header>

      <section className="statsGridReact backlogStats">
        {statusColumns.map((column) => (
          <div className="statCardReact" key={column.id}>
            <strong>{payload?.counts[column.id] ?? 0}</strong>
            <span>{column.label}</span>
          </div>
        ))}
      </section>

      <section className="panel backlogComposer">
        <div className="panelHeader">
          <h2>Создать задачу</h2>
          <span className="badge emphasis">Синхронизация: BACKLOG.md</span>
        </div>
        <div className="backlogCreateGrid">
          <label className="field">
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
          <button type="button" className="primaryButton" onClick={() => void submitTask()} disabled={savingTaskId === "new"}>
            Создать
          </button>
        </div>
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Этапы</h2>
          <label className="field compactField">
            <span>Фильтр</span>
            <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as BacklogTaskStatus | "")}>
              <option value="">Все статусы</option>
              {statusColumns.map((column) => (
                <option key={column.id} value={column.id}>
                  {column.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        {loading ? <div className="emptyState">Загружаем задачи из репозитория…</div> : null}

        <div className="backlogBoard">
          {statusColumns.map((column) => {
            const columnTasks = visibleTasks.filter((task) => task.status === column.id);
            return (
              <section className="backlogColumn" key={column.id}>
                <div className="backlogColumnHeader">
                  <div>
                    <h3>{column.label}</h3>
                    <span>{column.hint}</span>
                  </div>
                  <strong>{columnTasks.length}</strong>
                </div>
                <div className="backlogColumnList">
                  {columnTasks.map((task) => (
                    <BacklogCard key={`${task.section}-${task.id}`} task={task} disabled={savingTaskId === task.id} onChangeStatus={changeStatus} />
                  ))}
                  {!loading && columnTasks.length === 0 ? <div className="backlogEmpty">Нет задач</div> : null}
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
  onChangeStatus: (task: BacklogTask, status: BacklogTaskStatus) => Promise<void>;
}) {
  return (
    <article className={`backlogCard ${props.task.section}`}>
      <div className="backlogCardTop">
        <span className="badge">{props.task.priority}</span>
        <span className="backlogSection">{sectionLabels[props.task.section]}</span>
      </div>
      <h4>{props.task.title}</h4>
      {props.task.details ? <p>{props.task.details}</p> : null}
      <div className="backlogMeta">
        <span>#{props.task.id}</span>
        <span>{props.task.updated || "без даты"}</span>
      </div>
      <label className="field">
        <span>Этап</span>
        <select
          value={props.task.status}
          disabled={props.disabled}
          onChange={(event) => void props.onChangeStatus(props.task, event.target.value as BacklogTaskStatus)}
        >
          <option value="new">Новое</option>
          <option value="in_progress">В работе</option>
          <option value="paused">Отложено</option>
          <option value="done">Готово</option>
          <option value="rejected">Отклонено</option>
        </select>
      </label>
    </article>
  );
}
