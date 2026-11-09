import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError } from "../api/client";
import { admit, getSession, login, logout, register } from "../api/auth";
import type { AuthResponse, User } from "../api/types";
import { CompanyDescriptionPage } from "../features/companies/CompanyDescriptionPage";
import { CompanyParentsPage } from "../features/companies/CompanyParentsPage";
import { DigestPage } from "../features/digest/DigestPage";
import { JobsPage } from "../features/jobs/JobsPage";
import { MaterialsPage } from "../features/materials/MaterialsPage";
import { StatisticsPage } from "../features/statistics/StatisticsPage";
import { MaintenancePage } from "../features/maintenance/MaintenancePage";
import { ScoringPage } from "../features/scoring/ScoringPage";
import { SignalRadarPage } from "../features/signals/SignalRadarPage";
import { SourcesPage } from "../features/sources/SourcesPage";
import { TagsPage } from "../features/tags/TagsPage";
import { UsersPage } from "../features/users/UsersPage";

type ScreenId =
  | "business-feed" | "signal-radar" | "digest" | "documents" | "colleague-materials" | "sources" | "scoring" | "tags" | "users" | "jobs"
  | "maintenance" | "company-parents" | "company-description" | "statistics";

// Экраны только для администратора (настройка источников/скоринга/тегов, пользователи, операции).
const ADMIN_SCREENS = new Set<ScreenId>([
  "sources", "scoring", "tags", "users", "jobs", "maintenance",
  // Список «дочка → мать» правят, как теги, только админы (сервер: require_admin).
  "company-parents",
  // Описание Компании для агента гипотезы (#162): права как у тегов (ADR 0004).
  "company-description",
  // Статистика сводная (видно работу КАЖДОГО пользователя), решение владельца:
  // раздел только для администраторов. Серверный гейт на /api/stats/monthly тоже
  // require_admin: фронтовый гейт без серверного это дыра (аудит изоляции 24.07).
  "statistics",
]);

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
    id: "business-feed",
    // #97 (спека #93): бизнес-сигналы карточками, как радар: одна карточка на событие, публикации снизу.
    label: "Бизнес-лента",
    eyebrow: "Лента",
    title: "Бизнес-лента",
    description: "Бизнес-сигналы карточками событий: публикации, атрибуты карточки, фильтры и «всё про компанию».",
    status: "Экран активен",
  },
  {
    id: "digest",
    label: "Месячный дайджест",
    eyebrow: "Выпуск",
    title: "Сборка выпуска",
    description: "Выборка материалов, предпросмотр, черновик и выгрузка опираются на тот же API, но уже через новый интерфейс.",
    status: "Экран активен",
  },
  {
    id: "signal-radar",
    // «Радар сигналов» → «Технологический радар» по документу заказчика (19.09), как в MVP-1.
    label: "Технологический радар",
    eyebrow: "Радар",
    title: "Технологический радар",
    description: "Сигналы, найденные новым поисковым агентом: ссылки-доказательства, переносимость, развернутая ОС и выбор в дайджест.",
    status: "Экран активен",
  },
  {
    id: "colleague-materials",
    // Один раздел «Материалы» (решение владельца 23.10): материалы из бота Telegram и документы, внесённые на
    // платформе; у каждой записи видно, откуда она. Прежний адрес ?screen=documents ведёт сюда же.
    label: "Материалы",
    eyebrow: "Материалы",
    title: "Материалы",
    description: "Что коллеги прислали боту в Telegram, и документы, которые вы внесли на платформе.",
    status: "Экран активен",
  },
  {
    id: "sources",
    label: "Источники",
    eyebrow: "Источники",
    title: "Каталог источников",
    description: "Упрощённые карточки, фильтры, диагностика и настройки парсинга теперь собраны в отдельном экране.",
    status: "Экран активен",
  },
  {
    id: "scoring",
    label: "Скоринг",
    eyebrow: "Настройки",
    title: "Профиль критериев",
    description: "Весы критериев, редактор описаний и нормализация профиля уже перенесены в новый интерфейс.",
    status: "Экран активен",
  },
  {
    id: "tags",
    label: "Теги",
    eyebrow: "Тематики",
    title: "Дерево тем",
    description: "Направления и подтеги: включение, редактирование и сохранение теперь тоже в новом интерфейсе.",
    status: "Экран активен",
  },
  {
    id: "company-parents",
    // Решение владельца 20.10: фильтр «всё про компанию» показывает и дочерние общества по этому списку.
    label: "Дочерние общества",
    eyebrow: "Компании",
    title: "Дочерние общества",
    description: "Список «дочка → мать» для фильтра «всё про компанию»: правка действует сразу, без выката.",
    status: "Экран активен",
  },
  {
    id: "company-description",
    // #162: по этому тексту агент «Гипотеза для Компании» (#163) понимает, для кого пишет.
    label: "Описание Компании",
    eyebrow: "Компании",
    title: "Описание Компании",
    description: "Публичное описание Компании для агента гипотезы: текст, кто и когда правил.",
    status: "Экран активен",
  },
  {
    id: "users",
    label: "Пользователи",
    eyebrow: "Доступ",
    title: "Пользователи",
    description: "Управление учётными записями и ролями (только для администратора).",
    status: "Экран активен",
  },
  {
    id: "statistics",
    label: "Статистика",
    eyebrow: "Аналитика",
    title: "Статистика платформы",
    description: "Месячные результаты платформы и работа пользователей: воронка от сбора до дайджеста, затраты на ИИ, разметка по каждому и общий итог (только для администратора).",
    status: "Экран активен",
  },
];

