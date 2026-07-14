import { useEffect, useMemo, useState } from "react";
import { DEFAULT_ARTICLE_LIMIT, listArticles, type ArticleQuery, updateArticle } from "../../api/articles";
import { getDashboardStats } from "../../api/stats";
import type { Article, DashboardStats } from "../../api/types";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
  initialArticles: Article[];
  initialStats: DashboardStats | null;
  onArticlesReloaded: (articles: Article[]) => void;
  onStatsReloaded: (stats: DashboardStats) => void;
};

export const DEFAULT_SIGNAL_SCORE_MIN = 50;
export const DEFAULT_SIGNAL_SCORE_MAX = 100;
export const DEFAULT_SIGNAL_SORT: NonNullable<ArticleQuery["sort"]> = "score_desc";
export const DEFAULT_SIGNAL_ARTICLE_QUERY: ArticleQuery = {
  limit: DEFAULT_ARTICLE_LIMIT,
  minScore: DEFAULT_SIGNAL_SCORE_MIN,
  maxScore: DEFAULT_SIGNAL_SCORE_MAX,
  sort: DEFAULT_SIGNAL_SORT,
};

const STATUSES: Array<Article["status"]> = ["new", "review", "digest", "archive", "noise", "duplicate"];
const STATUS_LABELS: Record<Article["status"], string> = {
  new: "Новая",
  review: "На проверке",
  digest: "В дайджест",
  archive: "Архив",
  noise: "Шум",
  duplicate: "Дубликат",
};

// Интервал фонового обновления ленты. Консервативно: /api/articles и /api/stats —
// тяжёлые запросы, а прод-сервер слабый. 40с дают «живость» без лишней нагрузки.
const AUTO_REFRESH_MS = 40000;

