import { useEffect, useId, useMemo, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import {
  createSignalFeedback,
  editIssueMaterial,
  getSignalDictionaries,
  getSignalSearchHealth,
  getSignalSummary,
  listSignals,
  restoreSignal,
  returnSignal,
  updateSignal,
} from "../../api/signals";
import type {
  SignalDictionaries,
  SignalFilters,
  SignalReturnPayload,
  SignalSearchHealth,
  SignalShowcase,
  SignalSort,
  SignalSummary,
  SignalTile,
} from "../../api/signals";
import type { Signal, SignalEvidence, SignalFeedbackBlock } from "../../api/types";
import { radarDigestLockedText } from "../shared/feedWindow";
import { ratingClass, scoreClass } from "../shared/scoreScale";
import { SkeletonList } from "../shared/Skeleton";
import { PROFILES } from "../scoring/ScoringPage";
import { CHOICE_FIELDS, COUNTRY_FILTER, SignalAttributes, SignalCardLine, whyBlock } from "./SignalCardChips";
import { SignalDecisionLog } from "./SignalDecisionLog";
import { SignalHypothesis } from "./SignalHypothesis";
import {
  ARRIVAL_FILTER_LABEL, CompanyGroupHint, EMPTY_CARD_FILTERS, FEEDBACK_FILTER, SignalCardFilters, arrivalPeriod,
} from "./SignalCardFilters";
import type { CardFilterValues } from "./SignalCardFilters";
import { EMPTY_FEEDBACK_DRAFT, SignalFeedbackForm, duplicateMainId, verdictName } from "./SignalFeedbackForm";
import type { FeedbackDraft } from "./SignalFeedbackForm";
import { SignalReturnForm } from "./SignalReturnForm";

// Страница выдачи: остальное: «Показать ещё». Выборку и страницу считает сервер по всему
// радару, а не экран по загруженным карточкам (замечание заказчика 19.09).
export const RADAR_PAGE_SIZE = 100;
// Больше за один запрос сервер не отдаёт (/api/signals, limit ≤ 200).
const RADAR_MAX_PAGE = 200;
// Задержка серверного поиска, как у ленты бизнес-сигналов: запрос не на каждую букву.
const SEARCH_DELAY_MS = 400;
const DEFAULT_SORT: SignalSort = "score_desc";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
  isAdmin?: boolean;
  // Витрина (#97): «technology», технологический радар, «business», бизнес-лента, тот же экран:
  // карточка события, публикации снизу, атрибуты чипами. «В дайджест» и блоки ОС: в обеих витринах,
  // одной механикой (решение владельца 23.10, #130); служебное радара: только у радара.
  showcase?: SignalShowcase;
};

// Подписи экрана по витрине. Публикаций у события ленты (статья и перепечатки): сколько сервер отдаёт
// за раз (evidence_limit ≤ 20), у карточки радара: 5, как раньше.
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

// Плашки над списком и их фильтры: «Всего» снимает фильтр плашки.
const TILES: Array<{ tile: SignalTile | ""; label: string }> = [
  { tile: "", label: "Всего сигналов" },
  { tile: "new_7d", label: "Новые за 7 дней" },
  { tile: "in_digest", label: "В дайджесте" },
  { tile: "with_feedback", label: "С обратной связью" },
];

const TILE_EMPTY: Record<SignalTile, string> = {
  new_7d: "Новых за 7 дней сигналов здесь нет.",
  in_digest: "Отмеченных в дайджест сигналов здесь нет. Отметить можно кнопкой «В дайджест» на карточке.",
  with_feedback: "Сигналов с обратной связью здесь нет.",
};

function blockDate(value: string | null): string {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleDateString("ru-RU", { timeZone: RADAR_TIME_ZONE }) : "";
}

const EARLY_THEME_GROUP = "Ранние карточки — тема вне 13 тематик";

// Сутки радара и крон 07:15: по Москве, поэтому и время прогона показываем по Москве.
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

// Профиль балла (ADR 0002): у находки радара: «Технологический радар», у сигнала ленты: профиль его
// статьи, «Бизнес-сигналы». Имена: как на экране «Скоринг».
function profileLabel(signal: Signal): string | undefined {
  return PROFILES.find((item) => item.id === signal.score_profile)?.label;
}

