import { useEffect, useMemo, useRef, useState } from "react";
import { listArticles, updateArticle } from "../../api/articles";
import { updateSignal } from "../../api/signals";
import { getDigestContent, getMonthlyDigest, updateMonthlyDigest } from "../../api/digest";
import type { Article, DigestContent, MonthlyDigestDraft } from "../../api/types";
import { monthLabel, useFeedWindow } from "../articles/feedWindow";
import { SkeletonList } from "../shared/Skeleton";
import { DigestArchive } from "./DigestArchive";
import { exportDigestFile, type DigestExportFormat } from "./digestExport";
import { DigestActions, DigestHeader, DigestViewSwitch, type DigestView } from "./DigestHeader";
import { DigestPdfPreview } from "./DigestPdfPreview";
import { DigestFilters, DigestQueue, type IssueItem } from "./DigestQueue";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
  onArticlesChanged?: () => void;
};

const DIGEST_PREVIEW_LIMIT = 500;
// Один и тот же пустой массив, пока грузится архивный выпуск. Новый `[]` на каждом рендере
// пересоздавал бы цепочку useMemo до previewCandidateIds, а эффект синхронизации очереди
// звал бы setManualOrderIds снова и снова — бесконечный цикл рендеров.
const NO_ARTICLES: Article[] = [];

