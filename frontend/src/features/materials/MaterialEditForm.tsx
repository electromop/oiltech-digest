import { useState, type FormEvent } from "react";
import { updateColleagueMaterial, type ColleagueMaterialPatch } from "../../api/materials";
import type { ColleagueMaterial } from "../../api/types";
import { detailText } from "../documents/documentUtils";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  material: ColleagueMaterial;
  verdicts: Array<{ value: string; label: string }>;
  onUnauthorized: () => void;
  showToast: ToastWriter;
  onSaved: (material: ColleagueMaterial) => void;
  onCancel: () => void;
};

// Правка материала из бота на платформе (решение владельца 23.10). Те же поля и проверки, что у бота: тип, файл и
// автор не меняются. Ошибку сервера показываем как есть: она называет, что поправить.
export function MaterialEditForm({ material, verdicts, onUnauthorized, showToast, onSaved, onCancel }: Props) {
  const [title, setTitle] = useState(material.title);
  const [description, setDescription] = useState(material.description);
  const [url, setUrl] = useState(material.url ?? "");
  const [companies, setCompanies] = useState(material.companies.join(", "));
  const [verdict, setVerdict] = useState(material.verdict);
  const [comment, setComment] = useState(material.verdict_comment ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const patch: ColleagueMaterialPatch = {
      title,
      description,
      companies: companies.split(/[,;\n]/).map((item) => item.trim()).filter(Boolean),
      verdict,
      verdict_comment: comment,
    };
    if (material.kind === "link") patch.url = url;
    try {
      setSaving(true);
      setError(null);
      const response = await updateColleagueMaterial(material.id, patch);
      showToast("Материал сохранён");
      onSaved(response.material);
    } catch (caught) {
      if (typeof caught === "object" && caught !== null && "status" in caught && Number((caught as { status: unknown }).status) === 401) {
        onUnauthorized();
        return;
      }
      setError(detailText(caught, "Не удалось сохранить материал"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="materialEditForm" noValidate onSubmit={(event) => void handleSubmit(event)}>
      <label className="field">
        <span>Заголовок</span>
        <input name="title" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={200} />
      </label>
      <label className="field">
        <span>Описание</span>
        <textarea name="description" value={description} onChange={(event) => setDescription(event.target.value)} rows={6} />
      </label>
      {material.kind === "link" ? (
        <label className="field">
          <span>Ссылка</span>
          <input name="url" type="url" inputMode="url" value={url} onChange={(event) => setUrl(event.target.value)} spellCheck={false} />
        </label>
      ) : null}
      <label className="field">
        <span>Компании, через запятую</span>
        <input name="companies" value={companies} onChange={(event) => setCompanies(event.target.value)} placeholder="SLB, Halliburton" />
      </label>
      <div className="materialEditGrid">
        <label className="field">
          <span>Оценка</span>
          <select name="verdict" value={verdict} onChange={(event) => setVerdict(event.target.value)}>
            {verdicts.map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Комментарий к оценке</span>
          <input name="verdict_comment" value={comment} onChange={(event) => setComment(event.target.value)} maxLength={1000} />
        </label>
      </div>
      {error ? <div className="documentAlert bad" role="alert">{error}</div> : null}
      <div className="dialogActions">
        <button type="button" className="ghostButton" onClick={onCancel} disabled={saving}>
          Отмена
        </button>
        <button type="submit" className="primaryButton" disabled={saving}>
          {saving ? "Сохраняем…" : "Сохранить"}
        </button>
      </div>
    </form>
  );
}
