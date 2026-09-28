import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../../api/client";
import { createSignalFeedback, getSignalSearchHealth, listSignals, updateSignal } from "../../api/signals";
import type { SignalSearchHealth } from "../../api/signals";
import type { Signal, SignalFeedbackPayload } from "../../api/types";

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

// Дата поступления карточки (встреча 21.09): первая находка радаром, по Москве.
function formatArrival(signal: Signal): string {
  const value = signal.first_seen_at || signal.created_at;
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: RADAR_TIME_ZONE });
}

// Цвет среднего балла группы — те же пороги, что у групп «Бизнес-сигналов».
function scoreClass(score: number) {
  if (!score) return "muted";
  if (score >= 65) return "ok";
  if (score >= 40) return "warn";
  return "bad";
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
  const [busy, setBusy] = useState(false);
  const [maturity, setMaturity] = useState("");
  const [theme, setTheme] = useState("");
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [feedbackOpen, setFeedbackOpen] = useState<Set<number>>(new Set());
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  // Свёрнутые вручную — сильнее авто-раскрытия при поиске и выбранной теме.
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [feedbackDrafts, setFeedbackDrafts] = useState<Record<number, FeedbackDraft>>({});
  const [saving, setSaving] = useState<Record<number, boolean>>({});
  const [searchHealth, setSearchHealth] = useState<SignalSearchHealth | null>(null);

  useEffect(() => {
    void reload();
  }, []);

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

  async function reload() {
    try {
      setBusy(true);
      setSignals(await listSignals({ maturity: maturity || undefined, theme: theme || undefined, limit: 150, evidenceLimit: 5 }));
    } catch (error) {
      handleError(error, "Не удалось загрузить технологический радар");
    } finally {
      setBusy(false);
    }
  }

  // В фильтре — только тематики заказчика: ранние карточки со свободной темой — отдельным блоком.
  const themes = useMemo(
    () => [...new Set(signals.filter((signal) => signal.theme_is_topic !== false).map((signal) => signal.theme).filter(Boolean))].sort(),
    [signals],
  );
  const visibleSignals = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return signals;
    return signals.filter((signal) =>
      [
        signal.title_ru,
        signal.title,
        signal.theme,
        signal.summary,
        signal.thesis,
        signal.transferability,
        ...(signal.evidence || []).map((item) => `${item.title} ${item.title_ru || ""} ${item.publisher || ""}`),
      ].some((value) => String(value || "").toLowerCase().includes(q)),
    );
  }, [search, signals]);

  const groups = useMemo(() => {
    const byTheme = new Map<string, Signal[]>();
    for (const signal of visibleSignals) {
      // Ранние карточки (первая партия 13.09) — с темой свободным текстом: один блок, а не
      // десятки блоков по одной карточке. Тематику им даст разбор, а не догадка по словам.
      const key = signal.theme_is_topic === false ? EARLY_THEME_GROUP : signal.theme || "Без темы";
      byTheme.set(key, [...(byTheme.get(key) || []), signal]);
    }
    const best = (items: Signal[]) => Math.max(...items.map((item) => Number(item.score || 0)));
    return [...byTheme.entries()].sort(
      (a, b) =>
        Number(a[0] === EARLY_THEME_GROUP) - Number(b[0] === EARLY_THEME_GROUP) ||
        best(b[1]) - best(a[1]) ||
        a[0].localeCompare(b[0], "ru"),
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
    } catch (error) {
      handleError(error, "Не удалось обновить статус сигнала");
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
        void reload();
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

  function updateFeedbackDraft(signalId: number, patch: Partial<FeedbackDraft>) {
    setFeedbackDrafts((current) => ({
      ...current,
      [signalId]: { ...(current[signalId] || EMPTY_FEEDBACK_DRAFT), ...patch },
    }));
  }

  const digestCount = visibleSignals.filter((signal) => signal.selected_for_digest).length;
  const feedbackCount = visibleSignals.reduce((sum, signal) => sum + Number(signal.feedback_count || 0), 0);
  const searchNotice = isAdmin ? searchHealthNotice(searchHealth) : "";

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <div className="eyebrow">Signal Discovery</div>
          <h1>Технологический радар</h1>
        </div>
        <div className="signalRadarHeaderStats" aria-label="Сводка радара">
          <span><strong>{visibleSignals.length}</strong> сигналов</span>
          <span><strong>{digestCount}</strong> в дайджесте</span>
          <span>обратная связь: <strong>{feedbackCount}</strong></span>
        </div>
      </header>

      {searchNotice ? (
        // Стиль спокойного уведомления экранов ленты и выпуска (не красный).
        <div className="archiveNotice" role="status">
          <span>{searchNotice}</span>
        </div>
      ) : null}

      <section className="signalRadarToolbar">
        <label>
          <span>Поиск</span>
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="ZEUS IQ, бурение, робот..." />
        </label>
        <label>
          <span>Зрелость</span>
          <select value={maturity} onChange={(event) => setMaturity(event.target.value)}>
            <option value="">Все</option>
            <option value="watch">Наблюдать</option>
            <option value="shortlist">Кандидат</option>
            <option value="proven">Подтверждено</option>
            <option value="reject">Отклонено</option>
          </select>
        </label>
        <label>
          <span>Тема</span>
          <select value={theme} onChange={(event) => setTheme(event.target.value)}>
            <option value="">Все темы</option>
            {themes.map((item) => <option value={item} key={item}>{item}</option>)}
          </select>
        </label>
        <button type="button" className="ghostButton" disabled={busy} onClick={() => void reload()}>
          {busy ? "Обновляем" : "Обновить"}
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
      </section>

      <section className="signalRadarList">
        {busy ? (
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
              {groupOpen ? groupSignals.map((signal) => {
            const isExpanded = expanded.has(signal.id);
            const isFeedbackOpen = feedbackOpen.has(signal.id);
            const savingThis = Boolean(saving[signal.id]);
            return (
              <article className="signalRadarCard" key={signal.id}>
                <div className="signalRadarCardTop">
                  <div>
                    <div className="signalRadarMeta">
                      {/* ID виден всегда: без него нельзя сослаться на дубль
                          в поле «ID дубля» — заказчик спрашивал, где его взять. */}
                      <span className="signalIdBadge">#{signal.id}</span>
                      <span className="signalTheme">{signal.theme}</span>
                      <span>Зрелость: {MATURITY_LABELS[signal.maturity] || signal.maturity}</span>
                      <span>{Math.round(Number(signal.score || 0))} баллов</span>
                      <span>{signal.evidence_count} ссылок</span>
                      {formatArrival(signal) ? <span>Поступил: {formatArrival(signal)}</span> : null}
                      {/* Дубли того же события скрыты, их ссылки — в этой карточке. */}
                      {Number(signal.merged_count || 0) > 0 ? (
                        <span>Объединено дублей: {signal.merged_count}</span>
                      ) : null}
                    </div>
                    <h2>{signal.title_ru || signal.title}</h2>
                  </div>
                  <div className="signalRadarActions">
                    <button type="button" className="ghostButton compactButton" onClick={() => toggleExpanded(signal.id)}>
                      {isExpanded ? "Скрыть" : "Ссылки"}
                    </button>
                    <button type="button" className="ghostButton compactButton" onClick={() => toggleFeedback(signal.id)}>
                      Обратная связь
                    </button>
                    <button
                      type="button"
                      className={signal.selected_for_digest ? "dangerButton compactButton" : "primaryButton compactButton"}
                      disabled={savingThis}
                      onClick={() => void setDigest(signal, !signal.selected_for_digest)}
                    >
                      {signal.selected_for_digest ? "Убрать" : "В дайджест"}
                    </button>
                  </div>
                </div>

                <div className="signalRadarBody">
                  <p className="signalRadarSummaryText">{signal.summary || signal.thesis || "Суть сигнала ещё не сформирована."}</p>
                  <div className="signalRadarFacts">
                    <div>
                      <span>Почему сейчас</span>
                      <p>{signal.why_now || "Нет объяснения"}</p>
                    </div>
                    <div>
                      <span>Переносимость</span>
                      <p>{signal.transferability || "Нет оценки"}</p>
                    </div>
                    {/* Сравнение внутри пачки прогона, а не абсолютная оценка судьи:
                        поэтому рядом с баллами, но порядок списка — по баллам. */}
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

                {isExpanded ? (
                  <div className="signalEvidenceList">
                    {(signal.evidence || []).map((item) => (
                      <a className="signalEvidenceRow" href={item.source_url} target="_blank" rel="noreferrer" key={item.id}>
                        <span>{item.publisher || "source"}</span>
                        <strong>{item.title_ru || item.title}</strong>
                        <small>{item.summary_ru || item.extracted_fact || item.evidence_type}</small>
                      </a>
                    ))}
                  </div>
                ) : null}

                {isFeedbackOpen ? (
                  <div className="signalFeedbackBox">
                    <div className="signalFeedbackGrid">
                      <label>
                        <span>Оценка сигнала</span>
                        <select
                          value={(feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT).verdict}
                          onChange={(event) =>
                            updateFeedbackDraft(signal.id, { verdict: event.target.value as FeedbackDraft["verdict"] })
                          }
                        >
                          {VERDICT_LABELS.map((item) => (
                            <option value={item.value} key={item.value || "empty"}>{item.label}</option>
                          ))}
                        </select>
                      </label>
                      <label>
                        <span>ID дубля</span>
                        <input
                          inputMode="numeric"
                          value={(feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT).duplicateOfSignalId}
                          onChange={(event) => updateFeedbackDraft(signal.id, { duplicateOfSignalId: event.target.value })}
                          placeholder="если это дубль"
                        />
                      </label>
                    </div>
                    <label className="signalFeedbackField">
                      <span>Обоснование оценки</span>
                      <input
                        value={(feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT).reason}
                        onChange={(event) => updateFeedbackDraft(signal.id, { reason: event.target.value })}
                        placeholder="чем обоснована оценка"
                      />
                    </label>
                    <label className="signalFeedbackField">
                      <span>Рекомендуемый заголовок</span>
                      <input
                        value={(feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT).correctedTitle}
                        onChange={(event) => updateFeedbackDraft(signal.id, { correctedTitle: event.target.value })}
                        placeholder="если нужно переименовать карточку"
                      />
                    </label>
                    <label className="signalFeedbackField">
                      <span>Рекомендуемая формулировка сути</span>
                      <textarea
                        value={(feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT).correctedThesis}
                        onChange={(event) => updateFeedbackDraft(signal.id, { correctedThesis: event.target.value })}
                        placeholder="эталонная формулировка сути сигнала"
                      />
                    </label>
                    <label className="signalFeedbackField">
                      <span>Рекомендации AI-агенту</span>
                      <textarea
                        value={(feedbackDrafts[signal.id] || EMPTY_FEEDBACK_DRAFT).comment}
                        onChange={(event) => updateFeedbackDraft(signal.id, { comment: event.target.value })}
                        placeholder="термины, поисковый угол, сильный источник..."
                      />
                    </label>
                    <div className="signalFeedbackActions">
                      <span>Обратная связь: {signal.feedback_count || 0}</span>
                      <button type="button" className="primaryButton compactButton" disabled={savingThis} onClick={() => void submitFeedback(signal)}>
                        Сохранить
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="signalFeedbackCollapsed">
                    <span>Обратная связь: {signal.feedback_count || 0}</span>
                    <button type="button" className="ghostButton compactButton" onClick={() => toggleFeedback(signal.id)}>
                      Обратная связь
                    </button>
                  </div>
                )}
              </article>
            );
          }) : null}
            </section>
            );
          })}
          </div>
        ) : (
          <div className="emptyState">Сигналов по выбранным фильтрам нет.</div>
        )}
      </section>
    </section>
  );
}
