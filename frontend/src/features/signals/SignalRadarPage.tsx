import { useEffect, useId, useMemo, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import {
  createSignalFeedback,
  getSignalDictionaries,
  getSignalSearchHealth,
  getSignalSummary,
  getSignalWeekly,
  listSignals,
  updateSignal,
} from "../../api/signals";
import type {
  SignalDictionaries,
  SignalFilters,
  SignalSearchHealth,
  SignalShowcase,
  SignalSort,
  SignalSummary,
  SignalWeekly,
} from "../../api/signals";
import type { Signal, SignalEvidence, SignalFeedbackBlock, SignalFeedbackPayload } from "../../api/types";
import { radarDigestLockedText } from "../articles/feedWindow";
import { StatCard } from "../shared/StatCard";
import { ratingClass, scoreClass } from "../shared/scoreScale";
import { PROFILES } from "../scoring/ScoringPage";
import { CHOICE_FIELDS, SignalCardChips, eventDateLabel, whyBlock } from "./SignalCardChips";
import { CompanyGroupHint, EMPTY_CARD_FILTERS, SignalCardFilters } from "./SignalCardFilters";
import type { CardFilterValues } from "./SignalCardFilters";

// Страница выдачи: остальное — «Показать ещё». Выборку и страницу считает сервер по всему
// радару, а не экран по загруженным карточкам (замечание заказчика 19.09).
export const RADAR_PAGE_SIZE = 100;
// Больше за один запрос сервер не отдаёт (/api/signals, limit ≤ 200).
const RADAR_MAX_PAGE = 200;
// Задержка серверного поиска — как у ленты бизнес-сигналов: запрос не на каждую букву.
const SEARCH_DELAY_MS = 400;
const DEFAULT_SORT: SignalSort = "score_desc";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
  isAdmin?: boolean;
  // Витрина (#97): «technology» — технологический радар, «business» — бизнес-лента, тот же экран:
  // карточка события, публикации снизу, атрибуты чипами. «В дайджест» и блоки ОС — в обеих витринах,
  // одной механикой (решение владельца 23.10, #130); служебное радара — только у радара.
  showcase?: SignalShowcase;
};

// Подписи экрана по витрине. Публикаций у события ленты (статья и перепечатки) — сколько сервер отдаёт
// за раз (evidence_limit ≤ 20), у карточки радара — 5, как раньше.
const SCREENS: Record<SignalShowcase, {
  heading: string;
  catalog: string;
  searchIn: string;
  loadError: string;
  summary: string;
  evidence: string;
  evidenceLimit: number;
}> = {
  technology: {
    heading: "Технологический радар",
    catalog: "Каталог технологических сигналов",
    searchIn: "радару",
    loadError: "Не удалось загрузить технологический радар",
    summary: "Сводка радара",
    evidence: "Ссылки",
    evidenceLimit: 5,
  },
  business: {
    heading: "Бизнес-лента",
    catalog: "Каталог бизнес-сигналов",
    searchIn: "ленте",
    loadError: "Не удалось загрузить бизнес-ленту",
    summary: "Сводка бизнес-ленты",
    evidence: "Публикации",
    evidenceLimit: 20,
  },
};

const MATURITY_LABELS: Record<string, string> = {
  watch: "Наблюдать",
  shortlist: "Кандидат",
  proven: "Подтверждено",
  reject: "Отклонено",
};

type FeedbackDraft = {
  verdict: NonNullable<SignalFeedbackPayload["verdict"]> | "";
  reason: string;
  correctedTitle: string;
  correctedThesis: string;
  duplicateOfSignalId: string;
  comment: string;
};

const EMPTY_FEEDBACK_DRAFT: FeedbackDraft = {
  verdict: "",
  reason: "",
  correctedTitle: "",
  correctedThesis: "",
  duplicateOfSignalId: "",
  comment: "",
};

// Шкала заказчика (список Виктора от 13.09). Это оценка ЧЕЛОВЕКА и она намеренно
// отличается от maturity выше: та — оценка модели. «Наблюдать» встречается в обеих,
// поэтому машинная подписана в карточке как «Решение платформы», а эта — как «Оценка сигнала».
const VERDICT_LABELS: Array<{ value: FeedbackDraft["verdict"]; label: string }> = [
  { value: "", label: "Не оценено" },
  { value: "strong_signal", label: "Сильный сигнал — вынести в дайджест / обсуждать" },
  { value: "approved", label: "Полезный сигнал — релевантно, сохранить в базе" },
  { value: "watch_later", label: "Наблюдать — рано, нужен следующий этап" },
  { value: "background_material", label: "Фоновый материал — ориентир или контекст" },
  { value: "reject", label: "Низкая ценность / шум — по теме, но без новой ценности" },
  { value: "wrong_domain", label: "Не релевантно — вне интересов Компании" },
  // Встреча с заказчиком 21.09, решение 5: находка годная, но это бизнес-сигнал.
  { value: "wrong_block", label: "Не тот блок — бизнес-сигнал, а не технология" },
  { value: "merge_duplicate", label: "Дубль — тот же сигнал или технологический кластер" },
  // 03.10: находка годная, но балл выше, чем она стоит — судья учится ставить строже.
  { value: "overrated", label: "Оценка завышена — сигнал годный, балл слишком высокий" },
];

// Отзыв учит судью радара только о его находке вне бизнес-ленты (#130, repository.judge_learns_from): у неё
// полная форма. У остальных (сигнал ленты, находка в бизнес-ленте) отзыв — блок ОС: оценка, причина и
// комментарий, правок карточки и «дубля» (он прячет карточку) нет; блок только хранится.
function judgeFeedback(signal: Signal): boolean {
  return signal.origin !== "feed" && signal.signal_category !== "business";
}

// «Не тот блок» у бизнес-карточки — наоборот: технология, а не бизнес.
function verdictOptions(signal: Signal): typeof VERDICT_LABELS {
  const labels = signal.signal_category === "business"
    ? VERDICT_LABELS.map((item) =>
      item.value === "wrong_block" ? { ...item, label: "Не тот блок — технологический сигнал, а не бизнес" } : item)
    : VERDICT_LABELS;
  return judgeFeedback(signal) ? labels : labels.filter((item) => item.value !== "merge_duplicate");
}

// Оценка человека в списке блоков — коротко: первая часть подписи шкалы («Сильный сигнал»).
function verdictName(verdict: string | null): string {
  const found = VERDICT_LABELS.find((item) => item.value === verdict);
  return found ? found.label.split(" — ")[0] : verdict || "";
}

function blockDate(value: string | null): string {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleDateString("ru-RU", { timeZone: RADAR_TIME_ZONE }) : "";
}

const EARLY_THEME_GROUP = "Ранние карточки — тема вне 13 тематик";

