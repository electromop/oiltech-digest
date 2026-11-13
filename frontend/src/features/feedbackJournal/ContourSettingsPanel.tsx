import { useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import { getContourSettings, saveContourSettings, type ContourSettingField, type ContourSettings } from "../../api/feedbackJournal";

// Настройки контура ОС (#189): все 20 настроек «Настроек контура ОС» на вкладке «Обратная связь», правит
// администратор, действуют без выката. Пороги правил и автопроверки вступают в силу вместе с правилами эксперта.

type Props = {
  onError: (error: unknown) => void;
  showToast: (text: string, tone?: "default" | "error") => void;
};

export function ContourSettingsPanel({ onError, showToast }: Props) {
  const [settings, setSettings] = useState<ContourSettings | null>(null);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);

  // Один раз при открытии: перечитывание на каждой перерисовке сбросило бы несохранённую правку.
  useEffect(() => {
    let cancelled = false;
    getContourSettings()
      .then((loaded) => {
        if (cancelled) return;
        setSettings(loaded);
        setDraft(loaded.values);
      })
      .catch(onError);
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!settings) return null;
  const changed = JSON.stringify(draft) !== JSON.stringify(settings.values);

  async function save() {
    setSaving(true);
    setRefusal(null);
    try {
      const saved = await saveContourSettings(draft);
      setSettings(saved);
      setDraft(saved.values);
      showToast("Настройки контура сохранены: действуют сразу", "default");
    } catch (error) {
      if (error instanceof ApiError && error.status === 422) setRefusal(error.message);
      else onError(error);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="panel" aria-label="Настройки контура">
      <div className="panelHeader">
        <h2>Настройки контура</h2>
      </div>
      <div className="settingsGrid">
        {settings.fields.map((field) => (
          <SettingInput key={field.key} field={field} value={draft[field.key]}
            onChange={(value) => setDraft({ ...draft, [field.key]: value })} />
        ))}
      </div>
      {refusal ? <div className="documentAlert warn" role="alert">{refusal}</div> : null}
      <div className="digestActionGroup">
        <button type="button" className="primaryButton" disabled={saving || !changed} onClick={save}>
          {saving ? "Сохраняем…" : "Сохранить настройки"}
        </button>
        <button type="button" className="ghostButton" disabled={saving || !changed} onClick={() => setDraft(settings.values)}>
          Отменить правку
        </button>
      </div>
    </section>
  );
}

function SettingInput({ field, value, onChange }: { field: ContourSettingField; value: unknown;
  onChange: (value: unknown) => void }) {
  if (field.kind === "bool") {
    return (
      <label className="checkboxRow">
        <input type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} />
        <span>{field.label}</span>
      </label>
    );
  }
  if (field.kind === "choice") {
    return (
      <label className="field">
        <span>{field.label}</span>
        <select value={String(value ?? "")} onChange={(event) => onChange(event.target.value)}>
          {field.choices.map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>)}
        </select>
      </label>
    );
  }
  if (field.kind === "int" || field.kind === "number") {
    return (
      <label className="field">
        <span>{field.label}</span>
        <input type="number" value={value === undefined || value === null ? "" : String(value)}
          min={field.low ?? undefined} max={field.high ?? undefined} step={field.kind === "int" ? 1 : 0.01}
          onChange={(event) => onChange(event.target.value === "" ? "" : Number(event.target.value))} />
      </label>
    );
  }
  return (
    <label className="field">
      <span>{field.label}</span>
      <input type={field.kind === "time" ? "time" : "text"} value={String(value ?? "")}
        onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}
