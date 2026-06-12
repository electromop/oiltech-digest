import { useEffect, useMemo, useState } from "react";
import { listArticles } from "../../api/articles";
import { enqueueDigestExport, getDigestBranding, saveDigestBranding } from "../../api/digest";
import { downloadJobResult, getJob } from "../../api/jobs";
import type { Article, BackgroundJob, DigestBranding, DigestBrandingSocial, DigestHighlightCard, DigestHighlightRules } from "../../api/types";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

export function DigestPage({ onUnauthorized, showToast }: Props) {
  const [articles, setArticles] = useState<Article[]>([]);
  const [branding, setBranding] = useState<DigestBranding | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [brandingBusy, setBrandingBusy] = useState(false);
  const [search, setSearch] = useState("");
  const [tag, setTag] = useState("");
  const [tagQuery, setTagQuery] = useState("");
  const [showTagOptions, setShowTagOptions] = useState(false);
  const [month, setMonth] = useState("");
  const [scoreMin, setScoreMin] = useState(0);
  const [scoreMax, setScoreMax] = useState(100);
  const [draftInfo, setDraftInfo] = useState<string>("");
  const [exportJobId, setExportJobId] = useState<number | null>(null);

  useEffect(() => {
    void reload();
  }, []);

  async function reload() {
    try {
      setLoading(true);
      const [articleRows, brandingPayload] = await Promise.all([listArticles(), getDigestBranding()]);
      setArticles(articleRows);
      setBranding({
        ...brandingPayload,
        issue: {
          title_template: brandingPayload.issue?.title_template || "Нефтесервисный дайджест",
          title_template_with_month: brandingPayload.issue?.title_template_with_month || "Нефтесервисный дайджест · {month}",
          period_label_all: brandingPayload.issue?.period_label_all || "за всё время",
          preheader: brandingPayload.issue?.preheader || "Ключевые новости и обзоры нефтесервисного рынка",
          intro_template:
            brandingPayload.issue?.intro_template ||
            "Уважаемые коллеги! Представляем ключевые новости и обзоры нефтесервисного рынка, которые помогают отслеживать технологические тренды, рыночную динамику и возможности для развития бизнеса.",
          intro_template_with_month:
            brandingPayload.issue?.intro_template_with_month ||
            "Уважаемые коллеги! Представляем ключевые новости и обзоры за {month}, которые помогают отслеживать технологические тренды, рыночную динамику и возможности для развития нефтесервисного бизнеса.",
          highlights_title: brandingPayload.issue?.highlights_title || "Главное за период",
          news_title: brandingPayload.issue?.news_title || "Новости",
          read_more_label: brandingPayload.issue?.read_more_label || "ЧИТАТЬ ДАЛЕЕ",
          empty_summary_text: brandingPayload.issue?.empty_summary_text || "Суть ещё не сформирована.",
          preview_empty_text: brandingPayload.issue?.preview_empty_text || "В текущей выборке нет сигналов для превью.",
        },
        highlights: {
          analytics_source_keywords: brandingPayload.highlights?.analytics_source_keywords || [],
          analytics_category_keywords: brandingPayload.highlights?.analytics_category_keywords || [],
          business_category_keywords: brandingPayload.highlights?.business_category_keywords || [],
          cards: brandingPayload.highlights?.cards || defaultHighlightCards(),
        },
      });
    } catch (error) {
      handleError(error, "Не удалось загрузить статьи для дайджеста");
    } finally {
      setLoading(false);
    }
  }

  function handleError(error: unknown, fallback: string) {
    const status = typeof error === "object" && error && "status" in error ? Number(error.status) : 0;
    const message = error instanceof Error ? error.message : fallback;
    if (status === 401) {
      onUnauthorized();
      return;
    }
    showToast(message || fallback, "error");
  }

  const digestCandidates = useMemo(() => {
    const q = search.trim().toLowerCase();
    return articles.filter((article) => {
      const hay = [article.title, article.tag, article.summary].join(" ").toLowerCase();
      return (
        article.digest &&
        (!q || hay.includes(q)) &&
        (!tag || article.tag === tag || article.tag.startsWith(`${tag} /`)) &&
        Number(article.score || 0) >= scoreMin &&
        Number(article.score || 0) <= scoreMax &&
        (!month || String(article.date || "").startsWith(month))
      );
    });
  }, [articles, month, scoreMax, scoreMin, search, tag]);

  const months = useMemo(() => {
    return [...new Set(articles.filter((article) => article.digest).map((article) => String(article.date || "").slice(0, 7)).filter(Boolean))]
      .sort()
      .reverse();
  }, [articles]);

  const topTags = useMemo(() => {
    const names = [...new Set(articles.map((article) => article.tag).filter(Boolean))].sort();
    return [...new Set(names.map((name) => name.split(" / ")[0]))];
  }, [articles]);
  const filteredTagOptions = useMemo(() => {
    const q = tagQuery.trim().toLowerCase();
    return topTags.filter((option) => !q || option.toLowerCase().includes(q));
  }, [tagQuery, topTags]);

  function updateBrandingSection<K extends keyof DigestBranding>(section: K, value: DigestBranding[K]) {
    setBranding((prev) => (prev ? { ...prev, [section]: value } : prev));
  }

  function updateFooterSocial(index: number, field: keyof DigestBrandingSocial, value: string) {
    setBranding((prev) => {
      if (!prev) return prev;
      const socials = prev.footer.socials.map((item, itemIndex) =>
        itemIndex === index ? { ...item, [field]: value } : item,
      );
      return { ...prev, footer: { ...prev.footer, socials } };
    });
  }

  function addFooterSocial() {
    setBranding((prev) =>
      prev
        ? {
            ...prev,
            footer: {
              ...prev.footer,
              socials: [...prev.footer.socials, { label: "", accent: "#003da6", text: "" }],
            },
          }
        : prev,
    );
  }

  function removeFooterSocial(index: number) {
    setBranding((prev) =>
      prev
        ? {
            ...prev,
            footer: {
              ...prev.footer,
              socials: prev.footer.socials.filter((_, itemIndex) => itemIndex !== index),
            },
          }
        : prev,
    );
  }

  function updateHighlightRules(value: DigestHighlightRules) {
    setBranding((prev) => (prev ? { ...prev, highlights: value } : prev));
  }

  function updateHighlightCard(index: number, patch: Partial<DigestHighlightCard>) {
    setBranding((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        highlights: {
          ...prev.highlights,
          cards: prev.highlights.cards.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item)),
        },
      };
    });
  }

  async function handleSaveBranding() {
    if (!branding) return;
    try {
      setBrandingBusy(true);
      const payload = {
        ...branding,
        footer: {
          ...branding.footer,
          socials: branding.footer.socials.filter((item) => item.label.trim() || item.text.trim()),
        },
      };
      const saved = await saveDigestBranding(payload);
      setBranding({
        ...saved.branding,
        issue: {
          title_template: saved.branding.issue?.title_template || "Нефтесервисный дайджест",
          title_template_with_month: saved.branding.issue?.title_template_with_month || "Нефтесервисный дайджест · {month}",
          period_label_all: saved.branding.issue?.period_label_all || "за всё время",
          preheader: saved.branding.issue?.preheader || "Ключевые новости и обзоры нефтесервисного рынка",
          intro_template:
            saved.branding.issue?.intro_template ||
            "Уважаемые коллеги! Представляем ключевые новости и обзоры нефтесервисного рынка, которые помогают отслеживать технологические тренды, рыночную динамику и возможности для развития бизнеса.",
          intro_template_with_month:
            saved.branding.issue?.intro_template_with_month ||
            "Уважаемые коллеги! Представляем ключевые новости и обзоры за {month}, которые помогают отслеживать технологические тренды, рыночную динамику и возможности для развития нефтесервисного бизнеса.",
          highlights_title: saved.branding.issue?.highlights_title || "Главное за период",
          news_title: saved.branding.issue?.news_title || "Новости",
          read_more_label: saved.branding.issue?.read_more_label || "ЧИТАТЬ ДАЛЕЕ",
          empty_summary_text: saved.branding.issue?.empty_summary_text || "Суть ещё не сформирована.",
          preview_empty_text: saved.branding.issue?.preview_empty_text || "В текущей выборке нет сигналов для превью.",
        },
        highlights: {
          analytics_source_keywords: saved.branding.highlights?.analytics_source_keywords || [],
          analytics_category_keywords: saved.branding.highlights?.analytics_category_keywords || [],
          business_category_keywords: saved.branding.highlights?.business_category_keywords || [],
          cards: saved.branding.highlights?.cards || defaultHighlightCards(),
        },
      });
      showToast("Оформление дайджеста сохранено");
    } catch (error) {
      handleError(error, "Не удалось сохранить оформление дайджеста");
    } finally {
      setBrandingBusy(false);
    }
  }

  async function handleDigestExport(format: "pdf" | "docx" | "html") {
    try {
      setBusy(true);
      const queued = await enqueueDigestExport(month, 200, scoreMin, format);
      setExportJobId(queued.job.id);
      setDraftInfo(`export job #${queued.job.id}: ${statusLabel(queued.job.status)}`);
      showToast(`Экспорт поставлен в очередь: job #${queued.job.id}`);

      const finished = await waitForExportJob(queued.job.id);
      setDraftInfo(`export job #${finished.id}: ${statusLabel(finished.status)}`);
      if (finished.status !== "ok") {
        throw new Error(finished.error || "Экспорт завершился ошибкой");
      }

      const file = await downloadJobResult(finished.id);
      const url = window.URL.createObjectURL(file.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = file.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
      showToast(`Экспорт готов: ${file.filename}`);
    } catch (error) {
      handleError(error, "Не удалось скачать экспорт");
    } finally {
      setBusy(false);
    }
  }

  async function waitForExportJob(jobId: number) {
    for (let attempt = 0; attempt < 80; attempt += 1) {
      const job = await getJob(jobId);
      setDraftInfo(`export job #${job.id}: ${statusLabel(job.status)} · ${Math.round(job.progress)}%`);
      if (job.status === "ok" || job.status === "failed") {
        return job;
      }
      await sleep(1500);
    }
    throw new Error(`Job #${jobId} не завершилась за отведённое время`);
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Месячный дайджест</h1>
        </div>
        <div className="statusPill">{digestCandidates.length} статей</div>
      </header>

      <section className="panel">
        {busy ? <InlineLoader label="Готовим экспорт в фоне…" /> : null}
        <div className="panelHeader">
          <h2>Выборка дайджеста</h2>
        </div>

        <div className="digestFiltersRow">
          <label className="field">
            <span>Поиск</span>
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Найти статью" />
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

        <div className="digestFiltersRow digestFiltersRowSecondary">
          <label className="field">
            <span>Месяц</span>
            <select value={month} onChange={(event) => setMonth(event.target.value)}>
              <option value="">Все месяцы</option>
              {months.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Score от</span>
            <input type="number" value={scoreMin} onChange={(event) => setScoreMin(Number(event.target.value || 0))} />
          </label>
          <label className="field">
            <span>Score до</span>
            <input type="number" value={scoreMax} onChange={(event) => setScoreMax(Number(event.target.value || 100))} />
          </label>
        </div>

        <div className="digestToolbar">
          <div className="digestActionGroup digestActionGroupPrimary">
            <span className="digestGroupLabel">Скачать дайджест</span>
            <button type="button" className="primaryButton" onClick={() => void handleDigestExport("pdf")}>
              PDF
            </button>
            <button type="button" className="ghostButton" onClick={() => void handleDigestExport("docx")}>
              DOCX
            </button>
            <button type="button" className="ghostButton" onClick={() => void handleDigestExport("html")}>
              HTML
            </button>
          </div>
        </div>

        {draftInfo ? (
          <div className="digestDraftInfo metaText">
            {draftInfo}
            {exportJobId ? (
              <>
                {" · "}
                <a href="?screen=jobs">Открыть задачи</a>
              </>
            ) : null}
          </div>
        ) : null}

        {branding ? (
          <div className="settingsCard digestBrandingCard">
            <div className="panelHeader">
              <h2>Оформление выпуска</h2>
              <button type="button" className="primaryButton" disabled={brandingBusy} onClick={() => void handleSaveBranding()}>
                Сохранить оформление
              </button>
            </div>
            <div className="settingsGrid">
              <label className="field">
                <span>Бренд</span>
                <input
                  value={branding.header.brand_text}
                  onChange={(event) => updateBrandingSection("header", { ...branding.header, brand_text: event.target.value })}
                />
              </label>
              <label className="field">
                <span>Слоган</span>
                <input
                  value={branding.header.brand_suffix}
                  onChange={(event) => updateBrandingSection("header", { ...branding.header, brand_suffix: event.target.value })}
                />
              </label>
              <label className="field fieldWide">
                <span>Подразделение</span>
                <input
                  value={branding.header.department_text}
                  onChange={(event) => updateBrandingSection("header", { ...branding.header, department_text: event.target.value })}
                />
              </label>
              <label className="field">
                <span>Hero badge</span>
                <input
                  value={branding.hero.badge}
                  onChange={(event) => updateBrandingSection("hero", { ...branding.hero, badge: event.target.value })}
                />
              </label>
              <label className="field">
                <span>Hero headline</span>
                <input
                  value={branding.hero.headline}
                  onChange={(event) => updateBrandingSection("hero", { ...branding.hero, headline: event.target.value })}
                />
              </label>
              <label className="field fieldWide">
                <span>Hero subtitle</span>
                <input
                  value={branding.hero.subtitle}
                  onChange={(event) => updateBrandingSection("hero", { ...branding.hero, subtitle: event.target.value })}
                />
              </label>
              <label className="field fieldWide">
                <span>Hero image URL</span>
                <input
                  value={branding.hero.image_url}
                  onChange={(event) => updateBrandingSection("hero", { ...branding.hero, image_url: event.target.value })}
                  placeholder="https://example.com/hero.jpg"
                />
              </label>
              <label className="field">
                <span>Preheader</span>
                <input
                  value={branding.issue.preheader}
                  onChange={(event) => updateBrandingSection("issue", { ...branding.issue, preheader: event.target.value })}
                />
              </label>
              <label className="field">
                <span>Title template</span>
                <input
                  value={branding.issue.title_template}
                  onChange={(event) => updateBrandingSection("issue", { ...branding.issue, title_template: event.target.value })}
                />
              </label>
              <label className="field">
                <span>Title with month</span>
                <input
                  value={branding.issue.title_template_with_month}
                  onChange={(event) => updateBrandingSection("issue", { ...branding.issue, title_template_with_month: event.target.value })}
                />
              </label>
              <label className="field">
                <span>Заголовок KPI</span>
                <input
                  value={branding.issue.highlights_title}
                  onChange={(event) => updateBrandingSection("issue", { ...branding.issue, highlights_title: event.target.value })}
                />
              </label>
              <label className="field">
                <span>Заголовок новостей</span>
                <input
                  value={branding.issue.news_title}
                  onChange={(event) => updateBrandingSection("issue", { ...branding.issue, news_title: event.target.value })}
                />
              </label>
              <label className="field">
                <span>CTA</span>
                <input
                  value={branding.issue.read_more_label}
                  onChange={(event) => updateBrandingSection("issue", { ...branding.issue, read_more_label: event.target.value })}
                />
              </label>
              <label className="field">
                <span>Fallback сути</span>
                <input
                  value={branding.issue.empty_summary_text}
                  onChange={(event) => updateBrandingSection("issue", { ...branding.issue, empty_summary_text: event.target.value })}
                />
              </label>
              <label className="field">
                <span>Пустое превью</span>
                <input
                  value={branding.issue.preview_empty_text}
                  onChange={(event) => updateBrandingSection("issue", { ...branding.issue, preview_empty_text: event.target.value })}
                />
              </label>
              <label className="field">
                <span>Период без месяца</span>
                <input
                  value={branding.issue.period_label_all}
                  onChange={(event) => updateBrandingSection("issue", { ...branding.issue, period_label_all: event.target.value })}
                />
              </label>
              <label className="field fieldWide">
                <span>Intro template</span>
                <textarea
                  value={branding.issue.intro_template}
                  onChange={(event) => updateBrandingSection("issue", { ...branding.issue, intro_template: event.target.value })}
                />
              </label>
              <label className="field fieldWide">
                <span>Intro with month</span>
                <textarea
                  value={branding.issue.intro_template_with_month}
                  onChange={(event) => updateBrandingSection("issue", { ...branding.issue, intro_template_with_month: event.target.value })}
                  placeholder="Используйте {month}"
                />
              </label>
              <label className="field fieldWide">
                <span>Контактный текст</span>
                <textarea
                  value={branding.footer.contact_text}
                  onChange={(event) => updateBrandingSection("footer", { ...branding.footer, contact_text: event.target.value })}
                />
              </label>
              <label className="field">
                <span>Email</span>
                <input
                  value={branding.footer.contact_email}
                  onChange={(event) => updateBrandingSection("footer", { ...branding.footer, contact_email: event.target.value })}
                />
              </label>
              <label className="field">
                <span>Примечание</span>
                <input
                  value={branding.footer.note}
                  onChange={(event) => updateBrandingSection("footer", { ...branding.footer, note: event.target.value })}
                />
              </label>
            </div>
            <div className="digestSocialsEditor">
              <div className="panelHeader">
                <h3>Правила KPI</h3>
                <span className="metaText">Ключевые слова через запятую. Аналитика считается по тегам и источникам, бизнес — по тегам.</span>
              </div>
              <div className="settingsGrid">
                <label className="field fieldWide">
                  <span>Источники аналитики</span>
                  <textarea
                    value={branding.highlights.analytics_source_keywords.join(", ")}
                    onChange={(event) =>
                      updateHighlightRules({
                        ...branding.highlights,
                        analytics_source_keywords: parseKeywordList(event.target.value),
                      })
                    }
                    placeholder="rystad, wood mac, mckinsey"
                  />
                </label>
                <label className="field fieldWide">
                  <span>Теги аналитики</span>
                  <textarea
                    value={branding.highlights.analytics_category_keywords.join(", ")}
                    onChange={(event) =>
                      updateHighlightRules({
                        ...branding.highlights,
                        analytics_category_keywords: parseKeywordList(event.target.value),
                      })
                    }
                    placeholder="аналит, обзор, прогноз"
                  />
                </label>
                <label className="field fieldWide">
                  <span>Теги бизнес-возможностей</span>
                  <textarea
                    value={branding.highlights.business_category_keywords.join(", ")}
                    onChange={(event) =>
                      updateHighlightRules({
                        ...branding.highlights,
                        business_category_keywords: parseKeywordList(event.target.value),
                      })
                    }
                    placeholder="контракт, сделк, инвест"
                  />
                </label>
              </div>
            </div>
            <div className="digestSocialsEditor">
              <div className="panelHeader">
                <h3>KPI-плашки</h3>
                <span className="metaText">Можно поменять метрику, иконку и формулировку каждой карточки.</span>
              </div>
              <div className="digestSocialsList">
                {branding.highlights.cards.map((item, index) => (
                  <div className="digestSocialRow" key={`${item.metric}-${index}`}>
                    <label className="field">
                      <span>Метрика</span>
                      <select value={item.metric} onChange={(event) => updateHighlightCard(index, { metric: event.target.value as DigestHighlightCard["metric"] })}>
                        <option value="total">Всего</option>
                        <option value="analytics">Аналитика</option>
                        <option value="business">Бизнес</option>
                      </select>
                    </label>
                    <label className="field">
                      <span>Иконка</span>
                      <select value={item.icon} onChange={(event) => updateHighlightCard(index, { icon: event.target.value as DigestHighlightCard["icon"] })}>
                        <option value="doc">Doc</option>
                        <option value="chart">Chart</option>
                        <option value="people">People</option>
                      </select>
                    </label>
                    <label className="field">
                      <span>Префикс</span>
                      <input value={item.prefix} onChange={(event) => updateHighlightCard(index, { prefix: event.target.value })} />
                    </label>
                    <label className="field">
                      <span>1</span>
                      <input value={item.noun_one} onChange={(event) => updateHighlightCard(index, { noun_one: event.target.value })} />
                    </label>
                    <label className="field">
                      <span>2-4</span>
                      <input value={item.noun_few} onChange={(event) => updateHighlightCard(index, { noun_few: event.target.value })} />
                    </label>
                    <label className="field">
                      <span>5+</span>
                      <input value={item.noun_many} onChange={(event) => updateHighlightCard(index, { noun_many: event.target.value })} />
                    </label>
                    <label className="field">
                      <span>Суффикс</span>
                      <input value={item.suffix} onChange={(event) => updateHighlightCard(index, { suffix: event.target.value })} />
                    </label>
                  </div>
                ))}
              </div>
            </div>
            <div className="digestSocialsEditor">
              <div className="panelHeader">
                <h3>Соцсети и каналы</h3>
                <button type="button" className="ghostButton" onClick={addFooterSocial}>
                  Добавить
                </button>
              </div>
              <div className="digestSocialsList">
                {branding.footer.socials.map((item, index) => (
                  <div className="digestSocialRow" key={`${item.label}-${index}`}>
                    <label className="field">
                      <span>Название</span>
                      <input value={item.label} onChange={(event) => updateFooterSocial(index, "label", event.target.value)} />
                    </label>
                    <label className="field">
                      <span>Текст</span>
                      <input value={item.text} onChange={(event) => updateFooterSocial(index, "text", event.target.value)} />
                    </label>
                    <label className="field">
                      <span>Цвет</span>
                      <input value={item.accent} onChange={(event) => updateFooterSocial(index, "accent", event.target.value)} />
                    </label>
                    <button type="button" className="ghostButton compactButton" onClick={() => removeFooterSocial(index)}>
                      Удалить
                    </button>
                  </div>
                ))}
              </div>
            </div>
            <div className="digestPreviewLive">
              <div className="panelHeader">
                <h3>Live preview</h3>
                <span className="metaText">Показывает текущие поля branding и первые сигналы выборки</span>
              </div>
              <DigestBrandingPreview branding={branding} articles={digestCandidates.slice(0, 3)} />
            </div>
          </div>
        ) : null}

        {loading ? (
          <div className="emptyState"><LoadingState label="Загружаем сигналы…" /></div>
        ) : digestCandidates.length ? (
          <div className="digestLayout">
            <div className="digestPickListReact">
              {digestCandidates.map((article) => (
                <div className="digestPickRow" key={article.id}>
                  <a href={article.url} target="_blank" rel="noreferrer">
                    <strong>{article.title}</strong>
                  </a>
                  <div className="metaText">{article.tag} · score {Math.round(Number(article.score || 0))} · {formatDate(article.date)}</div>
                </div>
              ))}
            </div>
            <div className="digestPreviewGrid">
              {digestCandidates.map((article, index) => (
                <article className="digestPreviewCard" key={article.id}>
                  <strong>{index + 1}. {article.tag}</strong>
                  <div>{article.title}</div>
                  <div className="metaText">{article.summary || branding?.issue.empty_summary_text || "Суть ещё не сформирована."}</div>
                </article>
              ))}
            </div>
          </div>
        ) : (
          <div className="emptyState">Нет статей со статусом «В дайджест» по текущим фильтрам.</div>
        )}
      </section>
    </section>
  );
}

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function statusLabel(status: BackgroundJob["status"]) {
  if (status === "queued") return "в очереди";
  if (status === "running") return "в работе";
  if (status === "ok") return "готово";
  return "ошибка";
}

function parseKeywordList(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function defaultHighlightCards(): DigestHighlightCard[] {
  return [
    { metric: "total", icon: "doc", prefix: "", suffix: "", noun_one: "новость", noun_few: "новости", noun_many: "новостей" },
    { metric: "analytics", icon: "chart", prefix: "аналитических", suffix: "", noun_one: "материал", noun_few: "материала", noun_many: "материалов" },
    { metric: "business", icon: "people", prefix: "", suffix: "для бизнеса", noun_one: "возможность", noun_few: "возможности", noun_many: "возможностей" },
  ];
}

function plural(n: number, one: string, few: string, many: string) {
  const mod100 = Math.abs(n) % 100;
  if (mod100 > 10 && mod100 < 20) return many;
  const mod10 = mod100 % 10;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

function computePreviewHighlights(articles: Article[], rules: DigestHighlightRules) {
  const analytics = articles.filter((article) => {
    const tag = (article.tag || "").toLowerCase();
    const source = (article.source || "").toLowerCase();
    return (
      rules.analytics_category_keywords.some((keyword) => tag.includes(keyword.toLowerCase())) ||
      rules.analytics_source_keywords.some((keyword) => source.includes(keyword.toLowerCase()))
    );
  }).length;
  const business = articles.filter((article) => {
    const tag = (article.tag || "").toLowerCase();
    return rules.business_category_keywords.some((keyword) => tag.includes(keyword.toLowerCase()));
  }).length;
  const metrics = { total: articles.length, analytics, business };
  const cards = rules.cards?.length ? rules.cards : defaultHighlightCards();
  return cards.map((card) => ({
    value: metrics[card.metric],
    label: [card.prefix, plural(metrics[card.metric], card.noun_one, card.noun_few, card.noun_many), card.suffix]
      .filter(Boolean)
      .join(" "),
  }));
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

function LoadingState(props: { label: string }) {
  return (
    <div className="loadingStateReact">
      <div className="spinnerReact" />
      <span>{props.label}</span>
    </div>
  );
}

function DigestBrandingPreview(props: { branding: DigestBranding; articles: Article[] }) {
  const { branding, articles } = props;
  const highlights = computePreviewHighlights(articles, branding.highlights);
  return (
    <div className="digestPreviewLiveFrame">
      <div className="digestPreviewHeader">
        <div className="digestPreviewBrandLine">
          <strong>{branding.header.brand_text}</strong>
          <span>/ {branding.header.brand_suffix}</span>
        </div>
        <div className="digestPreviewDept">{branding.header.department_text}</div>
      </div>

      <div className="metaText">{branding.issue.preheader}</div>

      <div className="digestPreviewHero">
        {branding.hero.image_url ? <div className="digestPreviewHeroImage" style={{ backgroundImage: `url(${branding.hero.image_url})` }} /> : null}
        <div className="digestPreviewBadge">{branding.hero.badge}</div>
        <div className="digestPreviewHeadline">{branding.hero.headline}</div>
        <div className="digestPreviewSubtitle">{branding.hero.subtitle}</div>
      </div>

      <div className="metaText">
        {articles.length
          ? branding.issue.intro_template_with_month.replace("{month}", "текущий выпуск")
          : branding.issue.intro_template}
      </div>

      <div className="digestPreviewSectionTitle">{branding.issue.highlights_title}</div>
      <div className="digestPreviewHighlights">
        {highlights.map((item) => (
          <div className="digestPreviewHighlight" key={item.label}>
            <strong>{item.value}</strong>
            <span>{item.label}</span>
          </div>
        ))}
      </div>

      <div className="digestPreviewSectionTitle">{branding.issue.news_title}</div>
      <div className="digestPreviewCards">
        {articles.length ? (
          articles.map((article, index) => (
            <article className="digestPreviewLiveCard" key={article.id}>
              <div className="digestPreviewLiveMeta">{index + 1}. {article.tag}</div>
              <div className="digestPreviewLiveTitle">{article.title}</div>
              <div className="digestPreviewLiveSummary">{article.summary || branding.issue.empty_summary_text}</div>
              <div className="digestPreviewLiveFooter">
                <span>{branding.issue.read_more_label}</span>
                <span>score {Math.round(Number(article.score || 0))}</span>
              </div>
            </article>
          ))
        ) : (
          <div className="digestPreviewEmpty">{branding.issue.preview_empty_text}</div>
        )}
      </div>

      <div className="digestPreviewFooter">
        <div className="digestPreviewSocials">
          {branding.footer.socials.map((item, index) => (
            <span
              key={`${item.label}-${index}`}
              className="digestPreviewSocial"
              style={{ color: item.accent || "#262d3c" }}
              title={item.label}
            >
              {item.text || item.label}
            </span>
          ))}
        </div>
        <div className="digestPreviewContact">
          <strong>{branding.footer.contact_text}</strong>
          <span>{branding.footer.contact_email}</span>
          <span>{branding.footer.note}</span>
        </div>
      </div>
    </div>
  );
}