// Сутки радара и крон 07:15 — по Москве, поэтому и время прогона показываем по Москве.
const RADAR_TIME_ZONE = "Europe/Moscow";

function formatRunMoment(value: string | null): string {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "";
  const day = date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", timeZone: RADAR_TIME_ZONE });
  const time = date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", timeZone: RADAR_TIME_ZONE });
  return `${day} в ${time}`;
}

function formatDay(value: string | null | undefined): string {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: RADAR_TIME_ZONE });
}

// Профиль балла (ADR 0002): у находки радара — «Технологический радар», у сигнала ленты — профиль его
// статьи, «Бизнес-сигналы». Имена — как на экране «Скоринг».
function profileLabel(signal: Signal): string | undefined {
  return PROFILES.find((item) => item.id === signal.score_profile)?.label;
}

// Разбивка балла по критериям профиля: имя и вес — из снимка набора на момент оценки, а не из
// нынешнего экрана «Скоринг» (там их могли поменять).
function scoreBreakdown(signal: Signal): Array<{ id: number; name: string; weight: number; score: number }> {
  if (!profileLabel(signal)) return [];
  const byId = new Map((signal.criteria_snapshot || []).map((item) => [Number(item.id), item]));
  return (signal.score_items_json || []).map((item) => {
    const criterion = byId.get(Number(item.criterion_id));
    return {
      id: Number(item.criterion_id),
      name: criterion?.name || `Критерий ${item.criterion_id}`,
      weight: Number(criterion?.weight || 0),
      score: Math.round(Number(item.final_score || 0)),
    };
  });
}

function scoreTitle(signal: Signal, withBreakdown: boolean): string {
  if (withBreakdown) return `Балл по профилю «${profileLabel(signal)}»`;
  return signal.origin === "feed" ? "Балл статьи ленты" : "Балл судьи радара";
}

// Дата поступления карточки (встреча 21.09): первая находка радаром, по Москве.
function formatArrival(signal: Signal): string {
  return formatDay(signal.first_seen_at || signal.created_at);
}

// Строка над заголовком ссылки: «издатель · дата публикации» (колонки «Источник» и «Дата
// публикации» эталона заказчика). Даты нет — нет и пустого «·».
function evidenceSource(item: SignalEvidence): string {
  return [item.publisher || "источник", formatDay(item.published_at)].filter(Boolean).join(" · ");
}

// «из 1 сигнала», «из 21 сигнала», но «из 2 сигналов», «из 11 сигналов».
function signalsAfterFrom(count: number): string {
  return count % 10 === 1 && count % 100 !== 11 ? "сигнала" : "сигналов";
}

// Слово при числе: «1 ссылка», «2 ссылки», «5 ссылок».
function pluralWord(count: number, one: string, few: string, many: string): string {
  const tens = count % 100;
  const units = count % 10;
  if (units === 1 && tens !== 11) return one;
  if (units >= 2 && units <= 4 && (tens < 12 || tens > 14)) return few;
  return many;
}

const LINK_WORDS = ["ссылка", "ссылки", "ссылок"] as const;
const PUBLICATION_WORDS = ["публикация", "публикации", "публикаций"] as const;

// Мета под заголовком карточки: «тема · N ссылок · решение платформы · » — номер карточки следом, в обеих
// витринах одинаково (решение владельца 23.10). Решение платформы (наблюдать / кандидат / подтверждено /
// отклонено; до 22.10 — «Решение радара») — оценка машины, а не зрелость технологии (та — чипом карточки);
// оценка человека в ОС — «Оценка сигнала». У события ленты ссылки — публикации.
function cardMeta(signal: Signal, business: boolean): string {
  const links = Number(signal.evidence_count || 0);
  const [one, few, many] = business ? PUBLICATION_WORDS : LINK_WORDS;
  const maturity = MATURITY_LABELS[signal.maturity] || signal.maturity;
  return `${signal.theme} · ${links} ${pluralWord(links, one, few, many)} · Решение платформы: ${maturity} · `;
}

// Цвет балла — по словесной оценке, как в ленте: число и слово одного цвета.
function scoreTone(signal: Signal): string {
  return signal.score_label ? ratingClass(signal.score_label) : scoreClass(Number(signal.score || 0));
}

// Причина по-русски; прочее — короткий текст сервера. Код HTTP — впереди, как у 402:
// внутри скобок плашки вторых скобок нет.
function describeSearchError(health: SignalSearchHealth): string {
  const code = health.http_status;
  if (health.cause === "not_configured") {
    // provider «none» — поиск не подключён вовсе (ключ может быть на месте); иначе нет ключа.
    return health.provider === "none" ? "провайдер поиска не подключён" : "поиск не настроен — нет ключа";
  }
  if (code === 402 && health.provider === "brave") return "HTTP 402 — исчерпан месячный лимит поиска Brave";
  if (code === 429) return "HTTP 429 — превышен лимит запросов к поиску";
  if (code != null && code >= 500 && code <= 599) return `HTTP ${code} — сервис поиска недоступен`;
  if (health.cause === "network") return "поиск не ответил вовремя";
  if (health.cause === "connection") return "нет соединения с поиском";
  return (health.first_error || "").trim();
}

// Поиск не вызывался (нет ключа, провайдер не подключён или неизвестен) — он «не выполнен»,
// а не «не ответил».
const SEARCH_NOT_RUN = new Set(["not_configured", "unsupported_provider"]);

// Плашка только для админа: 23–27.09 поиск не отвечал ни в одной теме, задача была «ok»,
// и пять дней этого никто не видел. После прогона без сбоев плашки нет.
function searchHealthNotice(health: SignalSearchHealth | null): string {
  if (!health || !health.topics || !health.failed) return "";
  const when = formatRunMoment(health.run_at);
  const verb = SEARCH_NOT_RUN.has(health.cause ?? "") ? "не выполнен" : "не ответил";
  const topicsWord = health.topics % 10 === 1 && health.topics % 100 !== 11 ? "темы" : "тем";
  const cause = describeSearchError(health);
  const noSignals = health.signals === 0 ? " Новых сигналов нет." : "";
  return (
    `Прогон${when ? ` ${when}` : ""}: поиск ${verb} в ${health.failed} из ${health.topics} ${topicsWord}` +
    `${cause ? ` (${cause})` : ""}.${noSignals}`
  );
}

