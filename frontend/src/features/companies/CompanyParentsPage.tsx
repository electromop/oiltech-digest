import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../../api/client";
import {
  addCompanyParent,
  deleteCompanyParent,
  importCompanyParents,
  listCompanyParents,
  type CompanyParent,
  type CompanyParentsImport,
} from "../../api/companyParents";
import { getSignalDictionaries } from "../../api/signals";
import styles from "./CompanyParents.module.css";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

// Пределы сервера (company_parents.py): длина имени и примечания, строк во вставке.
const MAX_NAME = 200;
const MAX_NOTE = 500;
const MAX_LINES = 500;
const NAMES_LIST_ID = "company-parents-names";

function pluralRu(count: number, one: string, few: string, many: string) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return `${count} ${one}`;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${count} ${few}`;
  return `${count} ${many}`;
}

function formatMoment(value: string | null): string {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

// Экран «Дочерние общества» (решение владельца 20.10): список «дочка → мать» для фильтра «всё про компанию».
// Правят сами, без выката: форма по одной связи и вставка списком (так заносится черновик). Проверки на
// сервере, одни для формы и вставки: дочка не мать сама себе, у дочки одна мать, без циклов.
export function CompanyParentsPage({ onUnauthorized, showToast }: Props) {
  const [pairs, setPairs] = useState<CompanyParent[]>([]);
  const [participants, setParticipants] = useState<string[]>([]);
  const [companies, setCompanies] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [child, setChild] = useState("");
  const [parent, setParent] = useState("");
  const [note, setNote] = useState("");
  const [refusal, setRefusal] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [text, setText] = useState("");
  const [analysis, setAnalysis] = useState<CompanyParentsImport | null>(null);

  useEffect(() => {
    void reload();
    void loadCompanies();
  }, []);

  function handleError(error: unknown, fallback: string) {
    if (error instanceof ApiError && error.status === 401) {
      onUnauthorized();
      return;
    }
    showToast(error instanceof Error && error.message ? error.message : fallback, "error");
  }

  async function reload() {
    try {
      setLoading(true);
      const list = await listCompanyParents();
      setPairs(list.pairs);
      setParticipants(list.names);
    } catch (error) {
      handleError(error, "Не удалось загрузить список");
    } finally {
      setLoading(false);
    }
  }

  // Подсказки к полям — компании справочника карточки; не загрузились — форма работает и без них.
  async function loadCompanies() {
    try {
      setCompanies((await getSignalDictionaries()).companies ?? []);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onUnauthorized();
    }
  }

  // Подсказки без повторов: справочник, имена участников карточек (черновик заносят по ним) и уже введённые
  // имена (мать часто уже есть в списке дочкой другой матери).
  const names = useMemo(() => {
    const listed = pairs.flatMap((pair) => [pair.child_name, pair.parent_name]);
    const all = new Set([...companies, ...participants, ...listed]);
    return [...all].sort((one, other) => one.localeCompare(other, "ru"));
  }, [companies, participants, pairs]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return pairs;
    return pairs.filter((pair) =>
      [pair.child_name, pair.parent_name, pair.note ?? ""].some((value) => value.toLowerCase().includes(needle)),
    );
  }, [pairs, query]);

  async function handleAdd() {
    if (!child.trim() || !parent.trim()) {
      setRefusal("Укажите дочку и мать");
      return;
    }
    try {
      setBusy(true);
      await addCompanyParent({ child_name: child, parent_name: parent, note: note.trim() || null });
      setChild("");
      setParent("");
      setNote("");
      setRefusal(null);
      showToast("Связь добавлена");
      await reload();
    } catch (error) {
      // Отказ проверки (у дочки уже есть мать, цикл) — под формой: его надо прочитать, тост исчезнет.
      if (error instanceof ApiError && error.status === 400) setRefusal(error.message);
      else handleError(error, "Не удалось добавить связь");
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(pair: CompanyParent) {
    if (!window.confirm(`Удалить связь «${pair.child_name}» → «${pair.parent_name}»?`)) return;
    try {
      setBusy(true);
      await deleteCompanyParent(pair.id);
      showToast("Связь удалена");
      await reload();
    } catch (error) {
      handleError(error, "Не удалось удалить связь");
    } finally {
      setBusy(false);
    }
  }

  async function handleImport(apply: boolean) {
    if (!text.trim()) {
      showToast("Вставьте строки «дочка; мать»", "error");
      return;
    }
    try {
      setBusy(true);
      const result = await importCompanyParents(text, apply);
      if (result.applied) {
        setText("");
        setAnalysis(null);
        showToast(`Записано связей: ${result.lines.length}`);
        await reload();
        return;
      }
      setAnalysis(result);
      if (apply) showToast(`Ничего не записано: строк с ошибками ${result.problems}`, "error");
    } catch (error) {
      handleError(error, apply ? "Не удалось сохранить список" : "Не удалось проверить список");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Дочерние общества</h1>
          <p>
            Список «дочка → мать» для фильтра «всё про компанию»: компания показывается вместе с дочерними обществами по
            цепочке. Форму собственности, регистр и падеж сверка учитывает сама; другое написание (латиница, прежнее
            название) добавьте отдельной строкой.
          </p>
        </div>
        <div className="statusPill">{pluralRu(pairs.length, "связь", "связи", "связей")}</div>
      </header>

      <datalist id={NAMES_LIST_ID}>
        {names.map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>

      <section className="panel">
        <div className="panelHeader">
          <h2>Добавить связь</h2>
        </div>
        <div className={styles.addGrid}>
          <label className="field">
            <span>Дочка</span>
            <input
              list={NAMES_LIST_ID}
              value={child}
              maxLength={MAX_NAME}
              placeholder="Газпромнефть-Заполярье"
              onChange={(event) => {
                setChild(event.target.value);
                setRefusal(null);
              }}
            />
          </label>
          <label className="field">
            <span>Мать</span>
            <input
              list={NAMES_LIST_ID}
              value={parent}
              maxLength={MAX_NAME}
              placeholder="Газпром нефть"
              onChange={(event) => {
                setParent(event.target.value);
                setRefusal(null);
              }}
            />
          </label>
          <label className="field">
            <span>Примечание</span>
            <input
              value={note}
              maxLength={MAX_NOTE}
              placeholder="Необязательно: доля, источник"
              onChange={(event) => setNote(event.target.value)}
            />
          </label>
          <button type="button" className="primaryButton" disabled={busy} onClick={() => void handleAdd()}>
            Добавить
          </button>
        </div>
        {refusal ? <div className="documentAlert bad" role="alert">{refusal}</div> : null}
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Список</h2>
          <label className="field compactField">
            <span>Найти</span>
            <input
              value={query}
              placeholder="Компания или примечание"
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
        </div>
        {loading ? (
          <div className="emptyState">Загружаем список…</div>
        ) : !pairs.length ? (
          <div className="emptyState">
            <strong>Список пуст</strong>
            <span>Добавьте связь формой выше или вставьте черновик списком ниже.</span>
          </div>
        ) : !visible.length ? (
          <div className="emptyState"><span>Ничего не нашлось.</span></div>
        ) : (
          <div className="jobsTableWrap">
            <table className={`jobsTable ${styles.table}`}>
              <thead>
                <tr>
                  <th>Дочка</th>
                  <th>Мать</th>
                  <th>Примечание</th>
                  <th>Кто</th>
                  <th>Когда</th>
                  <th aria-label="Действия" />
                </tr>
              </thead>
              <tbody>
                {visible.map((pair) => (
                  <tr key={pair.id}>
                    <td className={styles.name}>{pair.child_name}</td>
                    <td className={styles.name}>
                      <span className={styles.arrow} aria-hidden="true">→</span>
                      {pair.parent_name}
                    </td>
                    <td>{pair.note ?? ""}</td>
                    <td>{pair.created_by_email ?? ""}</td>
                    <td className={styles.moment}>{formatMoment(pair.created_at)}</td>
                    <td>
                      <button
                        type="button"
                        className="ghostButton dangerButton compactButton"
                        disabled={busy}
                        aria-label={`Удалить связь «${pair.child_name}» → «${pair.parent_name}»`}
                        onClick={() => void handleDelete(pair)}
                      >
                        Удалить
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Вставить списком</h2>
        </div>
        <p className={`metaText ${styles.hint}`}>
          Строка: «дочка; мать», третьим полем можно примечание; до {MAX_LINES} строк. «Проверить» покажет разбор и
          ничего не запишет. «Сохранить» запишет все строки разом и только если ни в одной нет ошибки.
        </p>
        <label className="field">
          <span>Строки «дочка; мать»</span>
          <textarea
            className={styles.paste}
            value={text}
            placeholder={"Газпром нефть; Газпром\nГазпромнефть-Заполярье; Газпром нефть; контроль 100%"}
            onChange={(event) => {
              setText(event.target.value);
              setAnalysis(null);
            }}
          />
        </label>
        <div className={`settingsActions ${styles.pasteActions}`}>
          <button type="button" className="ghostButton" disabled={busy} onClick={() => void handleImport(false)}>
            Проверить
          </button>
          <button type="button" className="primaryButton" disabled={busy} onClick={() => void handleImport(true)}>
            Сохранить
          </button>
        </div>
        {analysis ? <ImportAnalysis analysis={analysis} /> : null}
      </section>
    </section>
  );
}

function ImportAnalysis({ analysis }: { analysis: CompanyParentsImport }) {
  const total = analysis.lines.length;
  return (
    <section className={styles.analysis} aria-label="Разбор строк">
      <div className={analysis.problems ? "documentAlert bad" : `documentAlert ${styles.fineAlert}`} role="status">
        {analysis.problems
          ? `Строк: ${total}, с ошибками: ${analysis.problems}. Исправьте их в поле выше и проверьте снова.`
          : `Ошибок нет, можно сохранить: ${pluralRu(total, "строка", "строки", "строк")}.`}
      </div>
      <div className={`jobsTableWrap ${styles.analysisRows}`}>
        <table className={`jobsTable ${styles.table}`}>
          <thead>
            <tr>
              <th>Строка</th>
              <th>Дочка</th>
              <th>Мать</th>
              <th>Проверка</th>
            </tr>
          </thead>
          <tbody>
            {analysis.lines.map((line) => (
              <tr key={line.line} className={line.problem ? styles.problemRow : undefined}>
                <td>{line.line}</td>
                <td className={styles.name}>{line.child_name}</td>
                <td className={styles.name}>{line.parent_name}</td>
                <td className={line.problem ? styles.problem : styles.fine}>{line.problem ?? "в порядке"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
