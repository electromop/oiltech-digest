import { useEffect, useState } from "react";
import { MIN_PASSWORD_LENGTH, PASSWORD_TOO_SHORT } from "../shared/passwordRules";
import { SkeletonList } from "../shared/Skeleton";
import { createUser, deleteUser, listUsers, updateUser } from "../../api/users";
import type { User } from "../../api/types";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
  currentUserId: number;
};

const ROLE_LABEL: Record<string, string> = { admin: "Администратор", user: "Пользователь" };

// Допуск по коду (#113): есть с даты или ждёт кода, пока подтверждающие его не передали. Учётку из регистрации на
// экране входа (с 16.11) отмечает подпись под строкой: администратор за неё не ручался.
function admissionLabel(target: User) {
  return target.admitted_at ? `есть с ${formatDay(target.admitted_at)}` : "ждёт кода";
}

function originNote(target: User) {
  const day = target.created_at ? ` ${formatDay(target.created_at)}` : "";
  return `Учётку завели сами на экране входа${day}.`;
}

function formatDay(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString("ru-RU", { timeZone: "Europe/Moscow" });
}

export function UsersPage({ onUnauthorized, showToast, currentUserId }: Props) {
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"admin" | "user">("user");

  useEffect(() => {
    void reload();
  }, []);

  function handleError(error: unknown, fallback: string) {
    const status = typeof error === "object" && error && "status" in error ? Number((error as { status: number }).status) : 0;
    if (status === 401) {
      onUnauthorized();
      return;
    }
    showToast(error instanceof Error ? error.message : fallback, "error");
  }

  async function reload() {
    try {
      setLoading(true);
      setUsers(await listUsers());
    } catch (error) {
      handleError(error, "Не удалось загрузить пользователей");
    } finally {
      setLoading(false);
    }
  }

  async function handleCreate() {
    if (!email.trim() || password.length < MIN_PASSWORD_LENGTH) {
      showToast(`Укажите email и пароль не короче ${MIN_PASSWORD_LENGTH} символов`, "error");
      return;
    }
    try {
      setBusy(true);
      await createUser(email.trim(), password, role);
      setEmail("");
      setPassword("");
      setRole("user");
      showToast("Пользователь создан");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось создать пользователя");
    } finally {
      setBusy(false);
    }
  }

  async function handleRoleChange(target: User, nextRole: "admin" | "user") {
    try {
      setBusy(true);
      await updateUser(target.id, { role: nextRole });
      showToast(`Роль ${target.email} → ${ROLE_LABEL[nextRole]}`);
      await reload();
    } catch (error) {
      handleError(error, "Не удалось изменить роль");
    } finally {
      setBusy(false);
    }
  }

  async function handleResetPassword(target: User) {
    const next = window.prompt(`Новый пароль для ${target.email} (не короче ${MIN_PASSWORD_LENGTH} символов):`, "");
    if (next === null) return;
    if (next.length < MIN_PASSWORD_LENGTH) {
      showToast(PASSWORD_TOO_SHORT, "error");
      return;
    }
    try {
      setBusy(true);
      await updateUser(target.id, { password: next });
      showToast("Пароль обновлён");
    } catch (error) {
      handleError(error, "Не удалось сменить пароль");
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(target: User) {
    if (!window.confirm(`Удалить пользователя ${target.email}?`)) return;
    try {
      setBusy(true);
      await deleteUser(target.id);
      showToast("Пользователь удалён");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось удалить пользователя");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Пользователи</h1>
        </div>
        <div className="statusPill">{users.length} пользователей</div>
      </header>

      <section className="panel">
        <div className="panelHeader">
          <h2>Добавить пользователя</h2>
        </div>
        <div className="userAddGrid">
          <label className="field">
            <span>Эл. почта</span>
            <input value={email} onChange={(event) => setEmail(event.target.value)} placeholder="user@example.com" />
          </label>
          <label className="field">
            <span>Пароль</span>
            <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder={`Не короче ${MIN_PASSWORD_LENGTH} символов`} />
          </label>
          <label className="field">
            <span>Роль</span>
            <select value={role} onChange={(event) => setRole(event.target.value as "admin" | "user")}>
              <option value="user">Пользователь</option>
              <option value="admin">Администратор</option>
            </select>
          </label>
          <button type="button" className="primaryButton userAddButton" disabled={busy} onClick={() => void handleCreate()}>
            Создать
          </button>
        </div>
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Список пользователей</h2>
          <div className="settingsActions">
            <button type="button" className="ghostButton" onClick={() => void reload()}>Обновить</button>
          </div>
        </div>
        {loading ? (
          <SkeletonList rows={4} label="Загружаем пользователей…" />
        ) : (
          <div className="settingsStack">
            {users.map((target) => {
              const isSelf = target.id === currentUserId;
              return (
                <div className="tagGroupCard" key={target.id}>
                  <div className="userRow">
                    <label className="field">
                      <span>Эл. почта</span>
                      <input value={target.email} readOnly />
                    </label>
                    <label className="field">
                      <span>Роль</span>
                      <select
                        value={target.role || "user"}
                        onChange={(event) => void handleRoleChange(target, event.target.value as "admin" | "user")}
                      >
                        <option value="user">Пользователь</option>
                        <option value="admin">Администратор</option>
                      </select>
                    </label>
                    <label className="field">
                      <span>Допуск</span>
                      <input value={admissionLabel(target)} readOnly />
                    </label>
                    <div className="settingsActions">
                      <button type="button" className="ghostButton" onClick={() => void handleResetPassword(target)}>
                        Сменить пароль
                      </button>
                      <button
                        type="button"
                        className="ghostButton dangerButton"
                        disabled={isSelf}
                        title={isSelf ? "Нельзя удалить себя" : undefined}
                        onClick={() => void handleDelete(target)}
                      >
                        Удалить
                      </button>
                    </div>
                  </div>
                  {target.self_registered ? <p className="userRowNote">{originNote(target)}</p> : null}
                </div>
              );
            })}
          </div>
        )}
      </section>
    </section>
  );
}
