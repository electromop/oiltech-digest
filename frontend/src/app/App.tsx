import { useEffect, useState } from "react";
import { listArticles } from "../api/articles";
import { ApiError } from "../api/client";
import { getSession, login, logout, register } from "../api/auth";
import { getDashboardStats } from "../api/stats";
import type { Article, DashboardStats, User } from "../api/types";
import { ArticlesPage } from "../features/articles/ArticlesPage";
import { DigestPage } from "../features/digest/DigestPage";
import { JobsPage } from "../features/jobs/JobsPage";
import { MaintenancePage } from "../features/maintenance/MaintenancePage";
import { ScoringPage } from "../features/scoring/ScoringPage";
import { SourcesPage } from "../features/sources/SourcesPage";
import { TagsPage } from "../features/tags/TagsPage";

type ScreenId = "articles" | "digest" | "sources" | "scoring" | "tags" | "jobs" | "maintenance";

type ScreenDef = {
  id: ScreenId;
  label: string;
  eyebrow: string;
  title: string;
  description: string;
  status: string;
};

type NavGroup = {
  label: string;
  screens: ScreenId[];
};

const screens: ScreenDef[] = [
  {
    id: "articles",
    label: "Сигналы",
    eyebrow: "Editorial Flow",
    title: "Поток сигналов",
    description: "Рабочий каталог сигналов: фильтры, группировка, AI-суть, score и редактор статусов уже живут в новом интерфейсе.",
    status: "Экран активен",
  },
  {
    id: "digest",
    label: "Месячный дайджест",
    eyebrow: "Editorial Output",
    title: "Сборка выпуска",
    description: "Выборка материалов, preview, draft и экспорт опираются на тот же backend API, но уже через новый интерфейс.",
    status: "Экран активен",
  },
  {
    id: "sources",
    label: "Источники",
    eyebrow: "Source Control",
    title: "Каталог источников",
    description: "Упрощённые карточки, фильтры, диагностика и настройки парсинга теперь собраны в отдельном экране.",
    status: "Экран активен",
  },
  {
    id: "scoring",
    label: "Скоринг",
    eyebrow: "Config Surface",
    title: "Профиль критериев",
    description: "Весы критериев, редактор описаний и нормализация профиля уже перенесены в новый интерфейс.",
    status: "Экран активен",
  },
  {
    id: "tags",
    label: "Теги",
    eyebrow: "Taxonomy",
    title: "Дерево тем",
    description: "Parent/subtag структура, включение, редактирование и сохранение теперь тоже в новом интерфейсе.",
    status: "Экран активен",
  },
];

const appHighlights = [
  "Общий auth gate и session flow",
  "Typed API client поверх текущего backend",
  "Источники и диагностика",
  "Месячный дайджест и экспорт",
  "Скоринг и дерево тегов",
  "Каталог сигналов",
];

const navGroups: NavGroup[] = [
  {
    label: "Работа",
    screens: ["articles", "digest", "sources"],
  },
  {
    label: "Настройки",
    screens: ["scoring", "tags"],
  },
];

function initialScreenFromUrl(): ScreenId {
  const value = new URLSearchParams(window.location.search).get("screen");
  if (value === "jobs") return "jobs";
  if (value === "maintenance") return "maintenance";
  return "articles";
}

