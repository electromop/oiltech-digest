import { useRef, useState, type FormEvent } from "react";
import { uploadDocument } from "../../api/documents";
import { detailText, formatSize } from "../documents/documentUtils";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
  // Файл принят: номер документа — открыть его разбор.
  onUploaded: (documentId: number | null) => void;
};

// Текст ДОСЛОВНО совпадает с ATTESTATION_TEXT в oiltech_digest/api.py: в базу
// пишется то, что человек реально прочитал. Аттестация — фиксация ответственности,
// и расхождение формулировок обесценивает её как доказательство.
const ATTESTATION_LABEL =
  "Подтверждаю, что вправе загрузить этот материал и передать его в обработку, " +
  "и что мне известно: его текст покидает контур для разбора моделью";

// «Внести материал» (решение владельца 23.10): загрузка документа по логике прежнего раздела «Материалы»,
// в подокне раздела. Кнопка доступна всегда, условия проверяются при отправке и называются у поля.
export function MaterialUploadForm({ onUnauthorized, showToast, onUploaded }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [attested, setAttested] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (uploading) return;
    if (!file) {
      setFormError("Выберите файл материала: PDF, PPTX или DOCX до 25 МБ.");
      fileInputRef.current?.focus();
      return;
    }
    if (!attested) {
      setFormError("Подтвердите право на загрузку: без этого файл не уйдёт в разбор.");
      return;
    }
    setFormError(null);
    try {
      setUploading(true);
      const payload = await uploadDocument(file, attested);
      showToast(payload.duplicate ? "Такой файл уже был загружен" : "Файл принят, идёт разбор");
      setFile(null);
      setAttested(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
      onUploaded(payload.document?.id ? Number(payload.document.id) : null);
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

  return (
    <form className="materialUploadForm" noValidate onSubmit={(event) => void handleSubmit(event)}>
      <p className="metaText">
        Принимаются документы до 25 МБ с текстовым слоем. Скан без распознанного текста будет отклонён: сводка по
        пустому тексту была бы выдумкой. Текст документа покидает контур для разбора моделью.
      </p>

      <div className="documentUploadGrid">
        <label className="field uploadField">
          <span>Файл материала</span>
          <input
            ref={fileInputRef}
            type="file"
            name="material-file"
            accept=".pdf,.pptx,.docx"
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

      {formError ? <div className="documentAlert bad" role="alert">{formError}</div> : null}

      <div className="dialogActions">
        <button type="submit" className="primaryButton" disabled={uploading}>
          {uploading ? "Загружаем…" : "Загрузить"}
        </button>
      </div>
    </form>
  );
}

function isUnauthorized(error: unknown): boolean {
  return typeof error === "object" && error !== null && "status" in error && Number((error as { status: unknown }).status) === 401;
}