const navGroups: NavGroup[] = [
  {
    label: "Работа",
    screens: ["business-feed", "signal-radar", "digest", "colleague-materials"],
  },
  {
    label: "Настройки",
    screens: ["sources", "scoring", "tags", "company-parents", "company-description"],
  },
  {
    label: "Администрирование",
    screens: ["users", "statistics"],
  },
];

// Экраны, адресуемые через ?screen=<id>. jobs/maintenance в меню нет (служебные, только по ссылке).
// ?screen=documents (прежний раздел документов) ведёт в «Материалы».
const URL_ADDRESSABLE: ScreenId[] = ["jobs", "maintenance", "sources", "signal-radar", "business-feed", "digest", "colleague-materials"];

// Первый экран: «Бизнес-лента».
const DEFAULT_SCREEN: ScreenId = "business-feed";

function screenFromUrl(): ScreenId | null {
  const value = new URLSearchParams(window.location.search).get("screen");
  // Прежние «Материалы» (загрузка документов): теперь часть общего раздела «Материалы».
  if (value === "documents") return "colleague-materials";
  return URL_ADDRESSABLE.find((id) => id === value) ?? null;
}

function initialScreenFromUrl(): ScreenId {
  return screenFromUrl() ?? DEFAULT_SCREEN;
}