// Разбивка балла по критериям профиля: имя и вес: из снимка набора на момент оценки, а не из
// нынешнего экрана «Скоринг» (там их могли поменять).
function scoreBreakdown(
  signal: Signal,
): Array<{ id: number; name: string; weight: number; score: number; excluded: string | null }> {
  if (!profileLabel(signal)) return [];
  const byId = new Map((signal.criteria_snapshot || []).map((item) => [Number(item.id), item]));
  return (signal.score_items_json || []).map((item) => {
    const criterion = byId.get(Number(item.criterion_id));
    return {
      id: Number(item.criterion_id),
      name: criterion?.name || `Критерий ${item.criterion_id}`,
      weight: Number(criterion?.weight || 0),
      score: Math.round(Number(item.final_score || 0)),
      // «Свежесть» без даты события в тексте в итог не входит (#132): балла у неё нет, и «0» здесь солгал бы.
      excluded: item.excluded ?? null,
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
// публикации» эталона заказчика). Даты нет: нет и пустого «·».
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

const PUBLICATION_WORDS = ["публикация", "публикации", "публикаций"] as const;

// «Спорный сигнал: …»: коротко, без служебного зачина «шаги ответа не сошлись и после повтора» (otbor.py).
function disputedWhy(text: string): string {
  return text.replace(/^шаги ответа не сошлись(?: и после повтора)?:\s*/i, "") || text;
}

// Раскрыть или свернуть карточку, её форму ОС или форму возврата: новый набор номеров без этого или с ним.
function toggled(current: Set<number>, signalId: number): Set<number> {
  const next = new Set(current);
  if (next.has(signalId)) next.delete(signalId);
  else next.add(signalId);
  return next;
}

// Цвет балла, по словесной оценке, как в ленте: число и слово одного цвета.
function scoreTone(signal: Signal): string {
  return signal.score_label ? ratingClass(signal.score_label) : scoreClass(Number(signal.score || 0));
}

// Плашка только для админа: 23-27.09 поиск не отвечал ни в одной теме, задача была «ok»,
// и пять дней этого никто не видел. После прогона без сбоев плашки нет. Причина: короткий
// текст сервера (первая ошибка поиска GPT).
function searchHealthNotice(health: SignalSearchHealth | null): string {
  if (!health || !health.topics || !health.failed) return "";
  const when = formatRunMoment(health.run_at);
  const topicsWord = health.topics % 10 === 1 && health.topics % 100 !== 11 ? "темы" : "тем";
  const cause = (health.first_error || "").trim();
  const noSignals = health.signals === 0 ? " Новых сигналов нет." : "";
  return (
    `Прогон${when ? ` ${when}` : ""}: поиск не ответил в ${health.failed} из ${health.topics} ${topicsWord}` +
    `${cause ? ` (${cause})` : ""}.${noSignals}`
  );
}

export function SignalRadarPage({ onUnauthorized, showToast, isAdmin = false, showcase = "technology" }: Props) {
  const business = showcase === "business";
  const screenText = SCREENS[showcase];
  const [signals, setSignals] = useState<Signal[]>([]);
  const [summary, setSummary] = useState<SignalSummary | null>(null);
  // Первый ответ ещё не пришёл: вместо списка «Загружаем»; дальше старая выборка видна,
  // пока идёт новая.
  const [loaded, setLoaded] = useState(false);
  const [searching, setSearching] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [maturity, setMaturity] = useState("");
  const [theme, setTheme] = useState("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SignalSort>(DEFAULT_SORT);
  // Вариант A (решение владельца 23.10): на виду поиск, тема и сортировка, остальные фильтры: в панели.
  const [filtersOpen, setFiltersOpen] = useState(false);
  const filtersPanelId = useId();
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [feedbackOpen, setFeedbackOpen] = useState<Set<number>>(new Set());
  // Форма возврата отклонённого (#132): у админа в «Отклонённых».
  const [returnOpen, setReturnOpen] = useState<Set<number>>(new Set());
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  // Свёрнутые вручную: сильнее авто-раскрытия при поиске и выбранной теме.
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [feedbackDrafts, setFeedbackDrafts] = useState<Record<number, FeedbackDraft>>({});
  const [saving, setSaving] = useState<Record<number, boolean>>({});
  const [searchHealth, setSearchHealth] = useState<SignalSearchHealth | null>(null);
  // Плашки над списком: фильтры (решение владельца 23.10): по нажатию список тех же карточек, что плашка посчитала;
  // «Всего сигналов»: без фильтра, «Отклонённые»: их список (только админу).
  const [tile, setTile] = useState<SignalTile | "">("");
  // Отметка «В дайджест» только что поставлена: короткая подсветка кнопки карточки и плашки «В дайджесте».
  const [digestFlash, setDigestFlash] = useState<number | null>(null);
  // «Отклонённые»: только админу (решение владельца 05.10; эксперт заказчика тоже админ): все разделы «кто убрал →
  // причина» (#130) одним списком в обеих витринах, причина: в самой карточке (решение владельца 23.10).
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
      country: cardFilters.country || undefined,
      company: cardFilters.company.trim() || undefined,
      since: cardFilters.arrivalFrom || undefined,
      until: cardFilters.arrivalTo || undefined,
      feedback: cardFilters.feedback || undefined,
      tile: !showHidden && tile ? tile : undefined,
    }),
    [business, search, theme, maturity, isAdmin, showHidden, cardFilters, tile],
  );
  // Номер последнего запроса выборки: ответ на устаревший запрос не применяется: иначе при
  // быстрой смене фильтров поздний ответ старой выборки встал бы поверх новой.
  const requestSeq = useRef(0);
  const pendingTimer = useRef<number | undefined>(undefined);
  const firstRequest = useRef(true);
  // Новая выборка поставлена или в пути: её ответ заменит список целиком. Ref, а не только
  // searching: читается после await, где состояние из замыкания уже устарело.
  const selectionPending = useRef(false);
  // Текущая выборка, для продолжений после await (отзыв, «В дайджест»): замыкание того
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

  // Фильтры применяются сразу, без «Обновить»: любая смена: новая выборка с сервера.
  useEffect(() => {
    const delay = firstRequest.current ? 0 : SEARCH_DELAY_MS;
    firstRequest.current = false;
    load(filters, sort, delay);
  }, [filters, sort]);

  useEffect(() => () => window.clearTimeout(pendingTimer.current), []);

  useEffect(() => {
    // Справочники не пришли: нет списков атрибутов; поиск по компании работает и без них.
    getSignalDictionaries()
      .then(setDictionaries)
      .catch(() => setDictionaries(null));
  }, []);

  useEffect(() => {
    // Обычный пользователь здоровье поиска не запрашивает: эндпоинт только для админа. Поиск: радара.
    if (!isAdmin || business) return;
    getSignalSearchHealth()
      .then((response) => setSearchHealth(response.search_health))
      // Служебная плашка не должна мешать экрану: не загрузилась: её просто нет.
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

  // keepLoaded: перечитать столько, сколько уже догружено «Показать ещё» (не больше, чем
  // сервер отдаёт за раз): после склейки дубля человек не теряет место в списке.
  function reload(options: { keepLoaded?: boolean } = {}) {
    const { filters: query, sort: order, loaded } = latestQuery.current;
    const size = options.keepLoaded ? Math.min(RADAR_MAX_PAGE, Math.max(RADAR_PAGE_SIZE, loaded)) : RADAR_PAGE_SIZE;
    load(query, order, 0, size);
  }

  // Плитки после «В дайджест» и отзыва: числа: с сервера, по всему радару. Ответ для
  // устаревшей выборки не применяется; сбой тихий: плитки просто остаются прежними.
  function refreshSummary() {
    const seq = requestSeq.current;
    getSignalSummary(latestQuery.current.filters)
      .then((counts) => {
        if (seq === requestSeq.current) setSummary(counts);
      })
      .catch(() => undefined);
  }

  // «В дайджест» или отзыв легли, пока в пути новая выборка: её ответ собран до отметки и
  // откатил бы её на экране (ревью PR #83): перечитываем выборку заново, на той же глубине.
  // Иначе хватает плиток.
  function afterListChange() {
    if (selectionPending.current) reload({ keepLoaded: true });
    else refreshSummary();
  }

  // Снять фильтры выборки, нажатая плитка тоже фильтр (ревью PR #155). Порядок списка, не фильтр: его возвращает
  // только «Сбросить».
  function clearFilters() {
    setSearch("");
    setTheme("");
    setMaturity("");
    setCardFilters(EMPTY_CARD_FILTERS);
    setTile("");
  }

  function resetFilters() {
    clearFilters();
    setSort(DEFAULT_SORT);
  }

  // Клик по компании на карточке, «всё про компанию» (#97): все сигналы витрины с её участием, а не
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
      // Между страницами радар мог добавить карточку: повтор не рисуем дважды.
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

  // В фильтре, только тематики заказчика, и со всего радара, а не с текущей выборки: иначе
  // после выбора темы в списке осталась бы она одна. Ранние карточки: отдельным блоком.
  const themes = useMemo(() => {
    const names = (summary?.themes ?? []).map((item) => item.theme);
    return theme && !names.includes(theme) ? [...names, theme] : names;
  }, [summary, theme]);
  const visibleSignals = signals;
  const remaining = Math.max(0, (summary?.matching ?? signals.length) - signals.length);
  const narrowedByCard = Object.values(cardFilters).some((value) => value.trim());
  const narrowedBesidesTile = Boolean(search.trim() || theme || maturity) || narrowedByCard;

  const groups = useMemo(() => {
    const byTheme = new Map<string, Signal[]>();
    for (const signal of visibleSignals) {
      // Ранние карточки радара (первая партия 13.09), с темой свободным текстом: один блок, а не
      // десятки блоков по одной карточке. Тематику им даст разбор, а не догадка по словам. У ленты
      // свободных тем нет: статья без тематики: в блоке своей темы («не классифицировано»).
      const key = !business && signal.theme_is_topic === false ? EARLY_THEME_GROUP : signal.theme || "Без темы";
      byTheme.set(key, [...(byTheme.get(key) || []), signal]);
    }
    // Блоки, в порядке выдачи, как у ленты: первым тот, чья карточка первая по выбранной
    // сортировке (по баллу: блок с лучшей карточкой, «сначала новые»: со свежей).
    // Ранние карточки: всегда последним блоком.
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
      if (selected) {
        setDigestFlash(signal.id);
        window.setTimeout(() => setDigestFlash((current) => (current === signal.id ? null : current)), 1200);
      }
      afterListChange();
    } catch (error) {
      handleError(error, "Не удалось обновить статус сигнала");
      // 409: месяц карточки закрылся, пока экран был открыт (день смены окна): выборка заново,
      // чтобы кнопка погасла по признаку сервера, а не ждала «Обновить».
      if (error instanceof ApiError && error.status === 409) reload({ keepLoaded: true });
    } finally {
      setSaving((current) => ({ ...current, [signal.id]: false }));
    }
  }

  // «Гипотеза для Компании» с карточки сигнала (#162): та же правка, что на экране «Дайджест»; поле на сигнале.
  async function saveHypothesis(signal: Signal, text: string) {
    try {
      setSaving((current) => ({ ...current, [signal.id]: true }));
      const saved = await editIssueMaterial(signal.id, { hypothesis: text });
      const now = new Date().toISOString();
      setSignals((current) =>
        current.map((item) =>
          item.id === signal.id
            ? { ...item, hypothesis: text || null, hypothesis_set_at: now, hypothesis_author: saved.author ?? null,
                card_set_by: { ...(item.card_set_by || {}), hypothesis: "human" } }
            : item,
        ),
      );
      showToast(text ? "Гипотеза сохранена" : "Гипотеза стёрта");
    } catch (error) {
      handleError(error, "Не удалось сохранить гипотезу");
      if (error instanceof ApiError && error.status === 409) reload({ keepLoaded: true });
    } finally {
      setSaving((current) => ({ ...current, [signal.id]: false }));
    }
  }

  function toggleReturn(signalId: number) {
    setReturnOpen((current) => toggled(current, signalId));
  }

  // Возврат отклонённого (#132): поля «Отбора» вписывает человек; публикация уходит из «Отклонённых» и пройдёт
  // остаток конвейера (суть, карточку, баллы) с пометкой «спорное».
  async function returnRejected(signal: Signal, payload: SignalReturnPayload) {
    try {
      setSaving((current) => ({ ...current, [signal.id]: true }));
      await returnSignal(signal.id, payload);
      toggleReturn(signal.id);
      setSignals((current) => current.filter((item) => item.id !== signal.id));
      showToast("Публикация возвращена: она пройдёт суть, карточку и баллы с пометкой «спорное»");
      reload({ keepLoaded: true });
    } catch (error) {
      handleError(error, "Не удалось вернуть публикацию");
    } finally {
      setSaving((current) => ({ ...current, [signal.id]: false }));
    }
  }

  // «Вернуть» в «Убрано людьми» (#188): прежнее решение «Отбора» одним нажатием, карточка уходит из «Отклонённых».
  async function restoreRemoved(signal: Signal) {
    try {
      setSaving((current) => ({ ...current, [signal.id]: true }));
      await restoreSignal(signal.id);
      setSignals((current) => current.filter((item) => item.id !== signal.id));
      showToast("Карточка возвращена: прежнее решение «Отбора» восстановлено");
      reload({ keepLoaded: true });
    } catch (error) {
      handleError(error, "Не удалось вернуть карточку");
    } finally {
      setSaving((current) => ({ ...current, [signal.id]: false }));
    }
  }

  // Отметка ОС (#188): одна форма у любой карточки, сервер пишет её одним путём. Явные действия сразу применяет только
  // отметка администратора; что стало с карточкой, говорит ответ сервера.
  async function submitFeedback(signal: Signal) {
    const draft = feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT;
    const comment = draft.comment.trim();
    const reason = draft.reason.trim();
    const correctedTitle = draft.correctedTitle.trim();
    const correctedThesis = draft.correctedThesis.trim();
    const duplicate = duplicateMainId(draft);
    if (duplicate === "words") {
      showToast("Выберите главную карточку из подсказок или впишите её номер", "error");
      return;
    }
    if (!comment && !draft.verdict && !reason && !correctedTitle && !correctedThesis && !duplicate) {
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
        duplicate_of_signal_id: duplicate,
      });
      setFeedbackDrafts((current) => ({ ...current, [signal.id]: EMPTY_FEEDBACK_DRAFT }));
      // Сохранено, форма закрывается. Сохранённый блок ОС ложится в список на карточке (#130), и счётчик
      // «Обратная связь: N» растёт: он считает записи этого списка, как сервер (решение владельца 03.11). Применённая
      // правка без оценки, причины и комментария блоком не приходит: она уже на карточке.
      closeFeedback(signal.id);
      const block = result.block;
      // Правка администратора применена сразу: карточка показывает новый текст, как покажет и сервер.
      const edited = result.applied ? {
        ...(correctedTitle ? { title_ru: correctedTitle } : {}),
        ...(correctedThesis ? { summary: correctedThesis } : {}),
      } : {};
      setSignals((current) =>
        current.map((item) =>
          item.id === signal.id
            ? {
              ...item,
              ...edited,
              ...(block ? { feedback_count: (item.feedback_count || 0) + 1, feedback_blocks: [...(item.feedback_blocks || []), block] } : {}),
            }
            : item,
        ),
      );
      if (result.recategorized) {
        // «Не тот блок» у сигнала после «Отбора» (#132): категория сменилась сразу: карточка уже в другой
        // витрине, баллы там пересчитываются по профилю новой категории.
        const target = result.recategorized === "technology" ? "технологический радар" : "бизнес-ленту";
        showToast(`Отметка сохранена: карточка перешла в ${target}, баллы пересчитываются`);
      } else if (result.recategorize_refused) {
        showToast(`Отметка сохранена, категория не сменилась: ${result.recategorize_refused}`);
      } else if (result.merged) {
        showToast("Отметка сохранена: карточка склеена с главной, её ссылки в главной карточке");
      } else if (result.merge_refused) {
        showToast(`Отметка сохранена, карточка не склеена: ${result.merge_refused}`);
      } else if (result.removed) {
        showToast("Отметка сохранена: карточка убрана в «Убрано людьми», вернуть её можно в «Отклонённых»");
      } else if (result.remove_refused) {
        showToast(`Отметка сохранена, карточка не убрана: ${result.remove_refused}`);
      } else if (!result.applied) {
        showToast("Отметка сохранена и видна на карточке; заголовок, суть, категорию и склейку меняет администратор");
      } else {
        showToast("Отметка сохранена");
      }
      if (result.recategorized || result.merged || result.removed) {
        // Карточка ушла из этой выборки: в другую витрину, в главную карточку или в «Убрано людьми».
        setSignals((current) => current.filter((item) => item.id !== signal.id));
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
    setExpanded((current) => toggled(current, signalId));
  }

  function toggleFeedback(signalId: number) {
    setFeedbackOpen((current) => toggled(current, signalId));
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

  // «Отмена»: форма закрывается, черновик не сохраняется.
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
  // «N из M сигналов»: N: в выборке по фильтрам, M: вся витрина (оба числа: с сервера). У «Отклонённых»: их
  // число: с витриной его не сравнить.
  const countBadge = searching
    ? "Обновляем выборку…"
    : summary
      ? (showHidden ? `Отклонённых: ${summary.matching}` : `${summary.matching} из ${summary.total} ${signalsAfterFrom(summary.total)}`) +
        (remaining > 0 ? ` · показаны ${visibleSignals.length}` : "")
      : "";
  // Включённые условия панели «Фильтры»: чипами со снятием; их число: на кнопке (вариант A, 23.10).
  const activeFilters: Array<{ key: string; label: string; clear: () => void }> = [
    ...(maturity
      ? [{ key: "maturity", label: `Решение платформы: ${MATURITY_LABELS[maturity] || maturity}`, clear: () => setMaturity("") }]
      : []),
    ...CHOICE_FIELDS.filter(({ filter }) => cardFilters[filter]).map(({ filter, label }) => ({
      key: filter, label: `${label}: ${cardFilters[filter]}`, clear: () => updateCardFilters({ [filter]: "" }),
    })),
    ...(cardFilters.country
      ? [{
          key: "country", label: `${COUNTRY_FILTER.label}: ${cardFilters.country}`,
          clear: () => updateCardFilters({ country: "" }),
        }]
      : []),
    ...(cardFilters.company.trim()
      ? [{ key: "company", label: `Компания: ${cardFilters.company.trim()}`, clear: () => updateCardFilters({ company: "" }) }]
      : []),
    // Период как один фильтр: один чип снимает обе границы.
    ...(cardFilters.arrivalFrom || cardFilters.arrivalTo
      ? [{
          key: "arrival", label: `${ARRIVAL_FILTER_LABEL}: ${arrivalPeriod(cardFilters)}`,
          clear: () => updateCardFilters({ arrivalFrom: "", arrivalTo: "" }),
        }]
      : []),
    ...(cardFilters.feedback
      ? [{
          key: "feedback", label: FEEDBACK_FILTER.choices[cardFilters.feedback],
          clear: () => updateCardFilters({ feedback: "" }),
        }]
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
            правилами радара. Причина у каждой карточки во флагах раскрытой карточки: «кто убрал → причина».
          </span>
        </div>
      ) : null}

      {searchNotice ? (
        // Стиль спокойного уведомления экранов ленты и выпуска (не красный).
        <div className="archiveNotice" role="status">
          <span>{searchNotice}</span>
        </div>
      ) : null}

      {/* Плашки: по всей витрине (поиск и фильтры их не меняют) и они же фильтры (решение владельца 23.10): нажатая
          показывает свои карточки. «Отклонённые»: админу, одна плашка в обеих витринах (#130). */}
      {summary ? (
        <section className="statsGridReact signalTiles" aria-label={screenText.summary}>
          {TILES.map((item) => {
            const pressed = item.tile === "" ? !tile && !showHidden : tile === item.tile && !showHidden;
            const value = item.tile === "" ? summary.total : summary[item.tile];
            return (
              <button
                key={item.label}
                type="button"
                className="statCardReact statCardButton"
                aria-pressed={pressed}
                onClick={() => {
                  setShowHidden(false);
                  setTile(item.tile === "" || pressed ? "" : item.tile);
                }}
              >
                <span key={item.tile === "in_digest" ? value : undefined} className={item.tile === "in_digest" && digestFlash ? "statValueReact statValueFlash" : "statValueReact"}>
                  {value}
                </span>
                <span className="metaText">{item.label}</span>
              </button>
            );
          })}
          {isAdmin && summary.rejected != null ? (
            <button
              type="button"
              className="statCardReact statCardButton"
              aria-pressed={showHidden}
              aria-label={`Отклонённые: ${summary.rejected}`}
              onClick={() => {
                setTile("");
                setShowHidden((current) => !current);
              }}
            >
              <span className="statValueReact">{summary.rejected}</span>
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

        {/* Вариант A (решение владельца 23.10): на виду поиск, тема и сортировка, остальные фильтры: в панели
            «Фильтры · N», включённые: чипами со снятием. Поля балла сняты; дата поступления с 03.11 в панели. */}
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
          <div className="signalFilterPanel revealIn" id={filtersPanelId} role="group" aria-label="Фильтры">
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
        {/* Кого добавил фильтр «всё про компанию» (дочки): видно и при закрытой панели. */}
        <CompanyGroupHint company={cardFilters.company} group={summary?.company_group} />

        {!loaded ? (
          <SkeletonList rows={5} label="Загружаем сигналы" />
        ) : visibleSignals.length ? (
          <div className="articleGroupsStack">
          {groups.map(([group, groupSignals]) => {
            // Свёрнуто по умолчанию, как в «Бизнес-сигналах»; раскрыто при поиске, фильтре атрибута
            // карточки («всё про компанию» видно сразу), нажатой плитке, выбранной теме или если блок один.
            const forcedOpen = Boolean(search.trim()) || narrowedByCard || Boolean(tile) || showHidden
              || theme === group || groups.length === 1;
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
                  <span className="metaText">
                    {groupSignals.length} {pluralWord(groupSignals.length, "сигнал", "сигнала", "сигналов")} · средняя
                  </span>
                  <span className={`miniPill ${scoreClass(groupAvg)}`}>{groupAvg}</span>
                </span>
              </button>
              {groupOpen ? (
              <div className="articleRows revealIn">
              {groupSignals.map((signal) => {
            const isExpanded = expanded.has(signal.id);
            const isFeedbackOpen = feedbackOpen.has(signal.id);
            const savingThis = Boolean(saving[signal.id]);
            const title = signal.title_ru || signal.title;
            const primaryUrl = signal.evidence?.[0]?.source_url;
            const arrival = formatArrival(signal);
            const breakdown = scoreBreakdown(signal);
            const why = whyBlock(signal, business);
            const morePublications = Number(signal.evidence_count || 0) - (signal.evidence?.length ?? 0);
            const tone = scoreTone(signal);
            // Месяц карточки закрыт (признак сервера): отметку «в дайджест» не ставят и не снимают.
            const digestLocked = Boolean(signal.digest_locked);
            // Отклонённое «Отбором» и не решённое им в выпуск не выбирают: вернуть отклонённое можно только
            // полями «Отбора» (#132). Скрытое прежними правилами: как раньше, выбранное выходит на радар.
            const digestOffered = signal.otbor_decision !== "reject" && !(signal.otbor_failure && !signal.otbor_decision);
            // Поля судьи радара («Почему сейчас», «Переносимость») у статьи сбора и материала коллеги пусты: их нет; у
            // находки на пути «Отбора» (решение или сбой) тоже: судьи там нет (#134).
            const judgeFieldsOptional = business || (signal.origin ?? "radar") !== "radar"
              || Boolean(signal.otbor_decision || signal.otbor_failure);
            return (
              <article className="articleCardReact" key={signal.id}>
                {/* Свёрнутая строка, как у «Бизнес-сигналов» (документ заказчика 19.09):
                    заголовок-ссылка и мета слева, дата, балл с оценкой и выбор: справа. */}
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
                    {/* Под заголовком, только «о чём»: тема, компании, регион, дата события (пересмотр 23.10). */}
                    <SignalCardLine signal={signal} onCompany={showCompany} />
                    {/* Под заголовком: только итог, коротко (решение владельца 24.10): «Отбор» сам себе противоречил
                        и после повтора (#130): спорный сигнал и почему. Причина отклонения или скрытия: флагом
                        в раскрытой карточке. */}
                    {signal.otbor_disputed ? (
                      <div className="signalCardStatus signalDisputed">Спорный сигнал: {disputedWhy(signal.otbor_disputed)}</div>
                    ) : null}
                  </div>
                  <div className="articleCardMetrics signalCardMetrics">
                    {/* Номер виден всегда: без него не сослаться на дубль в поле «ID дубля». */}
                    <span className="signalIdText metaText">№{signal.id}</span>
                    {/* «Не тот блок» (#132): до ответа второго вызова баллы прежнего профиля не показываются. */}
                    {signal.scores_pending ? (
                      <span className="miniPill muted" title="Категория сменилась: баллы пересчитываются по её профилю">
                        пересчитываются
                      </span>
                    ) : signal.unscored ? (
                      // Без баллов (#132): «другое», отклонённое, сбой «Отбора» и отсеянное поиском не оценивают, у
                      // принятого бизнеса и технологии второй вызов «Карточки» ещё не прошёл. «0» и «Низкая» солгали бы.
                      <span className="miniPill muted" title="Баллов нет: их ставят только принятым «Отбором» бизнесу и технологии, после карточки">
                        без баллов
                      </span>
                    ) : (
                      <span className={`scoreBadge ${tone}`} title={scoreTitle(signal, breakdown.length > 0)}>
                        <strong>{Math.round(Number(signal.score || 0))}</strong>
                        <span>{signal.score_label || "—"}</span>
                      </span>
                    )}
                    {/* «В дайджест», одна механика в обеих витринах (#130): у сигнала ленты: отметка его
                        статьи, та же, что в ленте статей. */}
                    {digestOffered ? (
                      <button
                        type="button"
                        className={`${signal.selected_for_digest ? "dangerButton" : "primaryButton"} compactButton signalDigestButton${digestFlash === signal.id ? " justAdded" : ""}`}
                        disabled={savingThis || digestLocked}
                        title={digestLocked ? radarDigestLockedText(signal.digest_month ?? "") : undefined}
                        onClick={() => void setDigest(signal, !signal.selected_for_digest)}
                      >
                        {signal.selected_for_digest ? "Убрать" : "В дайджест"}
                      </button>
                    ) : null}
                    {/* Убранное людьми (#188) возвращает «Вернуть» одним нажатием: прежнее решение «Отбора». */}
                    {isAdmin && signal.human_removal ? (
                      <button
                        type="button"
                        className="ghostButton compactButton"
                        disabled={savingThis}
                        onClick={() => void restoreRemoved(signal)}
                      >
                        Вернуть
                      </button>
                    ) : null}
                    {/* Отклонённое «Отбором» возвращает человек, вписав его поля (#132): разделы видит только админ. */}
                    {isAdmin && signal.otbor_decision === "reject" && !signal.human_removal && dictionaries?.otbor_return ? (
                      <button
                        type="button"
                        className="ghostButton compactButton"
                        aria-expanded={returnOpen.has(signal.id)}
                        disabled={savingThis}
                        onClick={() => toggleReturn(signal.id)}
                      >
                        Вернуть
                      </button>
                    ) : null}
                  </div>
                </div>
                {returnOpen.has(signal.id) && dictionaries?.otbor_return ? (
                  <SignalReturnForm
                    signal={signal}
                    choices={dictionaries.otbor_return}
                    saving={savingThis}
                    onSubmit={(payload) => void returnRejected(signal, payload)}
                    onCancel={() => toggleReturn(signal.id)}
                  />
                ) : null}

                {isExpanded ? (
                  <div className="articleDetailReact signalRadarDetail revealIn">
                    {/* Как в прежней карточке (решение владельца 24.10): суть слева и шире, все флаги карточки справа. */}
                    <div className="signalDetailTop">
                      <div className="articleSummaryBox">
                        <strong>Суть</strong>
                        <p>{signal.summary || signal.thesis || "Суть сигнала ещё не сформирована."}</p>
                      </div>
                      <div className="articleSummaryBox signalFlagsBox">
                        <strong>Флаги</strong>
                        <SignalAttributes
                          signal={signal}
                          onCompany={showCompany}
                          extra={[
                            // У «другого» после «Отбора» «Решения платформы» нет (#132).
                            ...(signal.maturity ? [{ key: "decision", label: "Решение платформы", value: MATURITY_LABELS[signal.maturity] || signal.maturity }] : []),
                            ...(arrival ? [{ key: "arrival", label: "Поступил", value: arrival }] : []),
                            { key: "links", label: business ? "Публикаций" : "Ссылок", value: String(Number(signal.evidence_count || 0)) },
                            ...(Number(signal.merged_count || 0) > 0 ? [{ key: "merged", label: "Объединено дублей", value: String(signal.merged_count) }] : []),
                            // Раздел отклонённого: короткая причина «кто убрал → причина» (#130); иначе: почему не на радаре.
                            ...(signal.section_reason
                              ? [{ key: "reason", label: "Причина", value: signal.section_reason, wide: true }]
                              : signal.hidden_reason
                                ? [{ key: "hidden", label: "Не на радаре", value: signal.hidden_reason, wide: true }]
                                : []),
                          ]}
                        />
                      </div>
                    </div>
                    <div className="signalRadarFacts">
                      {/* «Почему сейчас» и «Переносимость», поля судьи радара: у статьи ленты их нет. */}
                      {judgeFieldsOptional && !signal.why_now ? null : (
                        <div>
                          <span>Почему сейчас</span>
                          <p>{signal.why_now || "Нет объяснения"}</p>
                        </div>
                      )}
                      {/* «Релевантности мало» (эксперт заказчика, 29.09): судья называет, где это применить. */}
                      {signal.oilfield_application ? (
                        <div>
                          <span>
                            Применение в нефтесервисе
                            {signal.oilfield_relevance === "transferable" ? " · перенос из другой отрасли" : ""}
                          </span>
                          <p>{signal.oilfield_application}</p>
                        </div>
                      ) : null}
                      {/* «Гипотеза для Компании» (#162) сразу после «Применения», та же, что в выпуске. */}
                      <SignalHypothesis signal={signal} saving={savingThis} onSave={(text) => void saveHypothesis(signal, text)} />
                      {judgeFieldsOptional && !signal.transferability ? null : (
                        <div>
                          <span>Переносимость</span>
                          <p>{signal.transferability || "Нет оценки"}</p>
                        </div>
                      )}
                      {why ? (
                        <div>
                          <span>{why.label}</span>
                          <p>{why.text}</p>
                        </div>
                      ) : null}
                      {/* Ответ на «оценки завышены» (эксперт заказчика, 29.09): видно, за что балл. Итог: сумма «балл × вес / 100»,
                          как у статей ленты. */}
                      {breakdown.length ? (
                        <div className="signalScoreBreakdown">
                          <span>Балл по критериям</span>
                          <ul>
                            {breakdown.map((item) => (
                              <li key={item.id}>
                                <span>{item.name}{item.weight ? ` · вес ${item.weight}` : ""}</span>
                                {item.excluded ? (
                                  <strong title={item.excluded}>не входит</strong>
                                ) : (
                                  <strong>{item.score}</strong>
                                )}
                              </li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                    </div>

                    {/* Почему принята или отсеяна (решение 03.10): причина видна сразу, шаги: по кнопке. */}
                    <SignalDecisionLog signal={signal} />

                    {signal.evidence?.length ? (
                      <div className="signalEvidenceList">
                        {/* Публикации события: статья и её перепечатки (лента) или ссылки находки (радар). */}
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
                        {/* Все статьи о событии, под карточкой (история 1 спеки #93): не уместились: счётом. */}
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
                              {/* Отметка участника без прав администратора (#188): её правки и «Дубль» на карточку не
                                  легли, видно их здесь. */}
                              {block.proposed_title ? <span>Предлагает заголовок: {block.proposed_title}</span> : null}
                              {block.proposed_thesis ? <span>Предлагает суть: {block.proposed_thesis}</span> : null}
                              {block.duplicate_of_signal_id ? (
                                <span>Предлагает главную: №{block.duplicate_of_signal_id}</span>
                              ) : null}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}

                    {/* Форма ОС (#188): одна, как в технологическом радаре, у любой карточки обеих витрин и в
                        «Отклонённых». */}
                    {isFeedbackOpen ? (
                      <SignalFeedbackForm
                        signal={signal}
                        draft={draftOf(signal.id)}
                        saving={savingThis}
                        onChange={(patch) => updateFeedbackDraft(signal.id, patch)}
                        onCancel={() => cancelFeedback(signal.id)}
                        onSubmit={() => void submitFeedback(signal)}
                      />
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
            {/* Компания, целиком, а не подстрокой («Газпром», не «Газпром нефть»): часть имени ничего
                не найдёт, и это не «сигналов нет». Пусто под нажатой плиткой: сказать какой и дать выход;
                при других условиях пуста не плитка, а пересечение, поэтому и выход снимает всё. */}
            <span>
              {tile && !showHidden && !narrowedBesidesTile
                ? TILE_EMPTY[tile]
                : cardFilters.company.trim()
                  ? "Сигналов по выбранным фильтрам нет. Компания ищется по имени целиком — выберите её из подсказок поля «Компания»."
                  : "Сигналов по выбранным фильтрам нет."}
            </span>
            {tile && !showHidden ? (
              <button type="button" className="ghostButton compactButton" onClick={clearFilters}>
                Показать все сигналы
              </button>
            ) : null}
          </div>
        )}
      </section>
    </section>
  );
}