export function App() {
  const [activeScreen, setActiveScreen] = useState<ScreenId>(initialScreenFromUrl);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [toast, setToast] = useState<{ text: string; tone: "default" | "error" } | null>(null);
  const [articles, setArticles] = useState<Article[]>([]);
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const active = screens.find((screen) => screen.id === activeScreen) ?? screens[0];

  useEffect(() => {
    void loadSession();
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 2800);
    return () => window.clearTimeout(timer);
  }, [toast]);

  let currentScreen = (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <div className="eyebrow">{active.eyebrow}</div>
          <h1>{active.title}</h1>
          <p>{active.description}</p>
        </div>
        <div className="statusPill">{active.status}</div>
      </header>

      <section className="heroGrid">
        <article className="panel accentPanel">
          <div className="panelKicker">Admin UI</div>
          <h2>Новая админка уже собрана вокруг текущего API</h2>
          <p>
            Новый интерфейс уже покрывает ключевые редакторские и конфигурационные сценарии.
            `app.html` пока остаётся как запасной fallback, но основной каркас интерфейса уже здесь.
          </p>
        </article>

        <article className="panel">
          <div className="panelKicker">Что уже есть</div>
          <p>
            Мы переехали без смены backend-контрактов: это упрощает проверку parity и даёт
            возможность спокойно дотягивать поведение экран за экраном.
          </p>
        </article>
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Текущее покрытие</h2>
          <span className="badge">{appHighlights.length} зон</span>
        </div>
        <div className="stepList">
          {appHighlights.map((step, index) => (
            <div className="stepCard" key={step}>
              <div className="stepIndex">{String(index + 1).padStart(2, "0")}</div>
              <div className="stepText">{step}</div>
            </div>
          ))}
        </div>
      </section>
    </section>
  );

  if (activeScreen === "sources") {
    currentScreen = <SourcesPage onUnauthorized={() => setUser(null)} showToast={showToast} />;
  }

  if (activeScreen === "articles") {
    currentScreen = (
      <ArticlesPage
        onUnauthorized={() => setUser(null)}
        showToast={showToast}
        initialArticles={articles}
        initialStats={stats}
        onArticlesReloaded={setArticles}
        onStatsReloaded={setStats}
      />
    );
  }

  if (activeScreen === "digest") {
    currentScreen = <DigestPage onUnauthorized={() => setUser(null)} showToast={showToast} onArticlesChanged={() => void loadDashboardData()} />;
  }

  if (activeScreen === "scoring") {
    currentScreen = <ScoringPage onUnauthorized={() => setUser(null)} showToast={showToast} />;
  }

  if (activeScreen === "tags") {
    currentScreen = <TagsPage onUnauthorized={() => setUser(null)} showToast={showToast} />;
  }

  if (activeScreen === "jobs") {
    currentScreen = <JobsPage onUnauthorized={() => setUser(null)} showToast={showToast} />;
  }

  if (activeScreen === "maintenance") {
    currentScreen = <MaintenancePage onUnauthorized={() => setUser(null)} showToast={showToast} />;
  }

  function switchScreen(screenId: ScreenId) {
    setActiveScreen(screenId);
    if (screenId === "jobs" || screenId === "maintenance") {
      window.history.replaceState(null, "", `?screen=${screenId}`);
      return;
    }
    if (window.location.search.includes("screen=jobs") || window.location.search.includes("screen=maintenance")) {
      window.history.replaceState(null, "", window.location.pathname || "/");
    }
  }

  async function loadSession() {
    try {
      setAuthLoading(true);
      const payload = await getSession();
      setUser(payload.user);
      await loadDashboardData();
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) {
        showToast(error instanceof Error ? error.message : "Не удалось загрузить сессию", "error");
      }
      setUser(null);
    } finally {
      setAuthLoading(false);
    }
  }

  async function submitAuth() {
    try {
      const payload = authMode === "register" ? await register(email.trim(), password) : await login(email.trim(), password);
      setUser(payload.user);
      await loadDashboardData();
      setPassword("");
      showToast(authMode === "register" ? "Регистрация завершена" : "Вход выполнен");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "Не удалось выполнить вход", "error");
    }
  }

  async function handleLogout() {
    try {
      await logout();
      setUser(null);
      setArticles([]);
      setStats(null);
      showToast("Сессия завершена");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "Не удалось выйти", "error");
    }
  }

  async function loadDashboardData() {
    const [articlesPayload, statsPayload] = await Promise.all([listArticles(), getDashboardStats()]);
    setArticles(articlesPayload);
    setStats(statsPayload);
  }

  function showToast(text: string, tone: "default" | "error" = "default") {
    setToast({ text, tone });
  }

  if (authLoading) {
    return <div className="splashScreen">Проверяем сессию…</div>;
  }

  if (!user) {
    return (
      <div className="authShellReact">
        <div className="authCardReact">
          <div className="eyebrow">OilTech Digest</div>
          <h1>{authMode === "register" ? "Регистрация" : "Вход в админку"}</h1>
          <p>
            {authMode === "register"
              ? "Создайте аккаунт, чтобы работать с сигналами, дайджестами и источниками."
              : "Войдите в аккаунт, чтобы продолжить работу с редакторской панелью."}
          </p>
          <label className="field">
            <span>Email</span>
            <input value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@example.com" />
          </label>
          <label className="field">
            <span>Пароль</span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="Не короче 8 символов"
            />
          </label>
          <div className="authActions">
            <button type="button" className="primaryButton" onClick={() => void submitAuth()}>
              {authMode === "register" ? "Зарегистрироваться" : "Войти"}
            </button>
          </div>
          <div className="authSwitchText">
            {authMode === "login" ? "Нет аккаунта?" : "Уже есть аккаунт?"}{" "}
            <button
              type="button"
              className="authSwitchLink"
              onClick={() => setAuthMode((mode) => (mode === "login" ? "register" : "login"))}
            >
              {authMode === "login" ? "Зарегистрироваться" : "Войти"}
            </button>
          </div>
        </div>
        {toast ? <div className={`toastReact ${toast.tone === "error" ? "error" : ""}`}>{toast.text}</div> : null}
      </div>
    );
  }

  return (
    <div className={sidebarCollapsed ? "shell sidebarCollapsed" : "shell"}>
      <aside className="sidebar">
        <div className="sidebarTopRow">
          <div className="brand">
            <div className="brandMark">OT</div>
            <div className="brandText">
              <div className="brandTitle">OilTech Digest</div>
              <div className="brandSubtitle">Admin</div>
            </div>
          </div>
        </div>

        <div className="brandMobileDivider" />

        <div className="sidebarGroups">
          {navGroups.map((group) => (
            <section className="sidebarGroup" key={group.label}>
              <div className="sidebarSection">{group.label}</div>
              <nav className="nav">
                {group.screens.map((screenId) => {
                  const screen = screens.find((item) => item.id === screenId);
                  if (!screen) return null;
                  return (
                    <button
                      key={screen.id}
                      type="button"
                      className={screen.id === activeScreen ? "navButton active" : "navButton"}
                      onClick={() => switchScreen(screen.id)}
                      title={sidebarCollapsed ? screen.label : undefined}
                    >
                      <span className="navButtonIcon">
                        <ScreenIcon screenId={screen.id} />
                      </span>
                      <span className="navButtonLabel">{screen.label}</span>
                    </button>
                  );
                })}
              </nav>
            </section>
          ))}
        </div>

        <div className="sidebarBottom">
          <div className="sidebarFoot">
            <div className="footLabel">{user.email}</div>
            <div className="footValue">Админка активна</div>
          </div>

          <div className="sidebarUtilityActions">
            <button
              type="button"
              className="navButton utilityButton"
              onClick={() => setSidebarCollapsed((value) => !value)}
              aria-label={sidebarCollapsed ? "Развернуть сайдбар" : "Свернуть сайдбар"}
              title={sidebarCollapsed ? "Развернуть сайдбар" : undefined}
            >
              <span className="navButtonIcon">
                <UtilityIcon kind="toggle" collapsed={sidebarCollapsed} />
              </span>
              <span className="navButtonLabel">{sidebarCollapsed ? "Развернуть меню" : "Свернуть меню"}</span>
            </button>
            <button
              type="button"
              className="navButton utilityButton danger"
              onClick={() => void handleLogout()}
              title={sidebarCollapsed ? "Выйти" : undefined}
            >
              <span className="navButtonIcon">
                <UtilityIcon kind="logout" collapsed={sidebarCollapsed} />
              </span>
              <span className="navButtonLabel">Выйти</span>
            </button>
          </div>
        </div>
      </aside>

      <main className="content">{currentScreen}</main>
      <nav className="mobileNav">
        {screens.map((screen) => (
          <button
            key={screen.id}
            type="button"
            className={screen.id === activeScreen ? "mobileNavButton active" : "mobileNavButton"}
            onClick={() => switchScreen(screen.id)}
            aria-label={screen.label}
          >
            <span className="mobileNavIcon">
              <ScreenIcon screenId={screen.id} />
            </span>
            <span className="mobileNavLabel">{screen.label}</span>
          </button>
        ))}
      </nav>
      {toast ? <div className={`toastReact ${toast.tone === "error" ? "error" : ""}`}>{toast.text}</div> : null}
    </div>
  );
}