export function App() {
  const [activeScreen, setActiveScreen] = useState<ScreenId>(initialScreenFromUrl);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  // Мобильное меню (до 1056px): кнопка в шапке раскрывает список разделов поверх экрана.
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  const [user, setUser] = useState<User | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  // Шаг «код допуска» (#113): пароль верный, учётка ждёт допуска: пояснение сервера и поле кода.
  const [admissionNotice, setAdmissionNotice] = useState<string | null>(null);
  const [admissionCode, setAdmissionCode] = useState("");
  const [toast, setToast] = useState<{ text: string; tone: "default" | "error" } | null>(null);
  const active = screens.find((screen) => screen.id === activeScreen) ?? screens[0];
  const isAdmin = (user?.role ?? "user") === "admin";
  // Пункт меню виден, если роль пускает.
  const screenInMenu = (screenId: ScreenId) => isAdmin || !ADMIN_SCREENS.has(screenId);

  useEffect(() => {
    void loadSession();
  }, []);

  // Обычное уведомление уходит само через 2,8 с; ошибка остаётся, пока её не закроют:
  // за 2,8 с текст ошибки не всегда успевают прочитать.
  useEffect(() => {
    if (!toast || toast.tone === "error") return;
    const timer = window.setTimeout(() => setToast(null), 2800);
    return () => window.clearTimeout(timer);
  }, [toast]);

  // Заголовок вкладки браузера называет текущий экран.
  useEffect(() => {
    document.title = user ? `${active.label} · OilTech Digest` : "Вход · OilTech Digest";
  }, [user, active.label]);

  // Escape закрывает мобильное меню и возвращает фокус на кнопку меню.
  useEffect(() => {
    if (!mobileNavOpen) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setMobileNavOpen(false);
        menuButtonRef.current?.focus();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [mobileNavOpen]);

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

    </section>
  );

  if (activeScreen === "sources") {
    currentScreen = <SourcesPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "colleague-materials" || activeScreen === "documents") {
    currentScreen = <MaterialsPage onUnauthorized={resetSession} showToast={showToast} isAdmin={isAdmin} />;
  }

  // Лента и радар: один экран в двух витринах. Ключ, экран: при переходе между ними состояние
  // (фильтры, выборка) не переезжает из одной витрины в другую.
  if (activeScreen === "signal-radar" || activeScreen === "business-feed") {
    currentScreen = (
      <SignalRadarPage
        key={activeScreen}
        onUnauthorized={resetSession}
        showToast={showToast}
        isAdmin={isAdmin}
        showcase={activeScreen === "business-feed" ? "business" : "technology"}
      />
    );
  }

  if (activeScreen === "digest") {
    currentScreen = <DigestPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "scoring") {
    currentScreen = <ScoringPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "tags") {
    currentScreen = <TagsPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "company-parents") {
    currentScreen = <CompanyParentsPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "company-description") {
    currentScreen = <CompanyDescriptionPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "jobs") {
    currentScreen = <JobsPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "maintenance") {
    currentScreen = <MaintenancePage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "users") {
    currentScreen = <UsersPage onUnauthorized={resetSession} showToast={showToast} currentUserId={Number(user?.id ?? 0)} />;
  }

  if (activeScreen === "statistics") {
    currentScreen = <StatisticsPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  // Защита: не-админ не должен видеть админ-экраны даже по прямой ссылке ?screen=.
  if (!isAdmin && ADMIN_SCREENS.has(activeScreen)) {
    currentScreen = (
      <section className="screenStack">
        <header className="screenHeader">
          <div><h1>Нет доступа</h1></div>
        </header>
        <section className="panel">
          <div className="emptyState">Раздел доступен только администратору. Обратитесь к администратору системы.</div>
        </section>
      </section>
    );
  }

  function switchScreen(screenId: ScreenId) {
    setActiveScreen(screenId);
    setMobileNavOpen(false);
    // Переход по меню переносит фокус на экран: клавиатура и чтение с экрана начинают
    // с нового содержимого, а не с конца меню.
    window.requestAnimationFrame(() => mainRef.current?.focus({ preventScroll: true }));
    if (URL_ADDRESSABLE.includes(screenId)) {
      window.history.replaceState(null, "", `?screen=${screenId}`);
      return;
    }
    if (URL_ADDRESSABLE.some((id) => window.location.search.includes(`screen=${id}`))) {
      window.history.replaceState(null, "", window.location.pathname || "/");
    }
  }

  async function loadSession() {
    try {
      setAuthLoading(true);
      const payload = await getSession();
      setUser(payload.user);
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) {
        showToast(error instanceof Error ? error.message : "Не удалось загрузить сессию", "error");
      }
      setUser(null);
    } finally {
      setAuthLoading(false);
    }
  }

  async function submitAuth(event?: FormEvent) {
    event?.preventDefault();
    try {
      const payload = authMode === "register" ? await register(email.trim(), password) : await login(email.trim(), password);
      if ("admission_required" in payload) {
        setAdmissionCode("");
        setAdmissionNotice(payload.detail);
        return;
      }
      await enter(payload, authMode === "register" ? "Регистрация завершена" : "Вход выполнен");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "Не удалось выполнить вход", "error");
    }
  }

  async function submitAdmission(event?: FormEvent) {
    event?.preventDefault();
    try {
      await enter(await admit(email.trim(), password, admissionCode), "Допуск получен, вход выполнен");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "Не удалось проверить код", "error");
    }
  }

  function leaveAdmission() {
    setAdmissionNotice(null);
    setAdmissionCode("");
    setPassword("");
  }

  async function enter(payload: AuthResponse, message: string) {
    setUser(payload.user);
    setAdmissionNotice(null);
    setAdmissionCode("");
    setPassword("");
    showToast(message);
  }

  // Сброс сессии при 401: данные экранов живут в самих экранах и уходят вместе с ними.
  function resetSession() {
    setUser(null);
  }

  async function handleLogout() {
    try {
      await logout();
      setUser(null);
      showToast("Сессия завершена");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "Не удалось выйти", "error");
    }
  }

  function showToast(text: string, tone: "default" | "error" = "default") {
    setToast({ text, tone });
  }

  const toastRegion = <ToastRegion toast={toast} onClose={() => setToast(null)} />;

  if (authLoading) {
    return (
      <div className="splashScreen" role="status">
        <span className="spinnerReact" aria-hidden="true" />
        Проверяем сессию…
      </div>
    );
  }

  if (!user && admissionNotice !== null) {
    return (
      <div className="authShellReact">
        <main className="authCardReact">
          <div className="eyebrow">OilTech Digest</div>
          <h1>Код допуска</h1>
          <p>{admissionNotice}</p>
          <p>Учётка ещё не допущена к платформе. Введите код, который передали подтверждающие: он действует сутки, попыток ввода — пять.</p>
          <form noValidate onSubmit={(event) => void submitAdmission(event)}>
            <label className="field">
              <span>Код допуска</span>
              <input
                name="admission-code"
                value={admissionCode}
                onChange={(event) => setAdmissionCode(event.target.value)}
                placeholder="8 цифр"
                inputMode="numeric"
                autoComplete="one-time-code"
                spellCheck={false}
              />
            </label>
            <div className="authActions">
              <button type="submit" className="primaryButton">
                Подтвердить
              </button>
              <button type="button" className="ghostButton" onClick={leaveAdmission}>
                Назад ко входу
              </button>
            </div>
          </form>
        </main>
        {toastRegion}
      </div>
    );
  }

  if (!user) {
    return (
      <div className="authShellReact">
        <main className="authCardReact">
          <div className="eyebrow">OilTech Digest</div>
          <h1>{authMode === "register" ? "Регистрация" : "Вход в админ-панель"}</h1>
          <p>
            {authMode === "register"
              ? "Создайте аккаунт, чтобы работать с редакторскими инструментами."
              : "Войдите в аккаунт, чтобы продолжить работу с редакторской панелью."}
          </p>
          {/* Форма: Enter в поле пароля отправляет вход (раньше срабатывала только кнопка). */}
          <form noValidate onSubmit={(event) => void submitAuth(event)}>
            <label className="field">
              <span>Эл. почта</span>
              <input
                type="email"
                name="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="you@example.com"
                autoComplete="username"
                inputMode="email"
                spellCheck={false}
              />
            </label>
            <label className="field">
              <span>Пароль</span>
              <input
                type="password"
                name="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="Не короче 8 символов"
                autoComplete={authMode === "register" ? "new-password" : "current-password"}
              />
            </label>
            <div className="authActions">
              <button type="submit" className="primaryButton">
                {authMode === "register" ? "Зарегистрироваться" : "Войти"}
              </button>
            </div>
          </form>
          {/* Переключателя «Зарегистрироваться» больше нет: самостоятельная
              регистрация закрыта (#33), и кнопка вела в 403. Учётки заводит
              администратор: экран «Пользователи» или CLI create-user. */}
          <div className="authSwitchText">
            Нет доступа? Обратитесь к администратору платформы.
          </div>
        </main>
        {toastRegion}
      </div>
    );
  }

  const shellClass = ["shell", sidebarCollapsed ? "sidebarCollapsed" : "", mobileNavOpen ? "mobileNavOpen" : ""].filter(Boolean).join(" ");

  return (
    <div className={shellClass}>
      <a className="skipLink" href="#main-content">Перейти к содержимому</a>
      <header className="shellHeader">
        <button
          ref={menuButtonRef}
          type="button"
          className="shellMenuButton"
          aria-label={mobileNavOpen ? "Закрыть меню разделов" : "Открыть меню разделов"}
          aria-expanded={mobileNavOpen}
          aria-controls="mobile-nav"
          onClick={() => setMobileNavOpen((value) => !value)}
        >
          <MenuIcon open={mobileNavOpen} />
        </button>
        <div className="brand">
          <span className="brandTitle">OilTech Digest</span>
          <span className="brandSubtitle">Админ-панель</span>
        </div>
        <div className="shellHeaderActions">
          <span className="shellUser" title={user.email}>{user.email}</span>
          <button type="button" className="shellHeaderButton" onClick={() => void handleLogout()}>
            <UtilityIcon kind="logout" collapsed={false} />
            Выйти
          </button>
        </div>
      </header>

      <aside className="sidebar" aria-label="Разделы">
        <div className="sidebarGroups">
          {navGroups.map((group) => {
            const visibleScreens = group.screens.filter(screenInMenu);
            if (!visibleScreens.length) return null;
            return (
            <section className="sidebarGroup" key={group.label}>
              <div className="sidebarSection">{group.label}</div>
              <nav className="nav">
                {visibleScreens.map((screenId) => {
                  const screen = screens.find((item) => item.id === screenId);
                  if (!screen) return null;
                  return (
                    <button
                      key={screen.id}
                      type="button"
                      className={screen.id === activeScreen ? "navButton active" : "navButton"}
                      onClick={() => switchScreen(screen.id)}
                      title={sidebarCollapsed ? screen.label : undefined}
                      aria-current={screen.id === activeScreen ? "page" : undefined}
                    >
                      <span className="navButtonIcon" aria-hidden="true">
                        <ScreenIcon screenId={screen.id} />
                      </span>
                      <span className="navButtonLabel">{screen.label}</span>
                    </button>
                  );
                })}
              </nav>
            </section>
            );
          })}
        </div>

        <div className="sidebarBottom">
          <div className="sidebarUtilityActions">
            <button
              type="button"
              className="navButton utilityButton"
              onClick={() => setSidebarCollapsed((value) => !value)}
              aria-label={sidebarCollapsed ? "Развернуть сайдбар" : "Свернуть сайдбар"}
              title={sidebarCollapsed ? "Развернуть сайдбар" : undefined}
            >
              <span className="navButtonIcon" aria-hidden="true">
                <UtilityIcon kind="toggle" collapsed={sidebarCollapsed} />
              </span>
              <span className="navButtonLabel">{sidebarCollapsed ? "Развернуть меню" : "Свернуть меню"}</span>
            </button>
          </div>
        </div>
      </aside>

      <main id="main-content" ref={mainRef} className="content" tabIndex={-1}>{currentScreen}</main>
      {/* Мобильное меню: те же группы, что в боковой навигации. */}
      <nav id="mobile-nav" className="mobileNav" aria-label="Разделы">
        {navGroups.map((group) => {
          const visibleScreens = group.screens.filter(screenInMenu);
          if (!visibleScreens.length) return null;
          return (
            <section className="mobileNavGroup" key={group.label}>
              <div className="sidebarSection">{group.label}</div>
              {visibleScreens.map((screenId) => {
                const screen = screens.find((item) => item.id === screenId);
                if (!screen) return null;
                return (
                  <button
                    key={screen.id}
                    type="button"
                    className={screen.id === activeScreen ? "mobileNavButton active" : "mobileNavButton"}
                    onClick={() => switchScreen(screen.id)}
                    aria-current={screen.id === activeScreen ? "page" : undefined}
                  >
                    <span className="mobileNavIcon" aria-hidden="true">
                      <ScreenIcon screenId={screen.id} />
                    </span>
                    <span className="mobileNavLabel">{screen.label}</span>
                  </button>
                );
              })}
            </section>
          );
        })}
      </nav>
      <div className="mobileNavScrim" aria-hidden="true" onClick={() => setMobileNavOpen(false)} />
      {toastRegion}
    </div>
  );
}

