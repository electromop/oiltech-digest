import { useEffect, useRef, useState } from "react";
import {
  deleteDocument,
  documentOriginalUrl,
  getDocument,
  listDocuments,
  uploadDocument,
} from "../../api/documents";
import type { DocumentDetails, UploadedDocument } from "../../api/types";
import { ErrorBoundary } from "../../app/ErrorBoundary";
import { DocumentCardPanel } from "./DocumentCardPanel";
import {
  detailText,
  formatSize,
  isDocumentPending,
  statusLabel,
  statusTone,
} from "./documentUtils";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

const ATTESTATION_LABEL = "Подтверждаю, что вправе загрузить этот материал и передать его в обработку";
const POLL_INTERVAL_MS = 5000;

export function DocumentsPage({ onUnauthorized, showToast }: Props) {
  const [documents, setDocuments] = useState<UploadedDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [attested, setAttested] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  const [details, setDetails] = useState<DocumentDetails | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [detailsError, setDetailsError] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Опрос идёт, только пока сервер что-то делает. Зависимость — БУЛЕВО значение,
  // а не массив документов: иначе таймер пересоздавался бы на каждом ответе списка
  // и 5-секундный интервал не доживал бы до срабатывания.
  const pending = documents.some(isDocumentPending);

  useEffect(() => {
    void reload(true);
  }, []);

  useEffect(() => {
    if (!pending) return;
    const timer = window.setInterval(() => {
      void reload(false);
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [pending]);

  async function reload(initial: boolean) {
    try {
      if (initial) setLoading(true);
      const payload = await listDocuments();
      setDocuments(Array.isArray(payload?.documents) ? payload.documents : []);
      setListError(null);
    } catch (error) {
      if (isUnauthorized(error)) {
        onUnauthorized();
        return;
      }
      setListError(detailText(error, "Не удалось загрузить список материалов"));
    } finally {
      if (initial) setLoading(false);
    }
  }

  async function handleUpload() {
    if (!file || !attested || uploading) return;
    setFormError(null);
    try {
      setUploading(true);
      const payload = await uploadDocument(file, attested);
      showToast(payload.duplicate ? "Такой файл уже был загружен" : "Файл принят, идёт разбор");
      setFile(null);
      setAttested(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
      await reload(false);
      if (payload.document?.id) {
        void openDocument(Number(payload.document.id));
      }
    } catch (error) {
      if (isUnauthorized(error)) {
        onUnauthorized();
        return;
      }
      // Тело ответа показываем как есть: 400/413/415/422/503 приходят с осмысленным
      // detail, и подменять его общей фразой значит скрыть причину отказа.
      const text = detailText(error, "Не удалось загрузить файл");
      setFormError(text);
      showToast(text, "error");
    } finally {
      setUploading(false);
    }
  }

  async function openDocument(documentId: number) {
    setOpenId(documentId);
    setDetails(null);
    setDetailsError(null);
    try {
      setDetailsLoading(true);
      const payload = await getDocument(documentId);
      setDetails(payload);
    } catch (error) {
      if (isUnauthorized(error)) {
        onUnauthorized();
        return;
      }
      setDetailsError(detailText(error, "Не удалось открыть карточку документа"));
    } finally {
      setDetailsLoading(false);
    }
  }

  async function handleDelete(documentId: number) {
    try {
      await deleteDocument(documentId);
      setConfirmDeleteId(null);
      if (openId === documentId) {
        setOpenId(null);
        setDetails(null);
      }
      showToast("Материал удалён");
      await reload(false);
    } catch (error) {
      if (isUnauthorized(error)) {
        onUnauthorized();
        return;
      }
      showToast(detailText(error, "Не удалось удалить материал"), "error");
    }
  }

  const canSubmit = Boolean(file) && attested && !uploading;

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Материалы</h1>
          <p>Загруженные документы разбираются моделью: паспорт, суть, сводка и факты с привязкой к месту в файле.</p>
        </div>
        <div className="statusPill">{documents.length} материалов</div>
      </header>

      <section className="panel">
        <div className="panelHeader">
          <h2>Загрузить материал</h2>
        </div>
        <p className="metaText" style={{ margin: "0 0 4px" }}>
          Принимаются документы до 25 МБ с текстовым слоем. Скан без распознанного текста будет отклонён: сводка по
          пустому тексту была бы выдумкой. Текст документа покидает контур для разбора моделью.
        </p>

        <div className="documentUploadGrid">
          <label className="field uploadField">
            <span>Файл материала</span>
            <input
              ref={fileInputRef}
              type="file"
              onChange={(event) => {
                setFile(event.target.files?.[0] ?? null);
                setFormError(null);
              }}
            />
          </label>
          <div className="documentFileMeta">
            {file ? (
              <>
                <strong>{file.name}</strong>
                <span>{formatSize(file.size)}</span>
              </>
            ) : (
              <span className="metaText">Файл не выбран</span>
            )}
          </div>
        </div>

        <label className="attestRow">
          <input type="checkbox" checked={attested} onChange={(event) => setAttested(event.target.checked)} />
          <span>{ATTESTATION_LABEL}</span>
        </label>

        <div className="settingsActions" style={{ marginTop: 10 }}>
          <button type="button" className="primaryButton" disabled={!canSubmit} onClick={() => void handleUpload()}>
            {uploading ? "Загружаем…" : "Загрузить"}
          </button>
          {!attested ? <span className="metaText">Без подтверждения загрузка недоступна.</span> : null}
        </div>

        {formError ? <div className="documentAlert bad" role="alert">{formError}</div> : null}
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Мои материалы</h2>
          <button type="button" className="ghostButton" onClick={() => void reload(false)}>
            Обновить
          </button>
        </div>

        {listError ? <div className="documentAlert bad">{listError}</div> : null}

        {loading ? (
          <div className="emptyState">
            <span>Загружаем список материалов…</span>
          </div>
        ) : documents.length === 0 ? (
          <div className="emptyState">
            <strong>Материалов пока нет</strong>
            <span>Загрузите первый документ формой выше.</span>
          </div>
        ) : (
          <div className="jobsTableWrap">
            <table className="jobsTable documentsTable">
              <thead>
                <tr>
                  <th>Имя файла</th>
                  <th>Формат</th>
                  <th>Размер</th>
                  <th>Состояние</th>
                  <th>Якорей</th>
                  <th>Фактов</th>
                  <th>Действия</th>
                </tr>
              </thead>
              <tbody>
                {documents.map((document) => (
                  <tr key={document.id} className={openId === document.id ? "documentRow active" : "documentRow"}>
                    <td>
                      <button type="button" className="documentNameButton" onClick={() => void openDocument(document.id)}>
                        {document.filename}
                      </button>
                    </td>
                    <td>{(document.kind || "—").toUpperCase()}</td>
                    <td>{formatSize(document.size_bytes)}</td>
                    <td>
                      <span className={`jobStatus documentStatus ${statusTone(String(document.status))}`}>
                        {statusLabel(String(document.status))}
                      </span>
                      {document.error_message ? <div className="metaText">{document.error_message}</div> : null}
                    </td>
                    <td>{document.anchor_count ?? "—"}</td>
                    <td>{document.fact_count ?? "—"}</td>
                    <td>
                      <div className="documentRowActions">
                        <a className="documentLink" href={documentOriginalUrl(document.id)} download>
                          Скачать оригинал
                        </a>
                        {confirmDeleteId === document.id ? (
                          <>
                            <button
                              type="button"
                              className="ghostButton dangerButton compactButton"
                              onClick={() => void handleDelete(document.id)}
                            >
                              Да, удалить
                            </button>
                            <button
                              type="button"
                              className="ghostButton compactButton"
                              onClick={() => setConfirmDeleteId(null)}
                            >
                              Отмена
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            className="ghostButton dangerButton compactButton"
                            onClick={() => setConfirmDeleteId(document.id)}
                          >
                            Удалить
                          </button>
                        )}
                      </div>
                      {confirmDeleteId === document.id ? (
                        <div className="metaText">Материал и его разбор будут удалены безвозвратно.</div>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {openId !== null ? (
        <section className="panel">
          <div className="panelHeader">
            <h2>Карточка материала</h2>
            <button type="button" className="ghostButton" onClick={() => { setOpenId(null); setDetails(null); }}>
              Закрыть
            </button>
          </div>
          {/* key на границе: открытие другого документа монтирует её заново, иначе
              один упавший документ оставил бы карточку сломанной навсегда. */}
          <ErrorBoundary key={openId} title="Карточку документа отобразить не удалось">
            {detailsLoading ? (
              <div className="emptyState"><span>Открываем карточку…</span></div>
            ) : detailsError ? (
              <div className="documentAlert bad">{detailsError}</div>
            ) : details ? (
              <DocumentCardPanel details={details} />
            ) : (
              <div className="emptyState"><span>Данных по документу нет.</span></div>
            )}
          </ErrorBoundary>
        </section>
      ) : null}
    </section>
  );
}

function isUnauthorized(error: unknown): boolean {
  return typeof error === "object" && error !== null && "status" in error && Number((error as { status: unknown }).status) === 401;
}
