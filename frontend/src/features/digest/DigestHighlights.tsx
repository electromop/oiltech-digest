import { useEffect, useState } from "react";
import type { DigestHighlight, DigestSection } from "../../api/types";

type Props = {
  sections: DigestSection[];
  highlights: DigestHighlight[];
  readOnly: boolean;
  busy: boolean;
  onSave: (section: DigestSection["key"], text: string) => void;
};

function formatDay(value: string | null): string {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "Europe/Moscow" });
}

// «Главное в выпуске» на обложке PDF (#162): строка на раздел месяца, пишет человек. Пока не написано или стёрто —
// перечень рубрик раздела (решение владельца 04.11). Раздел без материалов строки не даёт. В закрытом месяце — только
// просмотр.
export function DigestHighlights({ sections, highlights, readOnly, busy, onSave }: Props) {
  const filled = sections.filter((section) => section.materials.length);
  const byKey = new Map(highlights.map((row) => [row.key, row]));
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  useEffect(() => {
    setDrafts(Object.fromEntries(highlights.map((row) => [row.key, row.edited ? row.text : ""])));
  }, [highlights]);
  if (!filled.length) return null;
  return (
    <section className="digestHighlights" aria-label="Главное в выпуске">
      <strong>Главное в выпуске</strong>
      {filled.map((section) => {
        const row = byKey.get(section.key);
        const draft = drafts[section.key] ?? "";
        const saved = row?.edited ? row.text : "";
        return (
          <div className="digestHighlightRow" key={section.key}>
            {readOnly ? (
              <p><b>{section.title}.</b> {row?.text || "—"}</p>
            ) : (
              <>
                <label className="field">
                  <span>{section.title}</span>
                  <textarea
                    rows={2}
                    value={draft}
                    maxLength={600}
                    placeholder={row?.default ? `по умолчанию: ${row.default}` : "по умолчанию строки нет"}
                    onChange={(event) => setDrafts((current) => ({ ...current, [section.key]: event.target.value }))}
                  />
                </label>
                <div className="digestHighlightMeta">
                  <span className="metaText">
                    {row?.edited
                      ? `Написал ${row.edited_by || "учётка удалена"}, ${formatDay(row.edited_at)}`
                      : "Сейчас на обложке перечень рубрик раздела."}
                  </span>
                  <button type="button" className="ghostButton compactButton" disabled={busy || draft.trim() === saved}
                    onClick={() => onSave(section.key, draft.trim())}>
                    {draft.trim() || !saved ? "Сохранить" : "Вернуть перечень рубрик"}
                  </button>
                </div>
              </>
            )}
          </div>
        );
      })}
    </section>
  );
}