// Область уведомлений всегда в DOM (role="status"): так чтение с экрана объявляет каждое
// новое сообщение. Ошибка закрывается кнопкой, обычное сообщение уходит само.
function ToastRegion(props: { toast: { text: string; tone: "default" | "error" } | null; onClose: () => void }) {
  return (
    <div className="toastRegion" role="status" aria-live="polite">
      {props.toast ? (
        <div className={`toastReact ${props.toast.tone === "error" ? "error" : ""}`}>
          <span className="toastText">{props.toast.text}</span>
          <button type="button" className="toastClose" aria-label="Закрыть уведомление" onClick={props.onClose}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.6} aria-hidden="true">
              <path d="M4 4l8 8M12 4l-8 8" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      ) : null}
    </div>
  );
}

function MenuIcon(props: { open: boolean }) {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.6} aria-hidden="true">
      {props.open ? (
        <path d="M5 5l10 10M15 5L5 15" strokeLinecap="round" />
      ) : (
        <path d="M3 5.5h14M3 10h14M3 14.5h14" strokeLinecap="round" />
      )}
    </svg>
  );
}

function UtilityIcon(props: { kind: "toggle" | "logout"; collapsed: boolean }) {
  const common = { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.7, "aria-hidden": true };

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
  const common = { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.6, "aria-hidden": true };

  if (props.screenId === "business-feed") {
    // Карточки событий стопкой: одна карточка: несколько публикаций.
    return (
      <svg {...common}>
        <rect x="2.5" y="4.5" width="9" height="9" rx="1.8" />
        <path d="M5 2.5h6.7a1.8 1.8 0 0 1 1.8 1.8V11M5 8h4M5 10.5h3" />
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

  if (props.screenId === "documents" || props.screenId === "colleague-materials") {
    return (
      <svg {...common}>
        <path d="M4 2.5h5l3 3v8H4z" strokeLinejoin="round" />
        <path d="M9 2.5v3h3M6 8h4M6 10.5h4" />
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

  if (props.screenId === "company-description") {
    // Лист с текстом.
    return (
      <svg {...common}>
        <rect x="3.5" y="2.5" width="9" height="11" rx="1" />
        <path d="M5.5 5.5h5M5.5 8h5M5.5 10.5h3" />
      </svg>
    );
  }

  if (props.screenId === "company-parents") {
    // Мать сверху, две дочки снизу.
    return (
      <svg {...common}>
        <rect x="5.5" y="2.5" width="5" height="3.5" rx="1" />
        <rect x="2.5" y="10" width="4.5" height="3.5" rx="1" />
        <rect x="9" y="10" width="4.5" height="3.5" rx="1" />
        <path d="M8 6v2H4.8V10M8 8h3.2V10" />
      </svg>
    );
  }

  if (props.screenId === "users") {
    return (
      <svg {...common}>
        <circle cx="6" cy="6" r="2.3" />
        <path d="M2.5 13c0-2.2 1.6-3.4 3.5-3.4s3.5 1.2 3.5 3.4" />
        <path d="M10.5 5.4c1.3 0 2.2 1 2.2 2.2s-.9 2.1-2.2 2.1" />
        <path d="M11.4 9.9c1.4.2 2.3 1.3 2.3 3.1" />
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
