import { useEffect, useMemo, useState } from "react";
import { listArticles, updateArticle } from "../../api/articles";
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

const STATUSES: Array<Article["status"]> = ["new", "review", "digest", "archive"];
const STATUS_LABELS: Record<Article["status"], string> = {
  new: "Новая",
  review: "На проверке",
  digest: "В дайджест",
  archive: "Архив",
};

export function ArticlesPage(props: Props) {
  const { initialArticles, initialStats } = props;
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [search, setSearch] = useState("");
  const [tag, setTag] = useState("");
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("");
  const [language, setLanguage] = useState("");
  const [scoreMin, setScoreMin] = useState(0);
  const [scoreMax, setScoreMax] = useState(100);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [sort, setSort] = useState("date_desc");
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
  // Вкладка «Со статусом»: статьи, у которых статус сменили (review/digest/archive).
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
      const [articlesPayload, statsPayload] = await Promise.all([listArticles(), getDashboardStats()]);
      props.onArticlesReloaded(articlesPayload);
      props.onStatsReloaded(statsPayload);
      props.showToast("Данные обновлены");
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

  useEffect(() => {
    setRenderLimit(200);
  }, [dateFrom, dateTo, language, scoreMax, scoreMin, search, sort, source, status, tag, viewTab]);

  // Поиск уходит на сервер и покрывает ВСЮ базу (а не только загруженный топ-2000).
  // Пустой запрос → возвращаемся к дефолтному набору. Debounce 400мс.
  useEffect(() => {
    const q = search.trim();
    if (!q) {
      setServerResults(null);
      setSearching(false);
      return;
    }
    let cancelled = false;
    setSearching(true);
    const timer = window.setTimeout(() => {
      listArticles({ search: q, limit: 5000 })
        .then((rows) => {
          if (!cancelled) setServerResults(rows);
        })
        .catch((error) => {
          if (!cancelled) handleError(error, "Не удалось выполнить поиск");
        })
        .finally(() => {
          if (!cancelled) setSearching(false);
        });
    }, 400);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [search]);

  const filteredArticles = useMemo(() => {
    const q = search.trim().toLowerCase();
    const items = articles.filter((article) => {
      const hay = [article.title, article.summary, article.source, article.tag].join(" ").toLowerCase();
      return (
        // Вкладка «Со статусом» оставляет только статьи с изменённым статусом.
        (viewTab === "all" || article.status !== "new") &&
        // При серверном поиске текст уже отфильтрован в Postgres (включая raw_text,
        // которого нет на клиенте) — не режем результат повторно по hay.
        (serverResults !== null || !q || hay.includes(q)) &&
        (!tag || article.tag === tag || article.tag.startsWith(`${tag} /`)) &&
        (!status || article.status === status) &&
        (!source || article.source === source) &&
        Number(article.score || 0) >= scoreMin &&
        Number(article.score || 0) <= scoreMax &&
        (!dateFrom || String(article.date || "") >= dateFrom) &&
        (!dateTo || String(article.date || "") <= dateTo) &&
        (!language || article.language === language)
      );
    });

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
    const processedFallback = articles.filter((item) => {
      const hasSummary = Boolean(item.summary);
      const hasRelevance = item.relevant !== null;
      const hasDownstreamResult =
        item.relevant === false || Boolean((item.score_items && item.score_items.length) || item.score_explanation || item.tag_rationale);
      return hasSummary && hasRelevance && hasDownstreamResult;
    }).length;
    const processedCount = stats?.processed_articles ?? processedFallback;

    // Порядок: общие по базе (всего, обработано) → затем пер-юзерные (статусы текущего юзера).
    return [
      { label: "Всего сигналов", value: total },
      { label: "Обработано", value: processedCount },
      { label: "Новые", value: newCount },
      { label: "На проверке", value: reviewCount },
      { label: "В дайджест", value: digestCount },
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
    setScoreMin(0);
    setScoreMax(100);
    setDateFrom("");
    setDateTo("");
    setSort("date_desc");
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
      const [refreshedArticles, refreshedStats] = await Promise.all([listArticles(), getDashboardStats()]);
      props.onArticlesReloaded(refreshedArticles);
      props.onStatsReloaded(refreshedStats);
      // Держим в синхроне активный серверный поиск (его не перезагружаем целиком).
      setServerResults((prev) => (prev ? prev.map((item) => (item.id === articleId ? { ...item, status: nextStatus } : item)) : prev));
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

      <div style={{ display: "flex", gap: 8, marginBottom: 4 }}>
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
                ? "Поиск по всей базе…"
                : serverResults !== null
                  ? `Найдено по всей базе: ${filteredArticles.length}`
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
            <select value={sort} onChange={(event) => setSort(event.target.value)}>
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
                  <span className="metaText">
                    {groupArticles.length} сигналов · средняя оценка {Math.round(groupArticles.reduce((sum, article) => sum + Number(article.score || 0), 0) / groupArticles.length)}
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
                              {article.text_truncated ? " · неполный текст" : ""}
                              {article.relevant === false ? " · нерелевантно" : ""}
                              {article.digest ? " · в дайджесте" : ""}
                              {article.future_date ? ` · публикация ${article.published_at || ""} (в будущем)` : ""}
                            </div>
                          </div>
                          <div className="articleMetric">{formatDate(article.collected || article.date)}</div>
                          <div className={`miniPill ${scoreClass(article.score)}`}>{Math.round(article.score || 0)}</div>
                          <div className="miniPill ok">{article.rating || "—"}</div>
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
                                          <span>{Math.round(Number(item.final_score || 0))}/100</span>
                                        </div>
                                        <div className="barReact">
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

function scoreClass(score: number) {
  if (score >= 80) return "ok";
  if (score >= 60) return "warn";
  if (score < 25) return "bad";
  return "muted";
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
