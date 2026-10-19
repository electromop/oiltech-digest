import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../../api/client";
import { colleagueMaterialFileUrl, listColleagueMaterials } from "../../api/materials";
import type { ColleagueMaterial } from "../../api/types";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

const KIND_ICONS: Record<ColleagueMaterial["kind"], string> = { link: "🔗", file: "📎", event: "📰" };

// Тон оценки — как читает её аналитик: в работу, держать в поле зрения, мимо.
const VERDICT_TONE: Record<string, string> = {
  strong_signal: "strong",
  approved: "good",
  watch_later: "watch",
  background_material: "watch",
};

function formatSize(bytes: number | null): string {
  if (!bytes) return "";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1).replace(".", ",")} МБ`;
}

function formatMoment(value: string | null): string {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function ColleagueMaterialsPage({ onUnauthorized, showToast }: Props) {
  const [materials, setMaterials] = useState<ColleagueMaterial[]>([]);
  const [verdicts, setVerdicts] = useState<Array<{ value: string; label: string }>>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [verdict, setVerdict] = useState("");
  const [kind, setKind] = useState("");
  const [query, setQuery] = useState("");

  useEffect(() => {
    void reload();
  }, []);

  async function reload() {
    try {
      setLoading(true);
      const payload = await listColleagueMaterials();
      setMaterials(Array.isArray(payload?.materials) ? payload.materials : []);
      setVerdicts(Array.isArray(payload?.verdicts) ? payload.verdicts : []);
      setError(null);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        onUnauthorized();
        return;
      }
      const text = caught instanceof Error ? caught.message : "Не удалось загрузить материалы";
      setError(text);
      showToast(text, "error");
    } finally {
      setLoading(false);
    }
  }

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return materials.filter((item) => {
      if (verdict && item.verdict !== verdict) return false;
      if (kind && item.kind !== kind) return false;
      if (!needle) return true;
      return [item.title, item.description, item.author, ...item.companies].some((text) => text.toLowerCase().includes(needle));
    });
  }, [materials, verdict, kind, query]);

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Материалы коллег</h1>
          <p>
            Что сотрудники прислали боту в рабочей группе Telegram: ссылки, файлы и события с упором на бизнес —
            с оценкой автора по шкале платформы.
          </p>
        </div>
        <div className="statusPill">{materials.length} материалов</div>
      </header>

      <section className="panel">
        <div className="panelHeader">
          <h2>Как внести материал</h2>
        </div>
        <p className="metaText" style={{ margin: 0 }}>
          В рабочей группе Telegram или в личке бота — команда <strong>/add</strong>: тип (ссылка, файл, событие), заголовок,
          описание, компании и ваша оценка. Перед отправкой бот покажет карточку целиком. Формат и шкала — <strong>/help</strong>,
          свои последние материалы — <strong>/my</strong>.
        </p>
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Поступившее</h2>
          <button type="button" className="ghostButton compactButton" onClick={() => void reload()} disabled={loading}>
            {loading ? "Обновляем…" : "Обновить"}
          </button>
        </div>

        <div className="materialsFilters">
          <label className="field">
            <span>Оценка</span>
            <select value={verdict} onChange={(event) => setVerdict(event.target.value)}>
              <option value="">Все оценки</option>
              {verdicts.map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Тип</span>
            <select value={kind} onChange={(event) => setKind(event.target.value)}>
              <option value="">Все типы</option>
              <option value="link">🔗 Ссылка</option>
              <option value="file">📎 Файл</option>
              <option value="event">📰 Событие</option>
            </select>
          </label>
          <label className="field">
            <span>Поиск</span>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Заголовок, компания, автор" />
          </label>
        </div>

        {error ? <div className="documentAlert bad" role="alert">{error}</div> : null}
        {!loading && !error && materials.length === 0 ? (
          <div className="emptyState">
            <strong>Материалов пока нет</strong>
            <span>Первый появится здесь, как только кто-то из коллег отправит его боту командой /add.</span>
          </div>
        ) : null}
        {!loading && materials.length > 0 && visible.length === 0 ? (
          <div className="emptyState"><span>Под фильтры ничего не подходит.</span></div>
        ) : null}

        <div className="materialsList">
          {visible.map((item) => (
            <article className="materialCard" key={item.id}>
              <div className="materialCardTop">
                <span className="badge">{KIND_ICONS[item.kind] ?? "•"} {item.kind_label}</span>
                <span className={`materialVerdict ${VERDICT_TONE[item.verdict] ?? "low"}`}>{item.verdict_label}</span>
                <span className="metaText">№{item.id} · {formatMoment(item.created_at)}</span>
              </div>
              <h3>{item.title}</h3>
              <p className="materialDescription">{item.description}</p>
              <div className="materialMeta">
                {item.url ? (
                  <a href={item.url} target="_blank" rel="noreferrer noopener">🔗 {item.url}</a>
                ) : null}
                {item.has_file ? (
                  <a href={colleagueMaterialFileUrl(item.id)}>📎 {item.file_name}{item.file_size ? ` · ${formatSize(item.file_size)}` : ""}</a>
                ) : null}
                {item.companies.length ? <span>🏢 {item.companies.join(", ")}</span> : null}
                {item.verdict_comment ? <span>💬 {item.verdict_comment}</span> : null}
                <span>👤 {item.author}{item.author_username ? ` (@${item.author_username})` : ""}</span>
              </div>
            </article>
          ))}
        </div>
      </section>
    </section>
  );
}
