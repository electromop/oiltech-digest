import { useEffect, useRef, useState } from "react";
import { getDigestPreviewPdf } from "../../api/digest";
import { SkeletonList } from "../shared/Skeleton";

export type PdfPreviewFilters = {
  month: string;
  limit: number;
  minScore: number;
  maxScore?: number;
  search?: string;
  topTag?: string;
};

type Props = {
  filters: PdfPreviewFilters;
  // Число растёт — PDF собирается заново (открытие экрана, смена выпуска, сохранённый черновик, «Обновить PDF»);
  // 0 — ещё не собирать.
  refreshToken: number;
  // PDF показывает прежнее состояние: фильтры сменились после сборки. Порядок черновика PDF не задаёт (#161).
  staleReason: "filters" | null;
  onRefresh: () => void;
  onError: (error: unknown) => void;
};

// Предпросмотр выпуска сразу в PDF (решение владельца 23.10): тот же документ, что уйдёт выгрузкой. PDF собирается
// на сервере 5-20 секунд, поэтому не на каждое нажатие: при открытии, смене выпуска, сохранении черновика и по кнопке.
// Пока собирается новый — виден прежний, сверху плашка «Собираем PDF…».
export function DigestPdfPreview({ filters, refreshToken, staleReason, onRefresh, onError }: Props) {
  const [url, setUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const urlRef = useRef<string | null>(null);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;
  // Сборку начала кнопка «Обновить PDF»: мимо памяти предпросмотра на сервере. Открытие экрана, смена выпуска и
  // сохранённый черновик берут готовый PDF того же выпуска.
  const freshRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    // 0 — экран ещё не знает выпуск (окно месяца не пришло): собирать нечего. В окружении без object URL
    // (тесты) предпросмотр просто не строится.
    if (refreshToken === 0 || typeof URL.createObjectURL !== "function") return;
    setLoading(true);
    setFailed(null);
    const fresh = freshRef.current;
    freshRef.current = false;
    getDigestPreviewPdf(filtersRef.current, { fresh })
      .then((file) => {
        if (cancelled) return;
        const next = URL.createObjectURL(file.blob);
        if (urlRef.current) URL.revokeObjectURL(urlRef.current);
        urlRef.current = next;
        setUrl(next);
      })
      .catch((error) => {
        if (cancelled) return;
        setFailed(error instanceof Error ? error.message : "Не удалось собрать PDF");
        onError(error);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshToken]);

  useEffect(
    () => () => {
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    },
    [],
  );

  return (
    <section className="digestPdfPane" aria-label="Предпросмотр выпуска в PDF">
      <div className="panelHeader">
        <h2>Предпросмотр PDF</h2>
        <button
          type="button"
          className="ghostButton compactButton"
          disabled={loading}
          onClick={() => {
            freshRef.current = true;
            onRefresh();
          }}
        >
          {loading ? "Собираем PDF…" : "Обновить PDF"}
        </button>
      </div>
      {staleReason && !loading ? (
        <div className="archiveNotice digestPdfStale" role="status">
          <span>Фильтры изменились после сборки PDF: обновите предпросмотр, чтобы увидеть выпуск как есть.</span>
        </div>
      ) : null}
      {failed ? <div className="documentAlert bad" role="alert">{failed}</div> : null}
      <div className={loading ? "digestPdfFrameWrap busy" : "digestPdfFrameWrap"}>
        {url ? (
          // Без панели миниатюр и по ширине окна: в узкой колонке миниатюры съедали половину места (25.10).
          <iframe className="digestPdfFrame" title="Предпросмотр выпуска в PDF" src={`${url}#navpanes=0&view=FitH`} />
        ) : loading ? (
          <SkeletonList rows={4} label="Собираем PDF выпуска" />
        ) : (
          <div className="emptyState">
            <span>PDF ещё не собран.</span>
          </div>
        )}
        {loading && url ? (
          <div className="digestPdfBusy" role="status">
            <span className="spinnerReact" aria-hidden="true" />
            Собираем PDF…
          </div>
        ) : null}
      </div>
    </section>
  );
}
