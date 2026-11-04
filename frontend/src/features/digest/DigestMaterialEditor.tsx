import { useState } from "react";
import type { DigestMaterial, IssueMaterialPatch } from "../../api/types";
import { hypothesisAuthorship } from "../signals/SignalHypothesis";

type Props = {
  material: DigestMaterial;
  busy: boolean;
  onSave: (patch: IssueMaterialPatch) => void;
  onCancel: () => void;
};

// Чего не хватает материалу до выпуска (#162): коды сервера (digest_materials) словами.
const MISSING_LABELS: Record<DigestMaterial["missing"][number], string> = {
  hypothesis: "нет гипотезы",
  criteria: "нет баллов по критериям",
  relevance: "связь с нефтесервисом не определена",
  picture: "нет своей картинки",
};

export function MissingLine({ material }: { material?: DigestMaterial }) {
  if (!material?.missing.length) return null;
  return (
    <div className="digestMissing" role="note">
      Не хватает: {material.missing.map((code) => MISSING_LABELS[code]).join(", ")}
    </div>
  );
}

// Правка материала выпуска на экране «Дайджест» (#162, ADR 0006): заголовок, суть, подпись, «Гипотеза для Компании»
// и картинка. Правка ложится на сигнал материала: витрина и выпуск показывают один текст. Уходит только изменённое.
export function DigestMaterialEditor({ material, busy, onSave, onCancel }: Props) {
  const [title, setTitle] = useState(material.title);
  const [summary, setSummary] = useState(material.summary);
  const [label, setLabel] = useState(material.label === material.label_default ? "" : material.label);
  const [hypothesis, setHypothesis] = useState(material.hypothesis ?? "");
  const [picture, setPicture] = useState(material.picture);
  const ownLabel = material.label === material.label_default ? "" : material.label;

  function changes(): IssueMaterialPatch {
    const patch: IssueMaterialPatch = {};
    if (title.trim() && title.trim() !== material.title) patch.title = title.trim();
    if (summary.trim() && summary.trim() !== material.summary) patch.summary = summary.trim();
    if (label.trim() !== ownLabel) patch.label = label.trim();
    if (hypothesis.trim() !== (material.hypothesis ?? "")) patch.hypothesis = hypothesis.trim();
    if (picture !== material.picture) patch.picture = picture;
    return patch;
  }

  const authorship = hypothesisAuthorship(material.hypothesis_set_by, material.hypothesis_author, material.hypothesis_set_at);
  const patch = changes();
  return (
    <div className="digestMaterialEditor" aria-label="Правка материала">
      <label className="field">
        <span>Заголовок</span>
        <input value={title} maxLength={500} onChange={(event) => setTitle(event.target.value)} />
      </label>
      <label className="field">
        <span>Суть</span>
        <textarea rows={4} value={summary} maxLength={6000} onChange={(event) => setSummary(event.target.value)} />
      </label>
      <label className="field">
        <span>Подпись после рубрики</span>
        <input
          value={label}
          maxLength={200}
          placeholder={material.label_default ? `по типу события: ${material.label_default}` : "подписи по типу события нет"}
          onChange={(event) => setLabel(event.target.value)}
        />
      </label>
      <label className="field">
        <span>Гипотеза для Компании{authorship ? ` · ${authorship}` : ""}</span>
        <textarea
          rows={2}
          value={hypothesis}
          maxLength={1000}
          placeholder="Одно предложение: «Оценить…», «Рассмотреть…» или «Изучить…»"
          onChange={(event) => setHypothesis(event.target.value)}
        />
      </label>
      <fieldset className="digestPictureChoice">
        <legend>Картинка в карточке</legend>
        <label>
          <input type="radio" name={`picture-${material.material_signal_id}`} checked={picture === "own"}
            disabled={!material.image_own} onChange={() => setPicture("own")} />
          {material.image_own ? "своя картинка публикации" : "своей картинки нет"}
        </label>
        <label>
          <input type="radio" name={`picture-${material.material_signal_id}`} checked={picture === "placeholder"}
            onChange={() => setPicture("placeholder")} />
          заглушка темы
        </label>
      </fieldset>
      <div className="signalFeedbackButtons">
        <button type="button" className="ghostButton compactButton" disabled={busy} onClick={onCancel}>
          Отмена
        </button>
        <button type="button" className="primaryButton compactButton" disabled={busy || !Object.keys(patch).length}
          onClick={() => onSave(patch)}>
          Сохранить
        </button>
      </div>
    </div>
  );
}