export function SignalRadarPage({ onUnauthorized, showToast, isAdmin = false, showcase = "technology" }: Props) {
  const business = showcase === "business";
  const screenText = SCREENS[showcase];
  const [signals, setSignals] = useState<Signal[]>([]);
  const [summary, setSummary] = useState<SignalSummary | null>(null);
  // Первый ответ ещё не пришёл — вместо списка «Загружаем»; дальше старая выборка видна,
  // пока идёт новая.
  const [loaded, setLoaded] = useState(false);
  const [searching, setSearching] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [maturity, setMaturity] = useState("");
  const [theme, setTheme] = useState("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SignalSort>(DEFAULT_SORT);
  // Вариант A (решение владельца 23.10): на виду поиск, тема и сортировка, остальные фильтры — в панели.
  const [filtersOpen, setFiltersOpen] = useState(false);
  const filtersPanelId = useId();
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [feedbackOpen, setFeedbackOpen] = useState<Set<number>>(new Set());
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  // Свёрнутые вручную — сильнее авто-раскрытия при поиске и выбранной теме.
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [feedbackDrafts, setFeedbackDrafts] = useState<Record<number, FeedbackDraft>>({});
  const [saving, setSaving] = useState<Record<number, boolean>>({});
  const [searchHealth, setSearchHealth] = useState<SignalSearchHealth | null>(null);
  const [weekly, setWeekly] = useState<SignalWeekly | null>(null);
  const [weeklyOpen, setWeeklyOpen] = useState(false);
  // «Отклонённые» — только админу (решение владельца 05.10; заказчик Виктор — админ): все разделы «кто убрал →
  // причина» (#130) одним списком в обеих витринах, причина — в самой карточке (решение владельца 23.10).
  const [showHidden, setShowHidden] = useState(false);
  // Фильтры атрибутов карточки (#97) и справочники для их списков.
  const [cardFilters, setCardFilters] = useState<CardFilterValues>(EMPTY_CARD_FILTERS);
  const [dictionaries, setDictionaries] = useState<SignalDictionaries | null>(null);

  const filters: SignalFilters = useMemo(
    () => ({
      showcase: business ? "business" : undefined,
      q: search.trim() || undefined,
      theme: theme || undefined,
      maturity: maturity || undefined,
      section: isAdmin && showHidden ? "all" : undefined,
      eventType: cardFilters.eventType || undefined,
      region: cardFilters.region || undefined,
      technologyClass: cardFilters.technologyClass || undefined,
      technologyMaturity: cardFilters.technologyMaturity || undefined,
      company: cardFilters.company.trim() || undefined,
    }),
    [business, search, theme, maturity, isAdmin, showHidden, cardFilters],
  );
  // Номер последнего запроса выборки: ответ на устаревший запрос не применяется — иначе при
  // быстрой смене фильтров поздний ответ старой выборки встал бы поверх новой.
  const requestSeq = useRef(0);
  const pendingTimer = useRef<number | undefined>(undefined);
  const firstRequest = useRef(true);
  // Новая выборка поставлена или в пути: её ответ заменит список целиком. Ref, а не только
  // searching: читается после await, где состояние из замыкания уже устарело.
  const selectionPending = useRef(false);
  // Текущая выборка — для продолжений после await (отзыв, «В дайджест»): замыкание того
  // рендера, где нажали кнопку, помнит фильтры на момент нажатия, а не нынешние.
  const latestQuery = useRef({ filters, sort, loaded: 0 });
  useEffect(() => {
    latestQuery.current = { filters, sort, loaded: signals.length };
  });

  function load(query: SignalFilters, order: SignalSort, delay: number, size = RADAR_PAGE_SIZE) {
    window.clearTimeout(pendingTimer.current);
    const seq = ++requestSeq.current;
    selectionPending.current = true;
    setSearching(true);
    pendingTimer.current = window.setTimeout(() => {
      Promise.all([
        listSignals({ ...query, sort: order, limit: size, offset: 0, evidenceLimit: screenText.evidenceLimit }),
        getSignalSummary(query),
      ])
        .then(([rows, counts]) => {
          if (seq !== requestSeq.current) return;
          setSignals(rows);
          setSummary(counts);
        })
        .catch((error) => {
          if (seq !== requestSeq.current) return;
          handleError(error, screenText.loadError);
        })
        .finally(() => {
          if (seq !== requestSeq.current) return;
          selectionPending.current = false;
          setSearching(false);
          setLoaded(true);
        });
    }, delay);
  }

  // Фильтры применяются сразу, без «Обновить»: любая смена — новая выборка с сервера.
  useEffect(() => {
    const delay = firstRequest.current ? 0 : SEARCH_DELAY_MS;
    firstRequest.current = false;
    load(filters, sort, delay);
  }, [filters, sort]);

  useEffect(() => () => window.clearTimeout(pendingTimer.current), []);

  useEffect(() => {
    // Справочники не пришли — нет списков атрибутов; поиск по компании работает и без них.
    getSignalDictionaries()
      .then(setDictionaries)
      .catch(() => setDictionaries(null));
  }, []);

  useEffect(() => {
    // Итоги недели — служебный блок радара: не загрузились — его просто нет.
    if (business) return;
    getSignalWeekly()
      .then(setWeekly)
      .catch(() => setWeekly(null));
  }, [business]);

  useEffect(() => {
    // Обычный пользователь здоровье поиска не запрашивает: эндпоинт только для админа. Поиск — радара.
    if (!isAdmin || business) return;
    getSignalSearchHealth()
      .then((response) => setSearchHealth(response.search_health))
      // Служебная плашка не должна мешать экрану: не загрузилась — её просто нет.
      .catch(() => setSearchHealth(null));
  }, [isAdmin, business]);

  function handleError(error: unknown, fallback: string) {
    const statusCode = error instanceof ApiError ? error.status : 0;
    const message = error instanceof Error ? error.message : fallback;
    if (statusCode === 401) {
      onUnauthorized();
      return;
    }
    showToast(message || fallback, "error");
  }

  // keepLoaded — перечитать столько, сколько уже догружено «Показать ещё» (не больше, чем
  // сервер отдаёт за раз): после склейки дубля человек не теряет место в списке.
  function reload(options: { keepLoaded?: boolean } = {}) {
    const { filters: query, sort: order, loaded } = latestQuery.current;
    const size = options.keepLoaded ? Math.min(RADAR_MAX_PAGE, Math.max(RADAR_PAGE_SIZE, loaded)) : RADAR_PAGE_SIZE;
    load(query, order, 0, size);
  }

  // Плитки после «В дайджест» и отзыва: числа — с сервера, по всему радару. Ответ для
  // устаревшей выборки не применяется; сбой тихий — плитки просто остаются прежними.
  function refreshSummary() {
    const seq = requestSeq.current;
    getSignalSummary(latestQuery.current.filters)
      .then((counts) => {
        if (seq === requestSeq.current) setSummary(counts);
      })
      .catch(() => undefined);
  }

  // «В дайджест» или отзыв легли, пока в пути новая выборка: её ответ собран до отметки и
  // откатил бы её на экране (ревью PR #83) — перечитываем выборку заново, на той же глубине.
  // Иначе хватает плиток.
  function afterListChange() {
    if (selectionPending.current) reload({ keepLoaded: true });
    else refreshSummary();
  }

  // Снять фильтры выборки. Порядок списка — не фильтр: его возвращает только «Сбросить».
  function clearFilters() {
    setSearch("");
    setTheme("");
    setMaturity("");
    setCardFilters(EMPTY_CARD_FILTERS);
  }

  function resetFilters() {
    clearFilters();
    setSort(DEFAULT_SORT);
  }

  // Клик по компании на карточке — «всё про компанию» (#97): все сигналы витрины с её участием, а не
  // пересечение с прежними фильтрами; порядок списка остаётся.
  function showCompany(name: string) {
    clearFilters();
    setCardFilters({ ...EMPTY_CARD_FILTERS, company: name });
  }

  async function showMore() {
    // Пока идёт новая выборка, догружать нечего: страница легла бы к старому списку по
    // смещению нового (ревью PR #83). Кнопка в это время и так недоступна.
    if (searching || selectionPending.current) return;
    const seq = requestSeq.current;
    try {
      setLoadingMore(true);
      const rows = await listSignals({
        ...filters,
        sort,
        limit: RADAR_PAGE_SIZE,
        offset: signals.length,
        evidenceLimit: screenText.evidenceLimit,
      });
      if (seq !== requestSeq.current) return;
      // Между страницами радар мог добавить карточку — повтор не рисуем дважды.
      setSignals((current) => {
        const known = new Set(current.map((item) => item.id));
        return [...current, ...rows.filter((item) => !known.has(item.id))];
      });
    } catch (error) {
      handleError(error, "Не удалось загрузить следующие сигналы");
    } finally {
      setLoadingMore(false);
    }
  }

  // В фильтре — только тематики заказчика, и со всего радара, а не с текущей выборки: иначе
  // после выбора темы в списке осталась бы она одна. Ранние карточки — отдельным блоком.
  const themes = useMemo(() => {
    const names = (summary?.themes ?? []).map((item) => item.theme);
    return theme && !names.includes(theme) ? [...names, theme] : names;
  }, [summary, theme]);
  const visibleSignals = signals;
  const remaining = Math.max(0, (summary?.matching ?? signals.length) - signals.length);
  const narrowedByCard = Object.values(cardFilters).some((value) => value.trim());

  const groups = useMemo(() => {
    const byTheme = new Map<string, Signal[]>();
    for (const signal of visibleSignals) {
      // Ранние карточки радара (первая партия 13.09) — с темой свободным текстом: один блок, а не
      // десятки блоков по одной карточке. Тематику им даст разбор, а не догадка по словам. У ленты
      // свободных тем нет: статья без тематики — в блоке своей темы («не классифицировано»).
      const key = !business && signal.theme_is_topic === false ? EARLY_THEME_GROUP : signal.theme || "Без темы";
      byTheme.set(key, [...(byTheme.get(key) || []), signal]);
    }
    // Блоки — в порядке выдачи, как у ленты: первым тот, чья карточка первая по выбранной
    // сортировке (по баллу — блок с лучшей карточкой, «сначала новые» — со свежей).
    // Ранние карточки — всегда последним блоком.
    return [...byTheme.entries()].sort(
      (a, b) => Number(a[0] === EARLY_THEME_GROUP) - Number(b[0] === EARLY_THEME_GROUP),
    );
  }, [visibleSignals, business]);

  function toggleGroup(group: string, open: boolean) {
    setExpandedGroups((current) => {
      const next = new Set(current);
      if (open) next.delete(group);
      else next.add(group);
      return next;
    });
    setCollapsedGroups((current) => {
      const next = new Set(current);
      if (open) next.add(group);
      else next.delete(group);
      return next;
    });
  }

  async function setDigest(signal: Signal, selected: boolean) {
    try {
      setSaving((current) => ({ ...current, [signal.id]: true }));
      await updateSignal(signal.id, { selected_for_digest: selected });
      setSignals((current) =>
        current.map((item) =>
          item.id === signal.id ? { ...item, selected_for_digest: selected, user_status: selected ? "digest" : "watch" } : item,
        ),
      );
      showToast(selected ? "Сигнал добавлен в дайджест" : "Сигнал убран из дайджеста");
      afterListChange();
    } catch (error) {
      handleError(error, "Не удалось обновить статус сигнала");
      // 409 — месяц карточки закрылся, пока экран был открыт (день смены окна): выборка заново,
      // чтобы кнопка погасла по признаку сервера, а не ждала «Обновить».
      if (error instanceof ApiError && error.status === 409) reload({ keepLoaded: true });
    } finally {
      setSaving((current) => ({ ...current, [signal.id]: false }));
    }
  }

  async function submitFeedback(signal: Signal) {
    const draft = feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT;
    const comment = draft.comment.trim();
    const reason = draft.reason.trim();
    const correctedTitle = draft.correctedTitle.trim();
    const correctedThesis = draft.correctedThesis.trim();
    const duplicateOfSignalId = Number(draft.duplicateOfSignalId || 0) || null;
    if (!comment && !draft.verdict && !reason && !correctedTitle && !correctedThesis && !duplicateOfSignalId) {
      showToast("Заполни вердикт, причину или комментарий по сигналу", "error");
      return;
    }
    const primaryEvidence = signal.evidence?.[0];
    try {
      setSaving((current) => ({ ...current, [signal.id]: true }));
      const result = await createSignalFeedback({
        signal_id: signal.id,
        signal_evidence_id: primaryEvidence?.id,
        source_url: primaryEvidence?.source_url,
        signal_title: signal.title_ru || signal.title,
        source: primaryEvidence?.publisher,
        comment,
        verdict: draft.verdict || null,
        reason: reason || null,
        corrected_title: correctedTitle || null,
        corrected_thesis: correctedThesis || null,
        duplicate_of_signal_id: duplicateOfSignalId,
      });
      setFeedbackDrafts((current) => ({ ...current, [signal.id]: EMPTY_FEEDBACK_DRAFT }));
      // Сохранено — форма закрывается: счётчик «Обратная связь: N» рядом подтверждает отзыв, а сохранённый
      // блок ОС ложится в список на карточке (#130).
      closeFeedback(signal.id);
      setSignals((current) =>
        current.map((item) =>
          item.id === signal.id
            ? {
                ...item,
                feedback_count: (item.feedback_count || 0) + 1,
                feedback_blocks: result.block ? [...(item.feedback_blocks || []), result.block] : item.feedback_blocks,
              }
            : item,
        ),
      );
      showToast(
        result.merged
          ? "Отзыв сохранён: карточка скрыта как дубль, её ссылки — в главной карточке"
          : result.merge_skipped
            ? "Отзыв сохранён, карточка не скрыта: она уже скрыта, выбрана в дайджест или главной с таким номером нет"
            // Блок ОС только хранится: учить им агентов будет сервис ОС.
            : judgeFeedback(signal)
              ? "Отзыв сохранён — агент учтёт его в следующем прогоне радара"
              : "Отзыв сохранён",
      );
      if (result.merged) {
        // Список заново: у главной карточки растёт «Объединено дублей» и появляются ссылки дубля.
        setSignals((current) => current.filter((item) => item.id !== signal.id));
      }
      if (result.merged || result.merge_skipped) {
        reload({ keepLoaded: true });
      } else {
        afterListChange();
      }
    } catch (error) {
      handleError(error, "Не удалось сохранить обратную связь по сигналу");
    } finally {
      setSaving((current) => ({ ...current, [signal.id]: false }));
    }
  }

  function updateCardFilters(patch: Partial<CardFilterValues>) {
    setCardFilters((current) => ({ ...current, ...patch }));
  }

  function toggleExpanded(signalId: number) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(signalId)) next.delete(signalId);
      else next.add(signalId);
      return next;
    });
  }

  function toggleFeedback(signalId: number) {
    setFeedbackOpen((current) => {
      const next = new Set(current);
      if (next.has(signalId)) next.delete(signalId);
      else next.add(signalId);
      return next;
    });
  }

  function draftOf(signalId: number): FeedbackDraft {
    return feedbackDrafts[signalId] || EMPTY_FEEDBACK_DRAFT;
  }

  function closeFeedback(signalId: number) {
    setFeedbackOpen((current) => {
      const next = new Set(current);
      next.delete(signalId);
      return next;
    });
  }

  // «Отмена» — форма закрывается, черновик не сохраняется.
  function cancelFeedback(signalId: number) {
    setFeedbackDrafts((current) => ({ ...current, [signalId]: EMPTY_FEEDBACK_DRAFT }));
    closeFeedback(signalId);
  }

  function updateFeedbackDraft(signalId: number, patch: Partial<FeedbackDraft>) {
    setFeedbackDrafts((current) => ({
      ...current,
      [signalId]: { ...(current[signalId] || EMPTY_FEEDBACK_DRAFT), ...patch },
    }));
  }

  const searchNotice = isAdmin ? searchHealthNotice(searchHealth) : "";
  // «N из M сигналов»: N — в выборке по фильтрам, M — вся витрина (оба числа — с сервера). У «Отклонённых» — их
  // число: с витриной его не сравнить.
  const countBadge = searching
    ? "Обновляем выборку…"
    : summary
      ? (showHidden ? `Отклонённых: ${summary.matching}` : `${summary.matching} из ${summary.total} ${signalsAfterFrom(summary.total)}`) +
        (remaining > 0 ? ` · показаны ${visibleSignals.length}` : "")
      : "";
  // Включённые условия панели «Фильтры» — чипами со снятием; их число — на кнопке (вариант A, 23.10).
  const activeFilters: Array<{ key: string; label: string; clear: () => void }> = [
    ...(maturity
      ? [{ key: "maturity", label: `Решение платформы: ${MATURITY_LABELS[maturity] || maturity}`, clear: () => setMaturity("") }]
      : []),
    ...CHOICE_FIELDS.filter(({ filter }) => cardFilters[filter]).map(({ filter, label }) => ({
      key: filter, label: `${label}: ${cardFilters[filter]}`, clear: () => updateCardFilters({ [filter]: "" }),
    })),
    ...(cardFilters.company.trim()
      ? [{ key: "company", label: `Компания: ${cardFilters.company.trim()}`, clear: () => updateCardFilters({ company: "" }) }]
      : []),
  ];

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>{screenText.heading}</h1>
        </div>
      </header>

      {showHidden ? (
        <div className="archiveNotice" role="status">
          <span>
            Отклонённые одним списком: что убрали «Отбор» и поиск радара, сбои «Отбора» и скрытое прежними
            правилами радара. Причина у каждой карточки: «кто убрал → причина».
          </span>
        </div>
      ) : null}

      {searchNotice ? (
        // Стиль спокойного уведомления экранов ленты и выпуска (не красный).
        <div className="archiveNotice" role="status">
          <span>{searchNotice}</span>
        </div>
      ) : null}

      {/* «Итоги недели» (03.10): что вышло, что отсеяно и почему, что ждёт разбора — для
          еженедельного разбора с заказчиком. */}
      {weekly ? (
        <section className="panel signalWeekly" aria-label="Итоги недели">
          <button
            type="button"
            className="signalWeeklyHead"
            aria-expanded={weeklyOpen}
            onClick={() => setWeeklyOpen((current) => !current)}
          >
            <strong>Итоги за {weekly.days} дн.</strong>
            <span>
              на радаре новых: {weekly.on_radar}
              {weekly.filtered != null ? ` · отсеяно и скрыто: ${weekly.filtered}` : ""} · ждут разбора:{" "}
              {weekly.awaiting_review} · отзывов: {weekly.feedback}
            </span>
          </button>
          {weeklyOpen ? (
            <div className="signalWeeklyBody">
              {weekly.top.length ? (
                <div>
                  <div className="signalEvidenceHeading">Лучшие новые</div>
                  <ol>
                    {weekly.top.map((row) => (
                      <li key={row.id}>
                        <span className="signalIdText">#{row.id}</span> {row.title} · {Math.round(Number(row.score || 0))}
                      </li>
                    ))}
                  </ol>
                </div>
              ) : null}
              {weekly.filtered_reasons?.length ? (
                <div>
                  <div className="signalEvidenceHeading">Почему отсеяно</div>
                  <ul>
                    {weekly.filtered_reasons.map((row) => (
                      <li key={row.reason}>{row.reason}: {row.count}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          ) : null}
        </section>
      ) : null}

      {/* Плитки — по всей витрине: поиск и фильтры сужают список, но не эти числа. «Отклонённые» — админу, одна
          плашка в обеих витринах (#130, решение владельца 23.10): по ней открывается их список. */}
      {summary ? (
        <section className="statsGridReact" aria-label={screenText.summary}>
          <StatCard label="Всего сигналов" value={summary.total} />
          <StatCard label="Новые за 7 дней" value={summary.new_7d} />
          <StatCard label="В дайджесте" value={summary.in_digest} />
          <StatCard label="С обратной связью" value={summary.with_feedback} />
          <StatCard label="Объединено дублей" value={summary.merged} />
          {isAdmin && summary.hidden != null ? (
            <button
              type="button"
              className="statCardReact statCardButton"
              aria-pressed={showHidden}
              aria-label={`Отклонённые: ${summary.hidden}`}
              onClick={() => setShowHidden((current) => !current)}
            >
              <span className="statValueReact">{summary.hidden}</span>
              <span className="metaText">Отклонённые</span>
            </button>
          ) : null}
        </section>
      ) : null}

      <section className="panel">
        <div className="panelHeader">
          <h2>{showHidden ? "Отклонённые" : screenText.catalog}</h2>
          <div className="settingsActions signalRadarPanelActions">
            {countBadge ? <span className="badge">{countBadge}</span> : null}
            {showHidden ? (
              <button type="button" className="primaryButton" onClick={() => setShowHidden(false)}>
                {business ? "К ленте" : "К радару"}
              </button>
            ) : null}
            {groups.length ? (
              <>
                <button
                  type="button"
                  className="ghostButton"
                  onClick={() => {
                    setExpandedGroups(new Set(groups.map(([group]) => group)));
                    setCollapsedGroups(new Set());
                  }}
                >
                  Развернуть всё
                </button>
                <button
                  type="button"
                  className="ghostButton"
                  onClick={() => {
                    setExpandedGroups(new Set());
                    setCollapsedGroups(new Set(groups.map(([group]) => group)));
                  }}
                >
                  Свернуть всё
                </button>
              </>
            ) : null}
            <button type="button" className="ghostButton signalRadarRefresh" disabled={searching} onClick={() => reload()}>
              Обновить
            </button>
          </div>
        </div>

        {/* Вариант A (решение владельца 23.10): на виду поиск, тема и сортировка, остальные фильтры — в панели
            «Фильтры · N», включённые — чипами со снятием. Поля балла и дат сняты. */}
        <div className="articlesFiltersRow signalToolbar">
          <label className="field">
            <span>Поиск</span>
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder={`Поиск по ${screenText.searchIn}: название, суть, источник, #номер`}
              // Не длиннее, чем принимает API (q ≤ 200): иначе 422 с сырым текстом ошибки.
              maxLength={200}
            />
          </label>
          <label className="field">
            <span>Тема</span>
            <select value={theme} onChange={(event) => setTheme(event.target.value)}>
              <option value="">Все темы</option>
              {themes.map((item) => <option value={item} key={item}>{item}</option>)}
            </select>
          </label>
          <label className="field">
            <span>Сортировка</span>
            <select value={sort} onChange={(event) => setSort(event.target.value as SignalSort)}>
              <option value="score_desc">Балл: по убыванию</option>
              <option value="date_desc">Сначала новые</option>
              <option value="score_asc">Балл: по возрастанию</option>
            </select>
          </label>
          <button
            type="button"
            className={filtersOpen ? "primaryButton signalFiltersButton" : "ghostButton signalFiltersButton"}
            aria-expanded={filtersOpen}
            aria-controls={filtersPanelId}
            onClick={() => setFiltersOpen((current) => !current)}
          >
            {activeFilters.length ? `Фильтры · ${activeFilters.length}` : "Фильтры"}
          </button>
        </div>

        {activeFilters.length ? (
          <div className="signalActiveFilters" role="group" aria-label="Включённые фильтры">
            {activeFilters.map((item) => (
              <button
                type="button"
                key={item.key}
                className="signalChip signalFilterChip"
                aria-label={`Снять: ${item.label}`}
                onClick={item.clear}
              >
                {item.label} <span aria-hidden="true">×</span>
              </button>
            ))}
            <button type="button" className="ghostButton compactButton" onClick={resetFilters}>
              Сбросить
            </button>
          </div>
        ) : null}

        {filtersOpen ? (
          <div className="signalFilterPanel" id={filtersPanelId} role="group" aria-label="Фильтры">
            <label className="field">
              <span>Решение платформы</span>
              <select value={maturity} onChange={(event) => setMaturity(event.target.value)}>
                <option value="">Любое решение</option>
                <option value="watch">Наблюдать</option>
                <option value="shortlist">Кандидат</option>
                <option value="proven">Подтверждено</option>
                <option value="reject">Отклонено</option>
              </select>
            </label>
            <SignalCardFilters dictionaries={dictionaries} values={cardFilters} onChange={updateCardFilters} />
          </div>
        ) : null}
        {/* Кого добавил фильтр «всё про компанию» (дочки) — видно и при закрытой панели. */}
        <CompanyGroupHint company={cardFilters.company} group={summary?.company_group} />

        {!loaded ? (
          <div className="emptyState">Загружаем сигналы...</div>
        ) : visibleSignals.length ? (
          <div className="articleGroupsStack">
          {groups.map(([group, groupSignals]) => {
            // Свёрнуто по умолчанию, как в «Бизнес-сигналах»; раскрыто при поиске, фильтре атрибута
            // карточки («всё про компанию» видно сразу), выбранной теме или если блок один.
            const forcedOpen = Boolean(search.trim()) || narrowedByCard || theme === group || groups.length === 1;
            const groupOpen = !collapsedGroups.has(group) && (expandedGroups.has(group) || forcedOpen);
            const groupAvg = Math.round(
              groupSignals.reduce((sum, item) => sum + Number(item.score || 0), 0) / groupSignals.length,
            );
            return (
            <section className="articleGroupCard" key={group}>
              <button
                type="button"
                className={groupOpen ? "articleGroupHead articleGroupToggle open" : "articleGroupHead articleGroupToggle"}
                onClick={() => toggleGroup(group, groupOpen)}
                aria-expanded={groupOpen}
                aria-label={groupOpen ? `Свернуть группу ${group}` : `Раскрыть группу ${group}`}
              >
                <span className="articleGroupHeadMain">
                  <svg className="groupChevron" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                    <path d="M4 6.5 8 10l4-3.5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  <span className="miniPill muted">{group}</span>
                </span>
                <span className="articleGroupHeadMeta">
                  <span className="metaText">{groupSignals.length} сигналов · средняя</span>
                  <span className={`miniPill ${scoreClass(groupAvg)}`}>{groupAvg}</span>
                </span>
              </button>
              {groupOpen ? (
              <div className="articleRows">
              {groupSignals.map((signal) => {
            const isExpanded = expanded.has(signal.id);
            const isFeedbackOpen = feedbackOpen.has(signal.id);
            const savingThis = Boolean(saving[signal.id]);
            const title = signal.title_ru || signal.title;
            const primaryUrl = signal.evidence?.[0]?.source_url;
            const arrival = formatArrival(signal);
            // Дата самого события, а не поступления («Поступил» говорит, когда нашли).
            const eventDate = eventDateLabel(signal);
            const breakdown = scoreBreakdown(signal);
            const why = whyBlock(signal, business);
            const morePublications = Number(signal.evidence_count || 0) - (signal.evidence?.length ?? 0);
            const tone = scoreTone(signal);
            // Месяц карточки закрыт (признак сервера): отметку «в дайджест» не ставят и не снимают.
            const digestLocked = Boolean(signal.digest_locked);
            // Отклонённое «Отбором» и не решённое им в выпуск не выбирают: вернуть отклонённое можно только
            // полями «Отбора» (#132). Скрытое прежними правилами — как раньше, выбранное выходит на радар.
            const digestOffered = signal.otbor_decision !== "reject" && !(signal.otbor_failure && !signal.otbor_decision);
            // Поля судьи радара («Почему сейчас», «Переносимость») у статьи сбора пусты — как в ленте, их нет.
            const judgeFieldsOptional = business || signal.origin === "feed";
            return (
              <article className="articleCardReact" key={signal.id}>
                {/* Свёрнутая строка, как у «Бизнес-сигналов» (документ заказчика 19.09):
                    заголовок-ссылка и мета слева, дата, балл с оценкой и выбор — справа. */}
                <div className="articleCardTop">
                  <button
                    type="button"
                    className={isExpanded ? "expandButtonReact open" : "expandButtonReact"}
                    onClick={() => toggleExpanded(signal.id)}
                    aria-expanded={isExpanded}
                    aria-label={isExpanded ? "Свернуть сигнал" : "Раскрыть сигнал"}
                  >
                    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                      <path d="M4 6.5 8 10l4-3.5" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </button>
                  <div className="articleCardMain">
                    {primaryUrl ? (
                      <a href={primaryUrl} target="_blank" rel="noreferrer" className="articleTitleReact">
                        {title}
                      </a>
                    ) : (
                      <span className="articleTitleReact">{title}</span>
                    )}
                    <div className="metaText">
                      {cardMeta(signal, business)}
                      {/* Номер виден всегда: без него не сослаться на дубль в поле «ID дубля». */}
                      <span className="signalIdText">#{signal.id}</span>
                      {/* Дубли того же события скрыты, их ссылки — в этой карточке. */}
                      {Number(signal.merged_count || 0) > 0 ? ` · объединено дублей: ${signal.merged_count}` : ""}
                    </div>
                    <SignalCardChips signal={signal} onCompany={showCompany} />
                    {/* В разделе отклонённого — его короткая причина (#130); иначе — почему не на радаре. */}
                    {signal.section_reason ? (
                      <div className="metaText signalHiddenReason">Причина: {signal.section_reason}</div>
                    ) : signal.hidden_reason ? (
                      <div className="metaText signalHiddenReason">Не на радаре: {signal.hidden_reason}</div>
                    ) : null}
                    {/* «Отбор» сам себе противоречил и после повтора (#130): проверять первым. */}
                    {signal.otbor_disputed ? (
                      <div className="metaText signalDisputed">Спорное: {signal.otbor_disputed}</div>
                    ) : null}
                  </div>
                  <div className="articleCardMetrics">
                    {eventDate ? (
                      <div className={eventDate.gap ? "articleMetric gap" : "articleMetric"}>{eventDate.text}</div>
                    ) : null}
                    {arrival ? <div className="articleMetric">Поступил: {arrival}</div> : null}
                    <div className={`miniPill ${tone}`} title={scoreTitle(signal, breakdown.length > 0)}>
                      {Math.round(Number(signal.score || 0))}
                    </div>
                    <div className={`miniPill ${tone}`}>{signal.score_label || "—"}</div>
                    {/* «В дайджест» — одна механика в обеих витринах (#130): у сигнала ленты — отметка его
                        статьи, та же, что в ленте статей. */}
                    {digestOffered ? (
                      <button
                        type="button"
                        className={`${signal.selected_for_digest ? "dangerButton" : "primaryButton"} compactButton signalDigestButton`}
                        disabled={savingThis || digestLocked}
                        title={digestLocked ? radarDigestLockedText(signal.digest_month ?? "") : undefined}
                        onClick={() => void setDigest(signal, !signal.selected_for_digest)}
                      >
                        {signal.selected_for_digest ? "Убрать" : "В дайджест"}
                      </button>
                    ) : null}
                  </div>
                </div>

                {isExpanded ? (
                  <div className="articleDetailReact signalRadarDetail">
                    <div className="articleDetailGrid">
                      <div className="articleSummaryBox">
                        <strong>Суть</strong>
                        <p>{signal.summary || signal.thesis || "Суть сигнала ещё не сформирована."}</p>
                      </div>
                      <div className="signalRadarFacts">
                        {/* «Почему сейчас» и «Переносимость» — поля судьи радара: у статьи ленты их нет. */}
                        {judgeFieldsOptional && !signal.why_now ? null : (
                          <div>
                            <span>Почему сейчас</span>
                            <p>{signal.why_now || "Нет объяснения"}</p>
                          </div>
                        )}
                        {/* «Релевантности мало» (Виктор 29.09): судья называет, где это применить. */}
                        {signal.oilfield_application ? (
                          <div>
                            <span>
                              Применение в нефтесервисе
                              {signal.oilfield_relevance === "transferable" ? " · перенос из другой отрасли" : ""}
                            </span>
                            <p>{signal.oilfield_application}</p>
                          </div>
                        ) : null}
                        {judgeFieldsOptional && !signal.transferability ? null : (
                          <div>
                            <span>Переносимость</span>
                            <p>{signal.transferability || "Нет оценки"}</p>
                          </div>
                        )}
                        {/* Сравнение внутри пачки прогона, а не абсолютная оценка судьи:
                            поэтому рядом с баллом, но порядок списка — по баллу. */}
                        {/* Ответ на «оценки завышены» (Виктор 29.09): видно, за что балл. Итог —
                            сумма «балл × вес / 100», как у статей ленты. */}
                        {breakdown.length ? (
                          <div className="signalScoreBreakdown">
                            <span>Балл по критериям</span>
                            <ul>
                              {breakdown.map((item) => (
                                <li key={item.id}>
                                  <span>{item.name}{item.weight ? ` · вес ${item.weight}` : ""}</span>
                                  <strong>{item.score}</strong>
                                </li>
                              ))}
                            </ul>
                          </div>
                        ) : null}
                        {why ? (
                          <div>
                            <span>{why.label}</span>
                            <p>{why.text}</p>
                          </div>
                        ) : null}
                      </div>
                    </div>

                    {/* Почему принята или отсеяна (решение 03.10): заказчик видит причину и шаги. */}
                    {signal.verdict_reason || signal.decision_log?.length ? (
                      <div className="signalDecision">
                        {signal.verdict_reason ? (
                          <p className="signalDecisionReason">
                            <strong>Почему такое решение:</strong> {signal.verdict_reason}
                          </p>
                        ) : null}
                        {signal.decision_log?.length ? (
                          <>
                            <div className="signalEvidenceHeading">Ход решения</div>
                            <ol className="signalDecisionLog">
                              {signal.decision_log.map((step, index) => (
                                <li key={index}>
                                  <span className="signalDecisionStage">{step.stage}</span>
                                  <span>{step.text}</span>
                                </li>
                              ))}
                            </ol>
                          </>
                        ) : null}
                      </div>
                    ) : null}

                    {signal.evidence?.length ? (
                      <div className="signalEvidenceList">
                        {/* Публикации события — статья и её перепечатки (лента) или ссылки находки (радар). */}
                        <div className="signalEvidenceHeading">{screenText.evidence}</div>
                        {signal.evidence.map((item) => (
                          <a className="signalEvidenceRow" href={item.source_url} target="_blank" rel="noreferrer" key={item.id}>
                            <span>{evidenceSource(item)}</span>
                            <strong>{item.title_ru || item.title}</strong>
                            {item.summary_ru || item.extracted_fact ? (
                              <small>{item.summary_ru || item.extracted_fact}</small>
                            ) : null}
                          </a>
                        ))}
                        {/* Все статьи о событии — под карточкой (история 1 спеки #93): не уместились — счётом. */}
                        {business && morePublications > 0 ? (
                          <div className="metaText">{`и ещё ${morePublications} ${pluralWord(morePublications, ...PUBLICATION_WORDS)}`}</div>
                        ) : null}
                      </div>
                    ) : null}

                    {/* Блоки ОС (#130): каждый человек добавляет свой, чужие не перезаписываются; на отбор и показ
                        они не влияют. */}
                    {signal.feedback_blocks?.length ? (
                      <div className="signalFeedbackBlocks">
                        <div className="signalEvidenceHeading">Оценки людей</div>
                        <ul aria-label="Оценки людей">
                          {signal.feedback_blocks.map((block: SignalFeedbackBlock, index) => (
                            <li key={block.id ?? `block-${index}`}>
                              <span className="metaText">
                                {[block.author, blockDate(block.created_at)].filter(Boolean).join(" · ")}
                              </span>
                              {block.verdict ? <strong>{verdictName(block.verdict)}</strong> : null}
                              {block.reason ? <span>Причина: {block.reason}</span> : null}
                              {block.comment ? <span>{block.comment}</span> : null}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}

                    {/* Форма ОС — в обеих витринах (#130): у находки судьи радара вне бизнес-ленты — полная, у
                        остальных — блок ОС (оценка, причина и комментарий). */}
                    {isFeedbackOpen ? (
                      // Форма ОС (документ заказчика 19.09: «сделать поприятнее оформление»):
                      // на десктопе две колонки — пары полей одной высоты, подписи как в
                      // остальных формах, «Отмена» и «Сохранить» справа.
                      <div className="signalFeedbackBox">
                        <div className="signalFeedbackGrid">
                          <label className="field">
                            <span>Оценка сигнала</span>
                            <select
                              value={draftOf(signal.id).verdict}
                              onChange={(event) =>
                                updateFeedbackDraft(signal.id, { verdict: event.target.value as FeedbackDraft["verdict"] })
                              }
                            >
                              {verdictOptions(signal).map((item) => (
                                <option value={item.value} key={item.value || "empty"}>{item.label}</option>
                              ))}
                            </select>
                          </label>
                          {judgeFeedback(signal) ? (
                            <label className="field signalFeedbackDuplicate">
                              <span>ID дубля</span>
                              <input
                                inputMode="numeric"
                                value={draftOf(signal.id).duplicateOfSignalId}
                                onChange={(event) => updateFeedbackDraft(signal.id, { duplicateOfSignalId: event.target.value })}
                                placeholder="номер главной карточки, если это дубль"
                              />
                            </label>
                          ) : null}
                          <label className="field">
                            <span>Обоснование оценки</span>
                            <input
                              value={draftOf(signal.id).reason}
                              onChange={(event) => updateFeedbackDraft(signal.id, { reason: event.target.value })}
                              placeholder="чем обоснована оценка"
                            />
                          </label>
                          {judgeFeedback(signal) ? (
                            <>
                              <label className="field">
                                <span>Рекомендуемый заголовок</span>
                                <input
                                  value={draftOf(signal.id).correctedTitle}
                                  onChange={(event) => updateFeedbackDraft(signal.id, { correctedTitle: event.target.value })}
                                  placeholder="если нужно переименовать карточку"
                                />
                              </label>
                              <label className="field">
                                <span>Рекомендуемая формулировка сути</span>
                                <textarea
                                  value={draftOf(signal.id).correctedThesis}
                                  onChange={(event) => updateFeedbackDraft(signal.id, { correctedThesis: event.target.value })}
                                  placeholder="эталонная формулировка сути сигнала"
                                />
                              </label>
                            </>
                          ) : null}
                          <label className="field">
                            <span>{judgeFeedback(signal) ? "Рекомендации AI-агенту" : "Комментарий"}</span>
                            <textarea
                              value={draftOf(signal.id).comment}
                              onChange={(event) => updateFeedbackDraft(signal.id, { comment: event.target.value })}
                              placeholder={judgeFeedback(signal) ? "термины, поисковый угол, сильный источник..." : "что важно знать о сигнале"}
                            />
                          </label>
                        </div>
                        <div className="signalFeedbackActions">
                          <span className="metaText">Обратная связь: {signal.feedback_count || 0}</span>
                          <div className="signalFeedbackButtons">
                            <button type="button" className="ghostButton" disabled={savingThis} onClick={() => cancelFeedback(signal.id)}>
                              Отмена
                            </button>
                            <button type="button" className="primaryButton" disabled={savingThis} onClick={() => void submitFeedback(signal)}>
                              Сохранить
                            </button>
                          </div>
                        </div>
                      </div>
                    ) : (
                      <div className="signalFeedbackCollapsed">
                        <span className="metaText">Обратная связь: {signal.feedback_count || 0}</span>
                        <button type="button" className="ghostButton compactButton" onClick={() => toggleFeedback(signal.id)}>
                          Обратная связь
                        </button>
                      </div>
                    )}
                  </div>
                ) : null}
              </article>
            );
          })}
              </div>
              ) : null}
            </section>
            );
          })}
          {remaining > 0 ? (
            <div className="showMoreWrap">
              <button type="button" className="ghostButton" disabled={loadingMore || searching} onClick={() => void showMore()}>
                {loadingMore
                  ? "Загружаем…"
                  : `Показать ещё ${Math.min(RADAR_PAGE_SIZE, remaining)} (осталось ${remaining})`}
              </button>
            </div>
          ) : null}
          </div>
        ) : (
          <div className="emptyState">
            {/* Компания — целиком, а не подстрокой («Газпром» — не «Газпром нефть»): часть имени ничего
                не найдёт, и это не «сигналов нет». */}
            {cardFilters.company.trim()
              ? "Сигналов по выбранным фильтрам нет. Компания ищется по имени целиком — выберите её из подсказок поля «Компания»."
              : "Сигналов по выбранным фильтрам нет."}
          </div>
        )}
      </section>
    </section>
  );
}
