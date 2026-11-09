import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../../api/client";
import { deleteDocument, documentOriginalUrl, getDocument, listDocuments } from "../../api/documents";
import { colleagueMaterialFileUrl, deleteColleagueMaterial, listColleagueMaterials } from "../../api/materials";
import type { ColleagueMaterial, DocumentDetails, UploadedDocument } from "../../api/types";
import { ErrorBoundary } from "../../app/ErrorBoundary";
import { DocumentCardPanel } from "../documents/DocumentCardPanel";
import { detailText, formatSize, isDocumentPending, statusLabel, statusTone } from "../documents/documentUtils";
import { Dialog } from "../shared/Dialog";
import { SkeletonList } from "../shared/Skeleton";
import { MaterialEditForm } from "./MaterialEditForm";
import { MaterialUploadForm } from "./MaterialUploadForm";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
  isAdmin?: boolean;
};

// Один раздел «Материалы» (решение владельца 23.10): то, что коллеги прислали боту в Telegram, и документы,
// внесённые на платформе. У каждой записи видно, откуда она пришла и кто её прислал. Документы сервер отдаёт только
// внёсшему (изоляция пользователей, 24.07), поэтому они подписаны «Внесено вами» (ревью 24.10).
type Entry =
  | { origin: "telegram"; key: string; time: number; material: ColleagueMaterial }
  | { origin: "upload"; key: string; time: number; document: UploadedDocument };

const ORIGIN_LABELS = { telegram: "Telegram · бот", upload: "Внесено вами" } as const;
const VERDICT_TONE: Record<string, string> = {
  strong_signal: "ok",
  approved: "ok",
  watch_later: "warn",
  background_material: "warn",
};
const POLL_INTERVAL_MS = 5000;