export function ArticlesPage(props: Props) {
  const { initialArticles, initialStats, onArticlesReloaded, onStatsReloaded } = props;
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [search, setSearch] = useState("");
  const [tag, setTag] = useState("");
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("");
  const [language, setLanguage] = useState("");
  const [scoreMin, setScoreMin] = useState(DEFAULT_SIGNAL_SCORE_MIN);
  const [scoreMax, setScoreMax] = useState(DEFAULT_SIGNAL_SCORE_MAX);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [sort, setSort] = useState<NonNullable<ArticleQuery["sort"]>>(DEFAULT_SIGNAL_SORT);
  const [tagQuery, setTagQuery] = useState("");
  const [showTagOptions, setShowTagOptions] = useState(false);
  const [sourceQuery, setSourceQuery] = useState("");
  const [showSourceOptions, setShowSourceOptions] = useState(false);
  const [showAdvancedFilters, setShowAdvancedFilters] = useState(false);
  const [renderLimit, setRenderLimit] = useState(200);
  const [serverResults, setServerResults] = useState<Article[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [viewTab, setViewTab] = useState<"all" | "withStatus">("all");
  // #9: группы-теги свёрнуты по умолчанию. Храним РАСКРЫТЫЕ (пустой набор = всё скрыто).
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());

  // serverResults != null → активен серверный поиск по всей базе; иначе — дефолтный топ-2000.
  const articles = serverResults ?? initialArticles;
  const stats = initialStats;
  // Вкладка «Со статусом»: статьи, у которых статус сменили (review/digest/archive/noise/duplicate).
  const statusChangedCount = articles.filter((article) => article.status !== "new").length;

  function handleError(error: unknown, fallback: string) {
    const statusCode = typeof error === "object" && error && "status" in error ? Number(error.status) : 0;
    const message = error instanceof Error ? error.message : fallback;
    if (statusCode === 401) {
      props.onUnauthorized();
      return;
    }
    props.showToast(message || fallback, "error");
  }

  async function reload() {
    try {
      setBusy(true);
      await refreshCatalog({ keepQuery: true });
    } catch (error) {
      handleError(error, "Не удалось обновить статьи");
    } finally {
      setBusy(false);
    }
  }

  const topTags = useMemo(() => {
    const names = [...new Set(articles.map((article) => article.tag).filter(Boolean))].sort();
    return [...new Set(names.map((name) => name.split(" / ")[0]))];
  }, [articles]);
  const filteredTagOptions = useMemo(() => {
    const q = tagQuery.trim().toLowerCase();
    return topTags.filter((option) => !q || option.toLowerCase().includes(q));
  }, [tagQuery, topTags]);

  const sourceOptions = useMemo(() => [...new Set(articles.map((article) => article.source).filter(Boolean))].sort(), [articles]);
  const filteredSourceOptions = useMemo(() => {
    const q = sourceQuery.trim().toLowerCase();
    return sourceOptions.filter((option) => !q || option.toLowerCase().includes(q));
  }, [sourceOptions, sourceQuery]);
  const hasServerQuery =
    Boolean(search.trim())
    || Boolean(tag)
    || Boolean(status)
    || Boolean(source)
    || Boolean(language)
    || scoreMin !== DEFAULT_SIGNAL_SCORE_MIN
    || scoreMax !== DEFAULT_SIGNAL_SCORE_MAX
    || Boolean(dateFrom)
    || Boolean(dateTo)
    || sort !== DEFAULT_SIGNAL_SORT
    || viewTab === "withStatus";
  // ВАЖНО: только useMemo, иначе объект пересоздаётся на каждом рендере. Он стоит в deps
  // ДВУХ эффектов ниже (серверный поиск + 40с-автообновление), а эффект поиска сам зовёт
  // setServerResults/setSearching → рендер → новая идентичность объекта → эффект снова →
  // бесконечный поток запросов /api/articles каждые ~400мс при ЛЮБОМ активном фильтре
  // (и 40с-таймер при этом пересоздавался, так и не успевая сработать).
  const activeServerQuery: ArticleQuery | null = useMemo(
    () =>
      hasServerQuery
        ? {
            search: search.trim() || undefined,
            tag: tag || undefined,
            status: status || undefined,
            source: source || undefined,
            language: language || undefined,
            minScore: scoreMin,
            maxScore: scoreMax,
            dateFrom: dateFrom || undefined,
            dateTo: dateTo || undefined,
            sort: sort as ArticleQuery["sort"],
            changedOnly: viewTab === "withStatus",
            limit: 5000,
          }
        : null,
    [hasServerQuery, search, tag, status, source, language, scoreMin, scoreMax, dateFrom, dateTo, sort, viewTab],
  );

  async function refreshCatalog(options: { silent?: boolean; keepQuery?: boolean } = {}) {
    const query = options.keepQuery ? activeServerQuery : null;
    const [articlesPayload, statsPayload] = await Promise.all([
      listArticles(query ?? DEFAULT_SIGNAL_ARTICLE_QUERY),
      getDashboardStats(),
    ]);
    if (query) {
      setServerResults(articlesPayload);
    } else {
      setServerResults(null);
      onArticlesReloaded(articlesPayload);
    }
    onStatsReloaded(statsPayload);
    if (!options.silent) {
      props.showToast("Данные обновлены");
    }
  }

  useEffect(() => {
    setRenderLimit(200);
  }, [dateFrom, dateTo, language, scoreMax, scoreMin, search, sort, source, status, tag, viewTab]);

  // Реалтайм без перезагрузки: тихо подтягиваем свежие сигналы и счётчики.
  // Без тостов; пауза в фоновой вкладке, во время загрузки и при серверном поиске
  // (его не перетираем). При возврате на вкладку — обновляем сразу.
  useEffect(() => {
    let inFlight = false;
    async function refresh() {
      if (inFlight || document.hidden || busy || searching) return;
      inFlight = true;
      try {
        await refreshCatalog({ silent: true, keepQuery: true });
      } catch {
        // Фоновое обновление молчит об ошибках, чтобы не мешать работе.
      } finally {
        inFlight = false;
      }
    }
    const timer = window.setInterval(() => void refresh(), AUTO_REFRESH_MS);
    function onVisible() {
      if (!document.hidden) void refresh();
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [activeServerQuery, busy, searching, onArticlesReloaded, onStatsReloaded]);

  // Фильтры каталога работают по серверной выборке на ВСЮ базу, а не только по
  // локальному топ-2000. Пустые фильтры → возвращаемся к дефолтному набору.
  useEffect(() => {
    if (!hasServerQuery) {
      setServerResults(null);
      setSearching(false);
      return;
    }
    let cancelled = false;
    setSearching(true);
    const timer = window.setTimeout(() => {
      listArticles(activeServerQuery || { ...DEFAULT_SIGNAL_ARTICLE_QUERY, limit: 5000 })
        .then((rows) => {
          if (!cancelled) setServerResults(rows);
        })
        .catch((error) => {
          if (!cancelled) handleError(error, "Не удалось обновить выборку по фильтрам");
        })
        .finally(() => {
          if (!cancelled) setSearching(false);
        });
    }, 400);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [activeServerQuery, hasServerQuery]);

  const filteredArticles = useMemo(() => {
    const items = articles.filter((article) => {
      const hay = [article.title, article.summary, article.source, article.tag].join(" ").toLowerCase();
      return (
        (
          serverResults !== null
            ? true
            : (
              (viewTab === "all" || article.status !== "new")
              &&
              (!search.trim() || hay.includes(search.trim().toLowerCase()))
              && (!tag || article.tag === tag || article.tag.startsWith(`${tag} /`))
              && (!status || article.status === status)
              && (!source || article.source === source)
              && Number(article.score || 0) >= scoreMin
              && Number(article.score || 0) <= scoreMax
              && (!dateFrom || String(article.date || "") >= dateFrom)
              && (!dateTo || String(article.date || "") <= dateTo)
              && (!language || article.language === language)
            )
        )
      );
    });

    if (serverResults !== null) {
      return items;
    }

    items.sort((a, b) => {
      if (sort === "score_asc") return Number(a.score || 0) - Number(b.score || 0);
      if (sort === "date_desc") return String(b.date || "").localeCompare(String(a.date || ""));
      return Number(b.score || 0) - Number(a.score || 0);
    });
    return items;
  }, [articles, dateFrom, dateTo, language, scoreMax, scoreMin, search, serverResults, sort, source, status, tag, viewTab]);

  const visibleArticles = filteredArticles.slice(0, renderLimit);
  const remaining = filteredArticles.length - visibleArticles.length;

  const grouped = useMemo(() => {
    const map = new Map<string, Article[]>();
    visibleArticles.forEach((article) => {
      const top = (article.tag || "Без тега").split(" / ")[0];
      if (!map.has(top)) map.set(top, []);
      map.get(top)!.push(article);
    });
    return [...map.entries()];
  }, [visibleArticles]);

  const dashboardCards = useMemo(() => {
    const total = stats?.total_articles ?? articles.length;
    const newCount = articles.filter((item) => item.status === "new").length;
    const reviewCount = articles.filter((item) => item.status === "review").length;
    const digestCount = stats?.selected_for_digest ?? articles.filter((item) => item.digest).length;
    const noiseCount = articles.filter((item) => item.status === "noise").length;
    const duplicateCount = articles.filter((item) => item.status === "duplicate").length;
    const processedFallback = articles.filter((item) => {
      const hasSummary = Boolean(item.summary);
      const hasRelevance = item.relevant !== null;
      const hasDownstreamResult =
        item.relevant === false || Boolean((item.score_items && item.score_items.length) || item.score_explanation || item.tag_rationale);
      return hasSummary && hasRelevance && hasDownstreamResult;
    }).length;
    const processedCount = stats?.processed_articles ?? processedFallback;

    // Порядок: общие по базе (всего, обработано) → затем по пользователю (его статусы).
    return [
      { label: "Всего сигналов", value: total },
      { label: "Обработано", value: processedCount },
      { label: "Новые", value: newCount },
      { label: "На проверке", value: reviewCount },
      { label: "В дайджест", value: digestCount },
      { label: "Шум", value: noiseCount },
      { label: "Дубликаты", value: duplicateCount },
    ];
  }, [articles, stats]);

  function resetFilters() {
    setSearch("");
    setTag("");
    setTagQuery("");
    setShowTagOptions(false);
    setStatus("");
    setSource("");
    setSourceQuery("");
    setShowSourceOptions(false);
    setLanguage("");
    setScoreMin(DEFAULT_SIGNAL_SCORE_MIN);
    setScoreMax(DEFAULT_SIGNAL_SCORE_MAX);
    setDateFrom("");
    setDateTo("");
    setSort(DEFAULT_SIGNAL_SORT);
    setRenderLimit(200);
  }

  function toggleExpand(articleId: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(articleId)) next.delete(articleId);
      else next.add(articleId);
      return next;
    });
  }

  function toggleGroup(group: string) {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(group)) next.delete(group);
      else next.add(group);
      return next;
    });
  }

  async function handleStatusChange(articleId: number, nextStatus: Article["status"]) {
    try {
      setBusy(true);
      await updateArticle(articleId, { status: nextStatus });
      await refreshCatalog({ silent: true, keepQuery: true });
      props.showToast("Статус статьи обновлён");
    } catch (error) {
      handleError(error, "Не удалось обновить статус статьи");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Сигналы</h1>
        </div>
      </header>

      <section className="statsGridReact">
        {dashboardCards.map((card) => (
          <StatCard key={card.label} label={card.label} value={card.value} />
        ))}
      </section>

      <div className="viewTabs">
        <button type="button" className={viewTab === "all" ? "primaryButton" : "ghostButton"} onClick={() => setViewTab("all")}>
          Все
        </button>
        <button type="button" className={viewTab === "withStatus" ? "primaryButton" : "ghostButton"} onClick={() => setViewTab("withStatus")}>
          Со статусом{statusChangedCount > 0 ? ` (${statusChangedCount})` : ""}
        </button>
      </div>

      <section className="panel">
        {busy ? <InlineLoader label="Обновляем сигналы…" /> : null}
        <div className="panelHeader">
          <h2>Каталог сигналов</h2>
          <div className="settingsActions">
            <span className="badge">
              {searching
                ? "Обновляем выборку по всей базе…"
                : serverResults !== null
                  ? `Выборка по всей базе: ${filteredArticles.length}`
                  : remaining > 0
                    ? `${filteredArticles.length} сигналов · показаны ${visibleArticles.length}`
                    : `${filteredArticles.length} сигналов`}
            </span>
            {grouped.length ? (
              <>
                <button type="button" className="ghostButton" onClick={() => setExpandedGroups(new Set(grouped.map(([group]) => group)))}>
                  Развернуть всё
                </button>
                <button type="button" className="ghostButton" onClick={() => setExpandedGroups(new Set())}>
                  Свернуть всё
                </button>
              </>
            ) : null}
            <button type="button" className="ghostButton" onClick={() => void reload()}>
              Обновить
            </button>
          </div>
        </div>

        <div className="articlesFiltersRow">
          <label className="field">
            <span>Поиск</span>
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Поиск по всей базе: название, текст, суть" />
          </label>
          <div className="field sourceComboReact">
            <span>Тег</span>
            <input
              value={tag || tagQuery}
              onChange={(event) => {
                setTag("");
                setTagQuery(event.target.value);
                setShowTagOptions(true);
              }}
              onFocus={() => setShowTagOptions(true)}
              onBlur={() => {
                window.setTimeout(() => setShowTagOptions(false), 120);
              }}
              placeholder="Все теги"
            />
            {showTagOptions ? (
              <div className="sourcePopoverReact">
                <button
                  type="button"
                  className="sourceOptionReact"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    setTag("");
                    setTagQuery("");
                    setShowTagOptions(false);
                  }}
                >
                  <span>Все теги</span>
                  <span className="metaText">сброс</span>
                </button>
                {filteredTagOptions.map((option) => (
                  <button
                    key={option}
                    type="button"
                    className="sourceOptionReact"
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => {
                      setTag(option);
                      setTagQuery(option);
                      setShowTagOptions(false);
                    }}
                  >
                    <span>{option}</span>
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </div>

        <div className="articlesFiltersRow articlesFiltersRowSecondary">
          <div className="field sourceComboReact">
            <span>Источник</span>
            <input
              value={source || sourceQuery}
              onChange={(event) => {
                setSource("");
                setSourceQuery(event.target.value);
                setShowSourceOptions(true);
              }}
              onFocus={() => setShowSourceOptions(true)}
              onBlur={() => {
                window.setTimeout(() => setShowSourceOptions(false), 120);
              }}
              placeholder="Начните вводить источник"
            />
            {showSourceOptions ? (
              <div className="sourcePopoverReact">
                <button
                  type="button"
                  className="sourceOptionReact"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    setSource("");
                    setSourceQuery("");
                    setShowSourceOptions(false);
                  }}
                >
                  <span>Все источники</span>
                  <span className="metaText">сброс</span>
                </button>
                {filteredSourceOptions.map((option) => (
                  <button
                    key={option}
                    type="button"
                    className="sourceOptionReact"
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => {
                      setSource(option);
                      setSourceQuery(option);
                      setShowSourceOptions(false);
                    }}
                  >
                    <span>{option}</span>
                  </button>
                ))}
              </div>
            ) : null}
          </div>
          <label className="field">
            <span>Статус</span>
            <select value={status} onChange={(event) => setStatus(event.target.value)}>
              <option value="">Любой статус</option>
              {STATUSES.map((option) => (
                <option key={option} value={option}>
                  {STATUS_LABELS[option]}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Сортировка</span>
            <select value={sort} onChange={(event) => setSort(event.target.value as NonNullable<ArticleQuery["sort"]>)}>
              <option value="date_desc">Сначала новые</option>
              <option value="score_desc">Оценка: по убыванию</option>
              <option value="score_asc">Оценка: по возрастанию</option>
            </select>
          </label>
        </div>

        <div className="advancedToggleRow">
          <button type="button" className="ghostButton" onClick={() => setShowAdvancedFilters((prev) => !prev)}>
            {showAdvancedFilters ? "Скрыть расширенные фильтры" : "Расширенные фильтры"}
          </button>
        </div>

        {showAdvancedFilters ? (
          <div className="articlesAdvancedGrid">
            <label className="field">
              <span>Оценка от</span>
              <input type="number" value={scoreMin} onChange={(event) => setScoreMin(Number(event.target.value || 0))} />
            </label>
            <label className="field">
              <span>Оценка до</span>
              <input type="number" value={scoreMax} onChange={(event) => setScoreMax(Number(event.target.value || 100))} />
            </label>
            <label className="field">
              <span>Дата от</span>
              <input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} />
            </label>
            <label className="field">
              <span>Дата до</span>
              <input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} />
            </label>
            <label className="field">
              <span>Язык</span>
              <select value={language} onChange={(event) => setLanguage(event.target.value)}>
                <option value="">Любой</option>
                <option value="ru">ru</option>
                <option value="en">en</option>
              </select>
            </label>
            <div className="field">
              <span>&nbsp;</span>
              <button type="button" className="ghostButton" onClick={resetFilters}>
                Сбросить
              </button>
            </div>
          </div>
        ) : null}

        {visibleArticles.length ? (
          <div className="articleGroupsStack">
            {grouped.map(([group, groupArticles]) => {
              // Свёрнуто по умолчанию; авто-раскрытие при активном поиске или выбранном этом теге.
              const groupOpen = expandedGroups.has(group) || Boolean(search.trim()) || tag === group;
              const groupAvg = Math.round(groupArticles.reduce((sum, article) => sum + Number(article.score || 0), 0) / groupArticles.length);
              return (
              <section className="articleGroupCard" key={group}>
                <button
                  type="button"
                  className={groupOpen ? "articleGroupHead articleGroupToggle open" : "articleGroupHead articleGroupToggle"}
                  onClick={() => toggleGroup(group)}
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
                    <span className="metaText">{groupArticles.length} сигналов · средняя</span>
                    <span className={`miniPill ${scoreClass(groupAvg)}`}>{groupAvg}</span>
                  </span>
                </button>
                {groupOpen ? (
                <div className="articleRows">
                  {groupArticles.map((article) => {
                    const open = expanded.has(article.id);
                    return (
                      <article className="articleCardReact" key={article.id}>
                        <div className="articleCardTop">
                          <button
                            type="button"
                            className={open ? "expandButtonReact open" : "expandButtonReact"}
                            onClick={() => toggleExpand(article.id)}
                            aria-label={open ? "Свернуть сигнал" : "Раскрыть сигнал"}
                          >
                            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8">
                              <path d="M4 6.5 8 10l4-3.5" strokeLinecap="round" strokeLinejoin="round" />
                            </svg>
                          </button>
                          <div className="articleCardMain">
                            <a href={article.url} target="_blank" rel="noreferrer" className="articleTitleReact">
                              {article.title}
                            </a>
                            <div className="metaText">
                              {article.source} · {article.tag} · {article.language || "язык не определён"} · {article.raw_text_chars || 0} симв.
                              {article.digest ? " · в дайджесте" : ""}
                            </div>
                            {article.relevant === false || article.text_truncated || article.future_date ? (
                              <div className="articleFlags">
                                {article.relevant === false ? <span className="miniPill bad">нерелевантно</span> : null}
                                {article.text_truncated ? <span className="miniPill warn">неполный текст</span> : null}
                                {article.future_date ? <span className="miniPill warn">дата в будущем</span> : null}
                              </div>
                            ) : null}
                          </div>
                          <div className="articleCardMetrics">
                            <div className="articleMetric">{formatDate(article.collected || article.date)}</div>
                            <div className={`miniPill ${ratingClass(article.rating)}`} title="Итоговая оценка релевантности">
                              {Math.round(article.score || 0)}
                            </div>
                            <div className={`miniPill ${ratingClass(article.rating)}`}>{article.rating || "—"}</div>
                            <label className="field">
                              <span>Статус</span>
                              <select value={article.status} onChange={(event) => void handleStatusChange(article.id, event.target.value as Article["status"])}>
                                {STATUSES.map((option) => (
                                  <option key={option} value={option}>
                                    {STATUS_LABELS[option]}
                                  </option>
                                ))}
                              </select>
                            </label>
                          </div>
                        </div>
                        {open ? (
                          <div className="articleDetailReact">
                            <div className="articleDetailGrid">
                              <div className="articleSummaryBox">
                                <strong>Суть</strong>
                                <p>{article.summary || "AI-суть ещё не сформирована."}</p>
                                <div className="metaText">
                                  {article.relevant === false
                                    ? `Отклонено фильтром релевантности: ${article.relevance_reason || ""}`
                                    : article.score_explanation || article.tag_rationale || "Оригинал открывается кликом по названию."}
                                </div>
                              </div>
                              <div>
                                <div className="criteriaScrollReact">
                                  {article.score_items?.length ? (
                                    article.score_items.map((item) => (
                                      <div className="criterionCardReact" key={item.name}>
                                        <div className="criterionTopReact">
                                          <span>{item.name}</span>
                                          <span className={`miniPill ${scoreClass(Number(item.final_score || 0))}`}>
                                            {Math.round(Number(item.final_score || 0))}/100
                                          </span>
                                        </div>
                                        <div className={`barReact ${scoreClass(Number(item.final_score || 0))}`}>
                                          <span style={{ width: `${Math.max(0, Math.min(100, Number(item.final_score || 0)))}%` }} />
                                        </div>
                                        {item.rationale ? <div className="metaText">{item.rationale}</div> : null}
                                      </div>
                                    ))
                                  ) : (
                                    <div className="criterionCardReact">
                                      <div className="metaText">Скоринг по критериям ещё не рассчитан.</div>
                                    </div>
                                  )}
                                </div>
                              </div>
                            </div>
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
                <button type="button" className="ghostButton" onClick={() => setRenderLimit((prev) => prev + 200)}>
                  Показать ещё {Math.min(200, remaining)} (осталось {remaining})
                </button>
              </div>
            ) : null}
          </div>
        ) : (
          <div className="emptyState">По текущим фильтрам ничего не найдено.</div>
        )}
      </section>
    </section>
  );
}

function StatCard(props: { label: string; value: number }) {
  return (
    <div className="statCardReact">
      <div className="statValueReact">{props.value}</div>
      <div className="metaText">{props.label}</div>
    </div>
  );
}

// Шкала цвета согласована с backend score_label (pipeline.py: пороги 80/65/40):
// «Высокая»/«Выше средней» (>=65) → зелёный, «Средняя» (>=40) → оранжевый,
// «Низкая» (<40) → красный, нет оценки → нейтральный серый.
function scoreClass(score: number) {
  if (!score) return "muted";
  if (score >= 65) return "ok";
  if (score >= 40) return "warn";
  return "bad";
}

// Текстовый рейтинг (score_label) красим в тот же тон, что и число, — чтобы
// метка и оценка совпадали по цвету.
function ratingClass(rating: string) {
  switch ((rating || "").trim()) {
    case "Высокая":
    case "Выше средней":
      return "ok";
    case "Средняя":
      return "warn";
    case "Низкая":
      return "bad";
    default:
      return "muted";
  }
}

function formatDate(value: string | null) {
  return value ? new Date(`${value}T00:00:00`).toLocaleDateString("ru-RU") : "—";
}

function InlineLoader(props: { label: string }) {
  return (
    <div className="loadingOverlay">
      <div className="spinnerReact" />
      <span>{props.label}</span>
    </div>
  );
}