// Месячный дайджест (пересмотр 23-24.10): «Сборка выпуска» — всегда выпуск, который собирается сейчас: слева состав,
// справа тот же выпуск в PDF и действия с ним. Прошлые выпуски — только во вкладке «Архив выпусков» и только на
// просмотр (решение владельца 24.10). Оформление выпуска с экрана убрано: шапка и подвал — из сохранённого на сервере.
export function DigestPage({ onUnauthorized, showToast, onArticlesChanged }: Props) {
  const [articles, setArticles] = useState<Article[]>([]);
  const [digestPreview, setDigestPreview] = useState<DigestContent | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [draftBusy, setDraftBusy] = useState(false);
  const [savedDraft, setSavedDraft] = useState<MonthlyDigestDraft | null>(null);
  const [manualOrderIds, setManualOrderIds] = useState<number[]>([]);
  const [draftDirty, setDraftDirty] = useState(false);
  const [search, setSearch] = useState("");
  const [tag, setTag] = useState("");
  const [scoreMin, setScoreMin] = useState(0);
  const [scoreMax, setScoreMax] = useState(100);
  const [view, setView] = useState<DigestView>("issue");
  // Прошлый выпуск, открытый во вкладке архива; null — список архива.
  const [archiveOpen, setArchiveOpen] = useState<string | null>(null);
  // PDF собирается заново, когда растёт число: открытие, смена выпуска, сохранённый черновик, «Обновить PDF».
  const [pdfToken, setPdfToken] = useState(0);
  // Дружелюбная метка готовящегося документа («PDF»/«DOCX»/«HTML») — без понятий
  // «задача/очередь/№N»: пользователь видит процесс и результат, а не job-runner.
  const [exporting, setExporting] = useState<string | null>(null);
  // Окно месяца (ADR 0001, п. 6): открытые месяцы и прошлые выпуски с выбранными статьями.
  // Прошлый выпуск — только просмотр и выгрузка (решение владельца 23.09): статусы и
  // черновик не меняются, сервер такие правки отклоняет.
  const feedWindow = useFeedWindow();
  const firstOpenMonth = feedWindow?.months?.[0] ?? "";
  // Собирается выпуск первого открытого месяца окна (1–10 числа — прошлый месяц, дальше — текущий); прошлый
  // выпуск — только открытый во вкладке архива.
  const month = view === "archive" && archiveOpen ? archiveOpen : firstOpenMonth;
  const activeMonth = month || new Date().toISOString().slice(0, 7);
  const archiveIssueMonths = useMemo(
    () => (feedWindow?.archive ?? []).filter((item) => item.digest > 0).map((item) => item.month),
    [feedWindow],
  );
  const [archiveArticles, setArchiveArticles] = useState<Article[] | null>(null);
  const isArchiveMonth = Boolean(month) && Boolean(firstOpenMonth) && month < firstOpenMonth;
  // Лента (/api/articles) без месяца отдаёт только открытые месяцы — для прошлого выпуска
  // выбранные статьи приходят отдельным запросом с `month`.
  const sourceArticles = isArchiveMonth ? archiveArticles ?? NO_ARTICLES : articles;
  // Прошлый выпуск — целиком (ревью 24.10): фильтры состава к нему не применяются и на экране их нет, иначе выгрузка
  // из панели и из таблицы архива давала бы разные файлы.
  const appliedSearch = isArchiveMonth ? "" : search;
  const appliedTag = isArchiveMonth ? "" : tag;
  const appliedMin = isArchiveMonth ? 0 : scoreMin;
  const appliedMax = isArchiveMonth ? 100 : scoreMax;
  // Фильтры, с которыми собран показанный PDF: сменились — PDF устарел до «Обновить PDF».
  const filterSignature = JSON.stringify([appliedMin, appliedMax, appliedSearch, appliedTag]);
  const [pdfFilterSignature, setPdfFilterSignature] = useState(filterSignature);

  // Окно сменилось, пока вкладка была открыта (наступил день смены окна): выбранное за закрывшийся
  // месяц ушло в архив. Перечитываем список, иначе в очереди остались бы статьи, которые
  // уже нельзя сохранить в черновик (сервер ответит 409).
  const loadedOpenMonth = useRef("");
  useEffect(() => {
    if (!firstOpenMonth) return;
    if (loadedOpenMonth.current && loadedOpenMonth.current !== firstOpenMonth) {
      listArticles({ status: "digest", limit: 5000 })
        .then(setArticles)
        .catch((error) => handleError(error, "Не удалось обновить выбранное в дайджест"));
    }
    loadedOpenMonth.current = firstOpenMonth;
  }, [firstOpenMonth]);

  useEffect(() => {
    void reload();
  }, []);

  useEffect(() => {
    if (!isArchiveMonth) {
      setArchiveArticles(null);
      return;
    }
    let cancelled = false;
    listArticles({ status: "digest", month, limit: 5000 })
      .then((rows) => {
        if (!cancelled) setArchiveArticles(rows);
      })
      .catch((error) => {
        if (!cancelled) handleError(error, "Не удалось загрузить архивный выпуск");
      });
    return () => {
      cancelled = true;
    };
  }, [isArchiveMonth, month]);

  useEffect(() => {
    if (loading) return;
    void loadDigestPreview();
  }, [loading, month, appliedMin, appliedMax, appliedSearch, appliedTag]);

  // PDF — заново на открытии экрана и при смене выпуска; фильтры его только помечают устаревшим. Ждём окно
  // месяца и выпуск по умолчанию: иначе первый PDF собрался бы за все месяцы и сразу ещё раз — за выпуск.
  const windowKnown = feedWindow !== null;
  useEffect(() => {
    if (loading || !windowKnown || (!month && firstOpenMonth)) return;
    rebuildPdf();
  }, [loading, month, windowKnown, firstOpenMonth]);

  function rebuildPdf() {
    setPdfFilterSignature(filterSignature);
    setPdfToken((current) => current + 1);
  }

  useEffect(() => {
    if (loading) return;
    void loadSavedDraft(activeMonth);
  }, [loading, activeMonth]);

  const previewFilters = {
    month, limit: DIGEST_PREVIEW_LIMIT, minScore: appliedMin, maxScore: appliedMax, search: appliedSearch, topTag: appliedTag,
  };

  async function reload() {
    try {
      setLoading(true);
      // Грузим ВСЕ статьи со статусом «digest» с сервера (а не топ-2000 по score),
      // иначе низкоскоринговые материалы выпадают из дайджеста и их не убрать.
      const [articleRows, preview] = await Promise.all([
        listArticles({ status: "digest", limit: 5000 }),
        getDigestContent(previewFilters),
      ]);
      setArticles(articleRows);
      setDigestPreview(preview);
    } catch (error) {
      handleError(error, "Не удалось загрузить статьи для дайджеста");
    } finally {
      setLoading(false);
    }
  }

  async function loadDigestPreview() {
    try {
      setDigestPreview(await getDigestContent(previewFilters));
    } catch (error) {
      handleError(error, "Не удалось обновить состав выпуска");
    }
  }

  async function loadSavedDraft(targetMonth: string) {
    try {
      const draft = await getMonthlyDigest(targetMonth);
      setSavedDraft(draft);
      setManualOrderIds(draft.items.map((item) => item.article_id));
      setDraftDirty(false);
    } catch (error) {
      const status = typeof error === "object" && error && "status" in error ? Number(error.status) : 0;
      if (status === 404) {
        setSavedDraft(null);
        setDraftDirty(false);
        return;
      }
      handleError(error, "Не удалось загрузить сохранённый черновик");
    }
  }

  // Сигнал радара снят из дайджеста: выпуск на сервере сменился — состав и PDF собираются заново.
  async function removeSignalFromDigest(signalId: number) {
    try {
      setBusy(true);
      await updateSignal(signalId, { selected_for_digest: false });
      await loadDigestPreview();
      rebuildPdf();
      showToast("Сигнал убран из дайджеста");
    } catch (error) {
      handleError(error, "Не удалось убрать сигнал из дайджеста");
    } finally {
      setBusy(false);
    }
  }

  async function removeFromDigest(articleId: number) {
    try {
      setBusy(true);
      await updateArticle(articleId, { status: "archive" });
      await reload();
      onArticlesChanged?.();
      showToast("Статья убрана из дайджеста");
    } catch (error) {
      handleError(error, "Не удалось убрать статью из дайджеста");
    } finally {
      setBusy(false);
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
    const q = appliedSearch.trim().toLowerCase();
    return sourceArticles.filter((article) => {
      const hay = [article.title, article.tag, article.summary].join(" ").toLowerCase();
      return (
        article.digest &&
        (!q || hay.includes(q)) &&
        (!appliedTag || article.tag === appliedTag || article.tag.startsWith(`${appliedTag} /`)) &&
        Number(article.score || 0) >= appliedMin &&
        Number(article.score || 0) <= appliedMax &&
        (!month || String(article.date || "").startsWith(month))
      );
    });
  }, [sourceArticles, month, appliedMax, appliedMin, appliedSearch, appliedTag]);

  const topTags = useMemo(() => {
    const names = [...new Set(sourceArticles.map((article) => article.tag).filter(Boolean))].sort();
    return [...new Set(names.map((name) => name.split(" / ")[0]))];
  }, [sourceArticles]);
  // useMemo обязателен: пока превью не пришло, `|| []` давал НОВЫЙ массив на каждом рендере,
  // цепочка useMemo до previewCandidateIds пересчитывалась, а эффект синхронизации очереди
  // звал setManualOrderIds с новым массивом — рендер за рендером, пока не придёт превью (23.09).
  const exportPreviewItems = useMemo(() => digestPreview?.news || [], [digestPreview]);
  const radarItems = useMemo(() => exportPreviewItems.filter((item) => item.item_type === "signal"), [exportPreviewItems]);
  // Материал, как он выйдет в выпуске (состав на сервере): плашка «Технология / Бизнес» и суть, сжатая под площадь
  // карточки выпуска, — очередь показывает то же, что уйдёт в PDF.
  const issueItems = useMemo(() => {
    const map = new Map<number, IssueItem>();
    for (const item of exportPreviewItems) {
      if (item.article_id) {
        map.set(item.article_id, { kind: item.kind ?? null, label: item.kind_label ?? null, summary: item.summary || null });
      }
    }
    return map;
  }, [exportPreviewItems]);
  const previewOrderedCandidates = useMemo(() => {
    const byId = new Map(digestCandidates.map((article) => [article.id, article]));
    const ordered = exportPreviewItems
      .map((item) => (item.article_id ? byId.get(item.article_id) : undefined))
      .filter((article): article is Article => Boolean(article));
    const usedIds = new Set(ordered.map((article) => article.id));
    const remainder = digestCandidates.filter((article) => !usedIds.has(article.id));
    return [...ordered, ...remainder];
  }, [digestCandidates, exportPreviewItems]);
  const previewCandidateIds = useMemo(() => previewOrderedCandidates.map((article) => article.id), [previewOrderedCandidates]);
  const orderedDigestCandidates = useMemo(() => {
    const byId = new Map(previewOrderedCandidates.map((article) => [article.id, article]));
    return manualOrderIds.map((id) => byId.get(id)).filter((article): article is Article => Boolean(article));
  }, [manualOrderIds, previewOrderedCandidates]);
  const availableDigestCandidates = useMemo(() => {
    const selectedIds = new Set(manualOrderIds);
    return previewOrderedCandidates.filter((article) => !selectedIds.has(article.id));
  }, [manualOrderIds, previewOrderedCandidates]);
  const savedDraftVisibleIds = useMemo(
    () => (savedDraft?.items || []).map((item) => item.article_id).filter((id) => previewCandidateIds.includes(id)),
    [savedDraft, previewCandidateIds],
  );
  const hasManualChanges = draftDirty || JSON.stringify(manualOrderIds) !== JSON.stringify(savedDraftVisibleIds.length ? savedDraftVisibleIds : previewCandidateIds);

  useEffect(() => {
    if (draftDirty) return;
    const baseIds = savedDraftVisibleIds.length ? savedDraftVisibleIds : previewCandidateIds;
    setManualOrderIds(baseIds);
  }, [draftDirty, previewCandidateIds, savedDraftVisibleIds]);

  // Выгрузка выпуска файлом. monthOverride — выпуск из архива: целиком, без фильтров экрана.
  async function handleDigestExport(format: DigestExportFormat, monthOverride?: string) {
    try {
      setBusy(true);
      setExporting(format.toUpperCase());
      const filename = await exportDigestFile(
        format,
        monthOverride
          ? { month: monthOverride, limit: DIGEST_PREVIEW_LIMIT, minScore: 0, maxScore: 100 }
          : { month, limit: DIGEST_PREVIEW_LIMIT, minScore: appliedMin, maxScore: appliedMax, search: appliedSearch, topTag: appliedTag },
      );
      showToast(`Документ готов: ${filename}`);
    } catch (error) {
      handleError(error, "Не удалось подготовить документ");
    } finally {
      setBusy(false);
      setExporting(null);
    }
  }

  async function handleSaveDraft() {
    // Прошлый выпуск — только просмотр и выгрузка; кнопки в архиве нет, это страховка.
    if (isArchiveMonth) return;
    try {
      setDraftBusy(true);
      const draftMonth = activeMonth;
      const orderedItems = orderedDigestCandidates.map((article) => ({
        article_id: article.id,
        section: article.tag,
        editor_note: article.summary || null,
      }));
      const saved = await updateMonthlyDigest(draftMonth, {
        title: digestPreview?.title || `Нефтесервисный дайджест · ${draftMonth}`,
        status: "draft",
        items: orderedItems,
      });
      await loadSavedDraft(draftMonth);
      await loadDigestPreview();
      // Порядок выпуска на сервере сменился — PDF собирается заново.
      rebuildPdf();
      showToast(`Черновик сохранён: ${monthLabel(saved.month)}`);
    } catch (error) {
      handleError(error, "Не удалось сохранить черновик дайджеста");
    } finally {
      setDraftBusy(false);
    }
  }

  function moveDigestItem(articleId: number, direction: -1 | 1) {
    setDraftDirty(true);
    setManualOrderIds((current) => {
      const index = current.indexOf(articleId);
      if (index < 0) return current;
      const nextIndex = index + direction;
      if (nextIndex < 0 || nextIndex >= current.length) return current;
      const next = [...current];
      const [item] = next.splice(index, 1);
      next.splice(nextIndex, 0, item);
      return next;
    });
  }

  function removeFromIssue(articleId: number) {
    setDraftDirty(true);
    setManualOrderIds((current) => current.filter((id) => id !== articleId));
  }

  function addToIssue(articleId: number) {
    setDraftDirty(true);
    setManualOrderIds((current) => (current.includes(articleId) ? current : [...current, articleId]));
  }

  function resetDraftQueue() {
    const baseIds = savedDraftVisibleIds.length ? savedDraftVisibleIds : previewCandidateIds;
    setManualOrderIds(baseIds);
    setDraftDirty(false);
  }

  function switchView(next: DigestView) {
    setView(next);
    setArchiveOpen(null);
  }

  const draftInfo = isArchiveMonth
    ? "Прошлый выпуск: только просмотр и выгрузка"
    : (savedDraft?.month === activeMonth
        ? "Порядок статей меняется стрелками; «Сохранить черновик» закрепляет его в выпуске."
        : "Порядок как в выпуске на сервере; черновик ещё не сохранён.") +
      (radarItems.length ? " Сигналы радара идут после статей, по баллу." : "");
  const issueCount = orderedDigestCandidates.length + radarItems.length;
  // PDF собран из сохранённого черновика: несохранённый порядок в нём не виден, сменившиеся фильтры — тоже.
  const pdfStaleReason = pdfToken === 0 ? null : draftDirty ? "order" : pdfFilterSignature !== filterSignature ? "filters" : null;

  const workspace = (
    <div className="digestWorkspace">
      <section className="panel digestComposition">
        {busy ? (
          <div className="loadingOverlay" role="status">
            <span className="spinnerReact" aria-hidden="true" />
            <span>{exporting ? `Готовим документ ${exporting}…` : "Обновляем выпуск…"}</span>
          </div>
        ) : null}
        <div className="panelHeader">
          <h2>Состав выпуска</h2>
          <span className="badge">{digestCandidates.length + radarItems.length} отмечено</span>
        </div>
        {isArchiveMonth ? (
          <div className="archiveNotice" role="status">
            <span>Выпуск за {monthLabel(month)} в архиве: его можно смотреть и выгружать, но не менять.</span>
          </div>
        ) : null}
        {isArchiveMonth ? null : (
          <DigestFilters
            search={search}
            tag={tag}
            topTags={topTags}
            scoreMin={scoreMin}
            scoreMax={scoreMax}
            onSearch={setSearch}
            onTag={setTag}
            onScoreMin={setScoreMin}
            onScoreMax={setScoreMax}
          />
        )}
        {loading ? (
          <SkeletonList rows={4} label="Загружаем материалы выпуска" />
        ) : orderedDigestCandidates.length || availableDigestCandidates.length || radarItems.length ? (
          <DigestQueue
            items={orderedDigestCandidates}
            radarItems={radarItems}
            available={availableDigestCandidates}
            issueItems={issueItems}
            readOnly={isArchiveMonth}
            busy={busy}
            hasManualChanges={hasManualChanges}
            draftInfo={draftInfo}
            onMove={moveDigestItem}
            onRemoveFromIssue={removeFromIssue}
            onRemoveFromDigest={(articleId) => void removeFromDigest(articleId)}
            onRemoveSignal={(signalId) => void removeSignalFromDigest(signalId)}
            onAdd={addToIssue}
          />
        ) : (
          <div className="emptyState">
            <strong>В выпуске пока нет материалов</strong>
            <span>Отметьте сигналы кнопкой «В дайджест» в бизнес-ленте или на радаре.</span>
          </div>
        )}
      </section>

      <aside className="panel digestPreviewSide">
        <DigestActions
          archive={isArchiveMonth}
          busy={busy}
          draftBusy={draftBusy}
          hasManualChanges={hasManualChanges}
          onSave={() => void handleSaveDraft()}
          onReset={resetDraftQueue}
          onExport={(format) => void handleDigestExport(format)}
        />
        <DigestPdfPreview
          filters={previewFilters}
          refreshToken={pdfToken}
          staleReason={pdfStaleReason}
          onRefresh={rebuildPdf}
          onError={(error) => handleError(error, "Не удалось собрать PDF выпуска")}
        />
      </aside>
    </div>
  );

  return (
    <section className="screenStack">
      <DigestHeader activeMonth={activeMonth} issueCount={issueCount} archive={isArchiveMonth} />

      <DigestViewSwitch view={view} archiveCount={archiveIssueMonths.length} onChange={switchView} />

      {view === "archive" && archiveOpen ? (
        <div className="digestArchiveBar">
          <button type="button" className="ghostButton compactButton" onClick={() => setArchiveOpen(null)}>
            ← Все выпуски
          </button>
        </div>
      ) : null}
      {view === "archive" && !archiveOpen ? (
        <section className="panel">
          <div className="panelHeader">
            <h2>Архив выпусков</h2>
            {exporting ? (
              <span className="inlineLoader digestArchiveExport" role="status">
                <span className="loaderDot" aria-hidden="true" />
                Готовим документ {exporting}…
              </span>
            ) : null}
          </div>
          <DigestArchive months={feedWindow?.archive ?? []} rolloverDay={feedWindow?.rollover_day} busy={busy}
            onOpen={setArchiveOpen} onDownload={(target) => void handleDigestExport("pdf", target)} />
        </section>
      ) : null}
      {/* Состав и PDF не размонтируются, пока открыт список архива (ревью 24.10): возврат на вкладку не собирает
          PDF заново, а «Открыть» в архиве шлёт один запрос PDF, а не два. */}
      <div hidden={view === "archive" && !archiveOpen}>{workspace}</div>
    </section>
  );
}