function formatMoment(value: string | null): string {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function timeOf(value: string | null): number {
  const time = value ? Date.parse(value) : NaN;
  return Number.isNaN(time) ? 0 : time;
}

export function MaterialsPage({ onUnauthorized, showToast, isAdmin = false }: Props) {
  const [materials, setMaterials] = useState<ColleagueMaterial[]>([]);
  const [verdicts, setVerdicts] = useState<Array<{ value: string; label: string }>>([]);
  const [documents, setDocuments] = useState<UploadedDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [origin, setOrigin] = useState<"" | Entry["origin"]>("");
  const [kind, setKind] = useState("");
  const [verdict, setVerdict] = useState("");
  const [query, setQuery] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [editing, setEditing] = useState<ColleagueMaterial | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [openDocumentId, setOpenDocumentId] = useState<number | null>(null);
  const [details, setDetails] = useState<DocumentDetails | null>(null);
  const [detailsError, setDetailsError] = useState<string | null>(null);

  // Опрос: пока сервер разбирает документ; зависимость булева, иначе таймер пересоздавался бы на каждом ответе.
  const pending = documents.some(isDocumentPending);

  useEffect(() => {
    void reload(true);
  }, [isAdmin]);

  useEffect(() => {
    if (!pending) return;
    const timer = window.setInterval(() => void reloadDocuments(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [pending]);

  function unauthorized(caught: unknown): boolean {
    if (caught instanceof ApiError && caught.status === 401) {
      onUnauthorized();
      return true;
    }
    return false;
  }

  async function reload(initial = false) {
    try {
      if (initial) setLoading(true);
      // Документы вносит и видит только админ (сервер: загрузка: админу, список: своих).
      const [payload, docs] = await Promise.all([listColleagueMaterials(), isAdmin ? listDocuments() : Promise.resolve(null)]);
      setMaterials(Array.isArray(payload?.materials) ? payload.materials : []);
      setVerdicts(Array.isArray(payload?.verdicts) ? payload.verdicts : []);
      setDocuments(Array.isArray(docs?.documents) ? docs.documents : []);
      setError(null);
    } catch (caught) {
      if (unauthorized(caught)) return;
      const text = detailText(caught, "Не удалось загрузить материалы");
      setError(text);
      showToast(text, "error");
    } finally {
      if (initial) setLoading(false);
    }
  }

  async function reloadDocuments() {
    try {
      const docs = await listDocuments();
      setDocuments(Array.isArray(docs?.documents) ? docs.documents : []);
    } catch (caught) {
      unauthorized(caught);
    }
  }

  async function openDocument(documentId: number) {
    setOpenDocumentId(documentId);
    setDetails(null);
    setDetailsError(null);
    try {
      setDetails(await getDocument(documentId));
    } catch (caught) {
      if (unauthorized(caught)) return;
      setDetailsError(detailText(caught, "Не удалось открыть разбор документа"));
    }
  }

  async function remove(entry: Entry) {
    try {
      if (entry.origin === "telegram") await deleteColleagueMaterial(entry.material.id);
      else await deleteDocument(entry.document.id);
      setConfirmDelete(null);
      showToast("Материал удалён");
      await reload();
    } catch (caught) {
      if (unauthorized(caught)) return;
      showToast(detailText(caught, "Не удалось удалить материал"), "error");
    }
  }

  const entries = useMemo<Entry[]>(() => {
    const fromBot: Entry[] = materials.map((material) => ({ origin: "telegram", key: `tg-${material.id}`, time: timeOf(material.created_at), material }));
    const uploaded: Entry[] = documents.map((document) => ({ origin: "upload", key: `doc-${document.id}`, time: timeOf(document.created_at), document }));
    return [...fromBot, ...uploaded].sort((a, b) => b.time - a.time);
  }, [materials, documents]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return entries.filter((entry) => {
      if (origin && entry.origin !== origin) return false;
      if (entry.origin === "telegram") {
        const item = entry.material;
        if (kind && item.kind !== kind) return false;
        if (verdict && item.verdict !== verdict) return false;
        return !needle || [item.title, item.description, item.author, ...item.companies].some((text) => (text || "").toLowerCase().includes(needle));
      }
      if (kind && kind !== "document") return false;
      if (verdict) return false;
      const doc = entry.document;
      return !needle || [doc.filename, doc.essence, doc.publisher, doc.doc_type].some((text) => (text || "").toLowerCase().includes(needle));
    });
  }, [entries, origin, kind, verdict, query]);

  const filtered = Boolean(origin || kind || verdict || query.trim());

  function resetFilters() {
    setOrigin("");
    setKind("");
    setVerdict("");
    setQuery("");
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Материалы</h1>
          <p>
            Что коллеги прислали боту в Telegram, и документы, которые вы внесли на платформе (их видите только вы). У
            каждой записи видно, откуда она пришла.
          </p>
        </div>
        <div className="panelActions">
          <span className="statusPill">{entries.length} {materialsWord(entries.length)}</span>
          {isAdmin ? (
            <button type="button" className="primaryButton" onClick={() => setUploadOpen(true)}>
              Внести материал
            </button>
          ) : null}
        </div>
      </header>

      <p className="materialsBotHint">
        Через бота: в рабочей группе Telegram или в личке бота команда <strong>/add</strong> (тип, заголовок, описание,
        компании, оценка); формат и шкала <strong>/help</strong>, свои последние <strong>/my</strong>.
      </p>

      <section className="panel">
        <div className="panelHeader">
          <h2>Все материалы</h2>
          <button type="button" className="ghostButton compactButton" onClick={() => void reload()} disabled={loading}>
            Обновить
          </button>
        </div>

        <div className="materialsFilters">
          <div className="field">
            <span id="materials-origin-label">Откуда</span>
            <div className="viewTabs" role="group" aria-labelledby="materials-origin-label">
              {([["", "Все"], ["telegram", "Telegram"], ["upload", "Внесено вами"]] as const).map(([value, label]) => (
                <button
                  key={value || "all"}
                  type="button"
                  className={origin === value ? "primaryButton compactButton" : "ghostButton compactButton"}
                  aria-pressed={origin === value}
                  onClick={() => setOrigin(value)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <label className="field">
            <span>Тип</span>
            <select value={kind} onChange={(event) => setKind(event.target.value)}>
              <option value="">Все типы</option>
              <option value="link">Ссылка</option>
              <option value="file">Файл</option>
              <option value="event">Событие</option>
              <option value="document">Документ с разбором</option>
            </select>
          </label>
          <label className="field">
            <span>Оценка автора</span>
            <select value={verdict} onChange={(event) => setVerdict(event.target.value)}>
              <option value="">Все оценки</option>
              {verdicts.map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Поиск</span>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Заголовок, компания, автор" />
          </label>
        </div>

        {error ? <div className="documentAlert bad" role="alert">{error}</div> : null}
        {loading ? <SkeletonList rows={3} label="Загружаем материалы" /> : null}
        {!loading && !error && entries.length === 0 ? (
          <div className="emptyState">
            <strong>Материалов пока нет</strong>
            <span>Коллеги присылают их боту командой /add{isAdmin ? "; документ можно внести кнопкой «Внести материал»" : ""}.</span>
          </div>
        ) : null}
        {!loading && entries.length > 0 && visible.length === 0 ? (
          <div className="emptyState">
            <span>Под фильтры ничего не подходит.</span>
            {filtered ? <button type="button" className="ghostButton compactButton" onClick={resetFilters}>Сбросить фильтры</button> : null}
          </div>
        ) : null}

        <div className="materialsList">
          {visible.map((entry) => (
            <article className={`materialCard ${entry.origin}`} key={entry.key}>
              {entry.origin === "telegram" ? (
                <TelegramEntry material={entry.material} />
              ) : (
                <UploadEntry document={entry.document} />
              )}
              {isAdmin ? (
                <div className="materialActions">
                  {entry.origin === "telegram" ? (
                    <button type="button" className="ghostButton compactButton" onClick={() => setEditing(entry.material)}>
                      Править
                    </button>
                  ) : (
                    <>
                      <button type="button" className="ghostButton compactButton" onClick={() => void openDocument(entry.document.id)}>
                        Открыть разбор
                      </button>
                      <a className="ghostButton compactButton" href={documentOriginalUrl(entry.document.id)} download>
                        Скачать оригинал
                      </a>
                    </>
                  )}
                  {confirmDelete === entry.key ? (
                    <>
                      <span className="metaText">Удалить безвозвратно?</span>
                      <button type="button" className="dangerButton compactButton" onClick={() => void remove(entry)}>
                        Да, удалить
                      </button>
                      <button type="button" className="ghostButton compactButton" onClick={() => setConfirmDelete(null)}>
                        Отмена
                      </button>
                    </>
                  ) : (
                    <button type="button" className="dangerButton compactButton" onClick={() => setConfirmDelete(entry.key)}>
                      Удалить
                    </button>
                  )}
                </div>
              ) : null}
            </article>
          ))}
        </div>
      </section>

      <Dialog open={uploadOpen} title="Внести материал" onClose={() => setUploadOpen(false)}>
        <MaterialUploadForm
          onUnauthorized={onUnauthorized}
          showToast={showToast}
          onUploaded={(documentId) => {
            setUploadOpen(false);
            void reload();
            if (documentId) void openDocument(documentId);
          }}
        />
      </Dialog>

      <Dialog open={editing !== null} title="Править материал" onClose={() => setEditing(null)}>
        {editing ? (
          <MaterialEditForm
            material={editing}
            verdicts={verdicts}
            onUnauthorized={onUnauthorized}
            showToast={showToast}
            onCancel={() => setEditing(null)}
            onSaved={(saved) => {
              setMaterials((current) => current.map((item) => (item.id === saved.id ? saved : item)));
              setEditing(null);
            }}
          />
        ) : null}
      </Dialog>

      <Dialog open={openDocumentId !== null} title="Разбор документа" wide onClose={() => setOpenDocumentId(null)}>
        {/* key на границе: открытие другого документа монтирует её заново, иначе один упавший документ оставил бы
            разбор сломанным навсегда. */}
        <ErrorBoundary key={openDocumentId ?? 0} title="Разбор документа отобразить не удалось">
          {detailsError ? (
            <div className="documentAlert bad">{detailsError}</div>
          ) : details ? (
            <DocumentCardPanel details={details} />
          ) : (
            <SkeletonList rows={2} label="Открываем разбор" />
          )}
        </ErrorBoundary>
      </Dialog>
    </section>
  );
}

function TelegramEntry({ material }: { material: ColleagueMaterial }) {
  return (
    <>
      <div className="materialCardTop">
        <span className="materialOrigin telegram">{ORIGIN_LABELS.telegram}</span>
        <span className="miniPill muted">{material.kind_label}</span>
        {/* На карточке: коротко («Сильный сигнал»), полностью: в подсказке и в подокне правки. */}
        <span className={`miniPill ${VERDICT_TONE[material.verdict] ?? "muted"}`} title={material.verdict_label}>
          {material.verdict_label.split(" — ")[0]}
        </span>
        <span className="metaText">№{material.id} · {formatMoment(material.created_at)}</span>
      </div>
      <h3>{material.title}</h3>
      <p className="materialDescription">{material.description}</p>
      <dl className="materialMeta">
        <div><dt>Прислал</dt><dd>{material.author}{material.author_username ? ` (@${material.author_username})` : ""}</dd></div>
        {material.companies.length ? <div><dt>Компании</dt><dd>{material.companies.join(", ")}</dd></div> : null}
        {material.url ? <div><dt>Ссылка</dt><dd><a href={material.url} target="_blank" rel="noreferrer noopener">{material.url}</a></dd></div> : null}
        {material.has_file ? (
          <div><dt>Файл</dt><dd><a href={colleagueMaterialFileUrl(material.id)}>{material.file_name}{material.file_size ? ` · ${formatSize(material.file_size)}` : ""}</a></dd></div>
        ) : null}
        {material.verdict_comment ? <div><dt>Комментарий</dt><dd>{material.verdict_comment}</dd></div> : null}
        <div><dt>Статус</dt><dd>{material.status_label}</dd></div>
      </dl>
    </>
  );
}

function UploadEntry({ document }: { document: UploadedDocument }) {
  return (
    <>
      <div className="materialCardTop">
        <span className="materialOrigin upload">{ORIGIN_LABELS.upload}</span>
        <span className="miniPill muted">Документ {(document.kind || "").toUpperCase()}</span>
        <span className={`miniPill ${statusTone(String(document.status))}`}>{statusLabel(String(document.status))}</span>
        <span className="metaText">№{document.id} · {formatMoment(document.created_at)}</span>
      </div>
      <h3>{document.filename}</h3>
      {document.essence ? <p className="materialDescription">{document.essence}</p> : null}
      {document.error_message ? <div className="documentAlert bad">{document.error_message}</div> : null}
      <dl className="materialMeta">
        {document.doc_type ? <div><dt>Тип документа</dt><dd>{document.doc_type}</dd></div> : null}
        {document.publisher ? <div><dt>Издатель</dt><dd>{document.publisher}</dd></div> : null}
        <div><dt>Размер</dt><dd>{formatSize(document.size_bytes)}</dd></div>
        {document.fact_count != null ? <div><dt>Фактов</dt><dd>{document.fact_count}</dd></div> : null}
      </dl>
    </>
  );
}

function materialsWord(count: number): string {
  const tens = count % 100;
  const units = count % 10;
  if (units === 1 && tens !== 11) return "материал";
  if (units >= 2 && units <= 4 && (tens < 12 || tens > 14)) return "материала";
  return "материалов";
}
