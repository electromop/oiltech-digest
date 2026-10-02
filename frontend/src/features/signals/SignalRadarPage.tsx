import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import {
  createSignalFeedback,
  getSignalSearchHealth,
  getSignalSummary,
  listSignals,
  updateSignal,
} from "../../api/signals";
import type { SignalFilters, SignalSearchHealth, SignalSort, SignalSummary } from "../../api/signals";
import type { Signal, SignalEvidence, SignalFeedbackPayload } from "../../api/types";
import { radarDigestLockedText } from "../articles/feedWindow";
import { StatCard } from "../shared/StatCard";
import { ratingClass, scoreClass } from "../shared/scoreScale";

// Страница выдачи: остальное — «Показать ещё». Выборку и страницу считает сервер по всему
// радару, а не экран по загруженным карточкам (замечание заказчика 19.09).
export const RADAR_PAGE_SIZE = 100;
// Больше за один запрос сервер не отдаёт (/api/signals, limit ≤ 200).
const RADAR_MAX_PAGE = 200;
// Задержка серверного поиска — как у ленты бизнес-сигналов: запрос не на каждую букву.
const SEARCH_DELAY_MS = 400;
const EVIDENCE_PER_CARD = 5;
// Без фильтров видно всё: в отличие от ленты, радар слабые карточки по умолчанию не прячет.
const SCORE_MIN = 0;
const SCORE_MAX = 100;
const DEFAULT_SORT: SignalSort = "score_desc";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
  isAdmin?: boolean;
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
// поэтому машинная подписана в карточке как «Зрелость», а эта — как «Оценка сигнала».
const VERDICT_LABELS: Array<{ value: FeedbackDraft["verdict"]; label: string }> = [
  { value: "", label: "Не оценено" },
  { value: "strong_signal", label: "Сильный сигнал — вынести в дайджест / обсуждать" },
  { value: "approved", label: "Полезный сигнал — релевантно, сохранить в базе" },
  { value: "watch_later", label: "Наблюдать — рано, нужен следующий milestone" },
  { value: "background_material", label: "Фоновый материал — benchmark или контекст" },
  { value: "reject", label: "Низкая ценность / шум — по теме, но без новой ценности" },
  { value: "wrong_domain", label: "Не релевантно — вне интересов Компании" },
  // Встреча с заказчиком 21.09, решение 5: находка годная, но это бизнес-сигнал.
  { value: "wrong_block", label: "Не тот блок — бизнес-сигнал, а не технология" },
  { value: "merge_duplicate", label: "Дубль — тот же сигнал или технологический кластер" },
];

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

// Дата самого события от судьи (ГГГГ-ММ-ДД, без времени) — «нашёл очень старый сигнал»
// (Виктор 29.09): «Поступил» говорит, когда нашли, а не когда случилось. Строкой, без Date:
// дата без часового пояса в Date сдвинулась бы на сутки.
function formatEventDate(value: string | null | undefined): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || "");
  return match ? `${match[3]}.${match[2]}.${match[1]}` : "";
}