function UtilityIcon(props: { kind: "toggle" | "logout"; collapsed: boolean }) {
  const common = { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.7 };

  if (props.kind === "toggle") {
    return (
      <svg
        {...common}
        style={{ transform: props.collapsed ? "rotate(180deg)" : "none", transition: "transform 220ms ease" }}
      >
        <path d="M10.5 3.5 5.5 8l5 4.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  return (
    <svg {...common}>
      <path d="M6 3.5H4.8A1.8 1.8 0 0 0 3 5.3v5.4a1.8 1.8 0 0 0 1.8 1.8H6" strokeLinecap="round" />
      <path d="M8.2 5.2 11 8l-2.8 2.8M11 8H6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function ScreenIcon(props: { screenId: ScreenId }) {
  const common = { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.6 };

  if (props.screenId === "articles") {
    return (
      <svg {...common}>
        <rect x="2.5" y="2.5" width="11" height="11" rx="2.2" />
        <path d="M5 6h6M5 8.5h6M5 11h4" />
      </svg>
    );
  }

  if (props.screenId === "digest") {
    return (
      <svg {...common}>
        <path d="M8 2.5 9.2 5l2.8.3-2 2 .5 2.7L8 8.7 5.5 10l.5-2.7-2-2L6.8 5 8 2.5Z" />
      </svg>
    );
  }

  if (props.screenId === "sources") {
    return (
      <svg {...common}>
        <path d="M3 4.5h10M3 8h10M3 11.5h6" />
        <circle cx="11.5" cy="11.5" r="2" />
      </svg>
    );
  }

  if (props.screenId === "scoring") {
    return (
      <svg {...common}>
        <path d="M3 12.5 6.2 8.5l2.2 2.2L13 5.5" />
        <path d="M10.5 5.5H13v2.5" />
      </svg>
    );
  }

  return (
    <svg {...common}>
      <path d="M3 4.5h10M3 8h7M3 11.5h5" />
      <path d="M11 9.5h2.5V12H11z" />
    </svg>
  );
}