// Разбивка балла по критериям профиля «Технологический радар»: имя и вес — из снимка набора
// на момент оценки, а не из нынешнего экрана «Скоринг» (там их могли поменять).
function scoreBreakdown(signal: Signal): Array<{ id: number; name: string; weight: number; score: number }> {
  if (signal.score_profile !== "tech_radar") return [];
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

// Балл карточки — 0–100: поле пустое или вне шкалы — край шкалы, а не ошибка сервера.
function clampScore(value: string, fallback: number): number {
  if (value.trim() === "") return fallback;
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.max(SCORE_MIN, Math.min(SCORE_MAX, number));
}

// «1 ссылка», «2 ссылки», «5 ссылок».
function linksWord(count: number): string {
  const tens = count % 100;
  const units = count % 10;
  if (units === 1 && tens !== 11) return "ссылка";
  if (units >= 2 && units <= 4 && (tens < 12 || tens > 14)) return "ссылки";
  return "ссылок";
}

// Мета под заголовком карточки: «тема · N ссылок · зрелость · » — номер карточки следом.
// Зрелость — оценка модели; оценка человека в ОС называется «Оценка сигнала».
function cardMeta(signal: Signal): string {
  const links = Number(signal.evidence_count || 0);
  const maturity = MATURITY_LABELS[signal.maturity] || signal.maturity;
  return `${signal.theme} · ${links} ${linksWord(links)} · Зрелость: ${maturity} · `;
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

export function SignalRadarPage({ onUnauthorized, showToast, isAdmin = false }: Props) {
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
  const [scoreMin, setScoreMin] = useState(SCORE_MIN);
  const [scoreMax, setScoreMax] = useState(SCORE_MAX);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [showAdvancedFilters, setShowAdvancedFilters] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [feedbackOpen, setFeedbackOpen] = useState<Set<number>>(new Set());
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  // Свёрнутые вручную — сильнее авто-раскрытия при поиске и выбранной теме.
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [feedbackDrafts, setFeedbackDrafts] = useState<Record<number, FeedbackDraft>>({});
  const [saving, setSaving] = useState<Record<number, boolean>>({});
  const [searchHealth, setSearchHealth] = useState<SignalSearchHealth | null>(null);
  // «Отсеянные и скрытые» — всем (решение 02.10: Виктор разбирает всё): бизнес, смешанные
  // события, старые, не про нефтесервис, брак судьи, отсеянное поиском, архив.
  const [showHidden, setShowHidden] = useState(false);

  const filters: SignalFilters = useMemo(
    () => ({
      q: search.trim() || undefined,
      theme: theme || undefined,
      maturity: maturity || undefined,
      minScore: scoreMin !== SCORE_MIN ? scoreMin : undefined,
      maxScore: scoreMax !== SCORE_MAX ? scoreMax : undefined,
      since: dateFrom || undefined,
      until: dateTo || undefined,
      hidden: showHidden || undefined,
    }),
    [search, theme, maturity, scoreMin, scoreMax, dateFrom, dateTo, showHidden],
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
        listSignals({ ...query, sort: order, limit: size, offset: 0, evidenceLimit: EVIDENCE_PER_CARD }),
        getSignalSummary(query),
      ])
        .then(([rows, counts]) => {
          if (seq !== requestSeq.current) return;
          setSignals(rows);
          setSummary(counts);
        })
        .catch((error) => {
          if (seq === requestSeq.current) handleError(error, "Не удалось загрузить технологический радар");
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
    // Обычный пользователь здоровье поиска не запрашивает: эндпоинт только для админа.
    if (!isAdmin) return;
    getSignalSearchHealth()
      .then((response) => setSearchHealth(response.search_health))
      // Служебная плашка не должна мешать экрану: не загрузилась — её просто нет.
      .catch(() => setSearchHealth(null));
  }, [isAdmin]);

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

  function resetFilters() {
    setSearch("");
    setTheme("");
    setMaturity("");
    setSort(DEFAULT_SORT);
    setScoreMin(SCORE_MIN);
    setScoreMax(SCORE_MAX);
    setDateFrom("");
    setDateTo("");
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
        evidenceLimit: EVIDENCE_PER_CARD,
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

  const groups = useMemo(() => {
    const byTheme = new Map<string, Signal[]>();
    for (const signal of visibleSignals) {
      // Ранние карточки (первая партия 13.09) — с темой свободным текстом: один блок, а не
      // десятки блоков по одной карточке. Тематику им даст разбор, а не догадка по словам.
      const key = signal.theme_is_topic === false ? EARLY_THEME_GROUP : signal.theme || "Без темы";
      byTheme.set(key, [...(byTheme.get(key) || []), signal]);
    }
    // Блоки — в порядке выдачи, как у ленты: первым тот, чья карточка первая по выбранной
    // сортировке (по баллу — блок с лучшей карточкой, «сначала новые» — со свежей).
    // Ранние карточки — всегда последним блоком.
    return [...byTheme.entries()].sort(
      (a, b) => Number(a[0] === EARLY_THEME_GROUP) - Number(b[0] === EARLY_THEME_GROUP),
    );
  }, [visibleSignals]);

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
      // 409 — месяц карточки закрылся, пока экран был открыт (5-е число): выборка заново,
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
      // Сохранено — форма закрывается: счётчик «Обратная связь: N» рядом подтверждает отзыв.
      closeFeedback(signal.id);
      setSignals((current) =>
        current.map((item) =>
          item.id === signal.id ? { ...item, feedback_count: (item.feedback_count || 0) + 1 } : item,
        ),
      );
      showToast(
        result.merged
          ? "Отзыв сохранён: карточка скрыта как дубль, её ссылки — в главной карточке"
          : result.merge_skipped
            ? "Отзыв сохранён, карточка не скрыта: она уже скрыта, выбрана в дайджест или главной с таким номером нет"
            : "Отзыв сохранён — агент учтёт его в следующем прогоне радара",
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
  // «N из M сигналов»: N — в выборке по фильтрам, M — весь радар (оба числа — с сервера).
  const countBadge = searching
    ? "Обновляем выборку…"
    : summary
      ? `${summary.matching} из ${summary.total} ${signalsAfterFrom(summary.total)}` +
        (remaining > 0 ? ` · показаны ${visibleSignals.length}` : "")
      : "";

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Технологический радар</h1>
        </div>
      </header>

      {showHidden ? (
        <div className="archiveNotice" role="status">
          <span>
            Отсеянные и скрытые: всё, что радар не пустил на экран, — отсеянное поиском и судьёй, бизнес-сигналы,
            не технологические события, не про нефтесервис, ссылки о разных событиях, старше срока, архив.
            Причина — в строке карточки.
          </span>
        </div>
      ) : null}

      {searchNotice ? (
        // Стиль спокойного уведомления экранов ленты и выпуска (не красный).
        <div className="archiveNotice" role="status">
          <span>{searchNotice}</span>
        </div>
      ) : null}

      {/* Плитки, как у «Бизнес-сигналов» (документ заказчика 19.09), — по всему радару:
          поиск и фильтры сужают список, но не эти числа. */}
      {summary ? (
        <section className="statsGridReact" aria-label="Сводка радара">
          <StatCard label="Всего сигналов" value={summary.total} />
          <StatCard label="Новые за 7 дней" value={summary.new_7d} />
          <StatCard label="В дайджесте" value={summary.in_digest} />
          <StatCard label="С обратной связью" value={summary.with_feedback} />
          <StatCard label="Объединено дублей" value={summary.merged} />
          {summary.hidden != null ? <StatCard label="Отсеяно и скрыто" value={summary.hidden} /> : null}
        </section>
      ) : null}

      {/* Раскладка — как у «Бизнес-сигналов» (документ заказчика 19.09): действия в шапке
          панели, поиск и тема первой строкой, зрелость и сортировка второй, остальное —
          в «Расширенных фильтрах». */}
      <section className="panel">
        <div className="panelHeader">
          <h2>{showHidden ? "Отсеянные и скрытые карточки" : "Каталог технологических сигналов"}</h2>
          <div className="settingsActions signalRadarPanelActions">
            {countBadge ? <span className="badge">{countBadge}</span> : null}
            <button
              type="button"
              className={showHidden ? "primaryButton" : "ghostButton"}
              aria-pressed={showHidden}
              onClick={() => setShowHidden((current) => !current)}
            >
              {showHidden ? "К радару" : `Отсеянные и скрытые${summary?.hidden != null ? ` (${summary.hidden})` : ""}`}
            </button>
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

        <div className="articlesFiltersRow">
          <label className="field">
            <span>Поиск</span>
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Поиск по радару: название, суть, источник, #номер"
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
        </div>

        <div className="articlesFiltersRow signalRadarFiltersSecondary">
          <label className="field">
            <span>Зрелость</span>
            <select value={maturity} onChange={(event) => setMaturity(event.target.value)}>
              <option value="">Любая зрелость</option>
              <option value="watch">Наблюдать</option>
              <option value="shortlist">Кандидат</option>
              <option value="proven">Подтверждено</option>
              <option value="reject">Отклонено</option>
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
        </div>

        <div className="advancedToggleRow">
          <button type="button" className="ghostButton" onClick={() => setShowAdvancedFilters((current) => !current)}>
            {showAdvancedFilters ? "Скрыть расширенные фильтры" : "Расширенные фильтры"}
          </button>
        </div>

        {showAdvancedFilters ? (
          <div className="articlesAdvancedGrid">
            <label className="field">
              <span>Балл от</span>
              <input
                type="number"
                min={SCORE_MIN}
                max={SCORE_MAX}
                value={scoreMin}
                onChange={(event) => setScoreMin(clampScore(event.target.value, SCORE_MIN))}
              />
            </label>
            <label className="field">
              <span>Балл до</span>
              <input
                type="number"
                min={SCORE_MIN}
                max={SCORE_MAX}
                value={scoreMax}
                onChange={(event) => setScoreMax(clampScore(event.target.value, SCORE_MAX))}
              />
            </label>
            <label className="field">
              <span>Поступил с</span>
              <input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} />
            </label>
            <label className="field">
              <span>Поступил по</span>
              <input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} />
            </label>
            <div className="field">
              <span>&nbsp;</span>
              <button type="button" className="ghostButton" onClick={resetFilters}>
                Сбросить
              </button>
            </div>
          </div>
        ) : null}

        {!loaded ? (
          <div className="emptyState">Загружаем сигналы...</div>
        ) : visibleSignals.length ? (
          <div className="articleGroupsStack">
          {groups.map(([group, groupSignals]) => {
            // Свёрнуто по умолчанию, как в «Бизнес-сигналах»; раскрыто при поиске, выбранной
            // теме или если блок один — сворачивать нечего.
            const forcedOpen = Boolean(search.trim()) || theme === group || groups.length === 1;
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
            const eventDate = formatEventDate(signal.event_date);
            const breakdown = scoreBreakdown(signal);
            const tone = scoreTone(signal);
            // Месяц карточки закрыт (признак сервера): отметку «в дайджест» не ставят и не снимают.
            const digestLocked = Boolean(signal.digest_locked);
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
                      {cardMeta(signal)}
                      {/* Номер виден всегда: без него не сослаться на дубль в поле «ID дубля». */}
                      <span className="signalIdText">#{signal.id}</span>
                      {/* Дубли того же события скрыты, их ссылки — в этой карточке. */}
                      {Number(signal.merged_count || 0) > 0 ? ` · объединено дублей: ${signal.merged_count}` : ""}
                    </div>
                    {signal.hidden_reason ? (
                      <div className="metaText signalHiddenReason">Не на радаре: {signal.hidden_reason}</div>
                    ) : null}
                  </div>
                  <div className="articleCardMetrics">
                    {eventDate ? <div className="articleMetric">Событие: {eventDate}</div> : null}
                    {arrival ? <div className="articleMetric">Поступил: {arrival}</div> : null}
                    <div
                      className={`miniPill ${tone}`}
                      title={breakdown.length ? "Балл по профилю «Технологический радар»" : "Балл судьи радара"}
                    >
                      {Math.round(Number(signal.score || 0))}
                    </div>
                    <div className={`miniPill ${tone}`}>{signal.score_label || "—"}</div>
                    <button
                      type="button"
                      className={`${signal.selected_for_digest ? "dangerButton" : "primaryButton"} compactButton signalDigestButton`}
                      disabled={savingThis || digestLocked}
                      title={digestLocked ? radarDigestLockedText(signal.digest_month ?? "") : undefined}
                      onClick={() => void setDigest(signal, !signal.selected_for_digest)}
                    >
                      {signal.selected_for_digest ? "Убрать" : "В дайджест"}
                    </button>
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
                        <div>
                          <span>Почему сейчас</span>
                          <p>{signal.why_now || "Нет объяснения"}</p>
                        </div>
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
                        <div>
                          <span>Переносимость</span>
                          <p>{signal.transferability || "Нет оценки"}</p>
                        </div>
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
                        {signal.why_interesting ? (
                          <div>
                            <span>
                              Почему интересно
                              {signal.interest_score != null ? ` · ${Math.round(Number(signal.interest_score))}` : ""}
                            </span>
                            <p>{signal.why_interesting}</p>
                          </div>
                        ) : null}
                      </div>
                    </div>

                    {signal.evidence?.length ? (
                      <div className="signalEvidenceList">
                        <div className="signalEvidenceHeading">Ссылки</div>
                        {signal.evidence.map((item) => (
                          <a className="signalEvidenceRow" href={item.source_url} target="_blank" rel="noreferrer" key={item.id}>
                            <span>{evidenceSource(item)}</span>
                            <strong>{item.title_ru || item.title}</strong>
                            {item.summary_ru || item.extracted_fact ? (
                              <small>{item.summary_ru || item.extracted_fact}</small>
                            ) : null}
                          </a>
                        ))}
                      </div>
                    ) : null}

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
                              {VERDICT_LABELS.map((item) => (
                                <option value={item.value} key={item.value || "empty"}>{item.label}</option>
                              ))}
                            </select>
                          </label>
                          <label className="field signalFeedbackDuplicate">
                            <span>ID дубля</span>
                            <input
                              inputMode="numeric"
                              value={draftOf(signal.id).duplicateOfSignalId}
                              onChange={(event) => updateFeedbackDraft(signal.id, { duplicateOfSignalId: event.target.value })}
                              placeholder="номер главной карточки, если это дубль"
                            />
                          </label>
                          <label className="field">
                            <span>Обоснование оценки</span>
                            <input
                              value={draftOf(signal.id).reason}
                              onChange={(event) => updateFeedbackDraft(signal.id, { reason: event.target.value })}
                              placeholder="чем обоснована оценка"
                            />
                          </label>
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
                          <label className="field">
                            <span>Рекомендации AI-агенту</span>
                            <textarea
                              value={draftOf(signal.id).comment}
                              onChange={(event) => updateFeedbackDraft(signal.id, { comment: event.target.value })}
                              placeholder="термины, поисковый угол, сильный источник..."
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
          <div className="emptyState">Сигналов по выбранным фильтрам нет.</div>
        )}
      </section>
    </section>
  );
}
