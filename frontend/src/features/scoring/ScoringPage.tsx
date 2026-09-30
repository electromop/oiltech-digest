import { useEffect, useMemo, useState } from "react";
import { deleteScoringCriterion, listScoringCriteria, saveScoringCriteria } from "../../api/scoring";
import type { ScoringCriterion, ScoringProfile } from "../../api/types";
import { mergeKeywords } from "../tags/KeywordChips";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

// Два набора критериев (сессия G, ADR 0002) — вкладками, чтобы не засорять экран
// (документ заказчика 19.09). У каждой вкладки свой список, своя сумма весов и своё «Сохранить».
const PROFILES: ReadonlyArray<{ id: ScoringProfile; label: string }> = [
  { id: "business", label: "Бизнес-сигналы" },
  { id: "tech_radar", label: "Технологический радар" },
];

export function ScoringPage({ onUnauthorized, showToast }: Props) {
  const [profile, setProfile] = useState<ScoringProfile>("business");
  const [criteria, setCriteria] = useState<ScoringCriterion[]>([]);
  // Как в базе — по нему видно несохранённые правки вкладки.
  const [stored, setStored] = useState<ScoringCriterion[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // Ответ по вкладке, с которой уже ушли, не должен лечь в список новой.
    let cancelled = false;
    setLoading(true);
    listScoringCriteria(profile)
      .then((rows) => {
        if (cancelled) return;
        setCriteria(rows);
        setStored(rows);
      })
      .catch((error: unknown) => {
        if (!cancelled) handleError(error, "Не удалось загрузить критерии");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [profile]);

  function handleError(error: unknown, fallback: string) {
    const status = typeof error === "object" && error && "status" in error ? Number(error.status) : 0;
    const message = error instanceof Error ? error.message : fallback;
    if (status === 401) {
      onUnauthorized();
      return;
    }
    showToast(message || fallback, "error");
  }

  const totalWeight = useMemo(() => criteria.reduce((sum, item) => sum + Number(item.weight || 0), 0), [criteria]);
  const dirty = useMemo(() => JSON.stringify(criteria) !== JSON.stringify(stored), [criteria, stored]);
  const activeLabel = PROFILES.find((item) => item.id === profile)?.label ?? profile;

  function switchProfile(next: ScoringProfile) {
    if (next === profile) return;
    // Правки не переносятся между вкладками и не хранятся в фоне: уход с вкладки их сбрасывает.
    if (dirty && !window.confirm(`На вкладке «${activeLabel}» есть несохранённые правки. Перейти без сохранения?`)) {
      return;
    }
    setCriteria([]);
    setStored([]);
    setProfile(next);
  }

  function updateCriterion(index: number, field: keyof ScoringCriterion, value: string | number | string[]) {
    setCriteria((prev) =>
      prev.map((item, currentIndex) => {
        if (currentIndex !== index) return item;
        return { ...item, [field]: value };
      }),
    );
  }

  function addCriterion() {
    setCriteria((prev) => [
      ...prev,
      {
        id: null,
        name: "Новый критерий",
        description: "",
        weight: 0,
        keywords_json: [],
        keywords_en_json: [],
        sort_order: (prev.length + 1) * 10,
      },
    ]);
  }

  function normalizeWeights() {
    const count = criteria.length;
    if (!count) return;
    const each = Math.floor(100 / count);
    let rest = 100;
    setCriteria((prev) =>
      prev.map((item, index) => {
        const weight = index === count - 1 ? rest : each;
        rest -= each;
        return { ...item, weight };
      }),
    );
    showToast("Веса нормализованы до 100%");
  }

  async function removeCriterion(index: number) {
    const item = criteria[index];
    if (item.id) {
      try {
        setBusy(true);
        await deleteScoringCriterion(item.id);
      } catch (error) {
        handleError(error, "Не удалось удалить критерий");
        return;
      } finally {
        setBusy(false);
      }
      // Удаление уже в базе — это не несохранённая правка.
      setStored((prev) => prev.filter((row) => row.id !== item.id));
    }
    setCriteria((prev) => prev.filter((_, currentIndex) => currentIndex !== index));
  }

  async function handleSave() {
    const target = profile;
    try {
      setBusy(true);
      await saveScoringCriteria(
        criteria.map((item, index) => ({ ...item, sort_order: item.sort_order || (index + 1) * 10 })),
        target,
      );
      showToast(`Скоринг «${activeLabel}» сохранён`);
      const rows = await listScoringCriteria(target);
      setCriteria(rows);
      setStored(rows);
    } catch (error) {
      handleError(error, "Не удалось сохранить критерии");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Скоринг</h1>
        </div>
        <div className={`statusPill ${totalWeight !== 100 ? "warningPill" : ""}`}>Сумма весов: {totalWeight}%</div>
      </header>

      <section className="panel">
        {busy ? <InlineLoader label="Сохраняем скоринг…" /> : null}
        <div className="scoringProfileTabs" role="tablist" aria-label="Набор критериев">
          {PROFILES.map((item) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              id={`scoring-tab-${item.id}`}
              aria-selected={profile === item.id}
              aria-controls="scoring-profile-panel"
              className={profile === item.id ? "primaryButton" : "ghostButton"}
              onClick={() => switchProfile(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>

        <div id="scoring-profile-panel" role="tabpanel" aria-labelledby={`scoring-tab-${profile}`}>
          <div className="panelHeader settingsHeader">
            <h2>Критерии оценки</h2>
            <div className="settingsActions">
              <button type="button" className="ghostButton" onClick={normalizeWeights}>
                Нормализовать
              </button>
              <button type="button" className="primaryButton" disabled={totalWeight !== 100} onClick={() => void handleSave()}>
                Сохранить
              </button>
            </div>
          </div>

          {profile === "tech_radar" ? (
            // Вариант Б1 ADR 0002: набор хранится и правится, но оценку радара пока ставит его судья.
            <div className="archiveNotice scoringProfileNotice" role="status">
              <span>
                Набор «Технологический радар» хранится и правится здесь, но в оценку сигналов радара пока не
                входит: её подключим отдельным шагом. Статьи ленты оцениваются набором «Бизнес-сигналы».
              </span>
            </div>
          ) : null}

          {loading ? (
            <div className="emptyState"><LoadingState label="Загружаем критерии…" /></div>
          ) : (
            <div className="settingsStack">
              {criteria.map((criterion, index) => (
                <div className="settingsCard" key={criterion.id ?? `new-${index}`}>
                  <div className="settingsGrid">
                    <label className="field">
                      <span>Параметр</span>
                      <input value={criterion.name} onChange={(event) => updateCriterion(index, "name", event.target.value)} />
                    </label>
                    <label className="field">
                      <span>Вес</span>
                      <input
                        type="number"
                        min={0}
                        max={100}
                        value={criterion.weight}
                        onChange={(event) => updateCriterion(index, "weight", Number(event.target.value || 0))}
                      />
                    </label>
                    <label className="field fieldWide">
                      <span>Описание для ИИ</span>
                      <textarea
                        value={criterion.description || ""}
                        onChange={(event) => updateCriterion(index, "description", event.target.value)}
                      />
                    </label>
                    {/* RU и EN — друг под другом во всю ширину (документ заказчика 19.09):
                        рядом по 240 px длинный список ключей читался только прокруткой. */}
                    <KeywordsField
                      label="Ключевые слова RU / любые"
                      values={criterion.keywords_json}
                      onChange={(values) => updateCriterion(index, "keywords_json", values)}
                    />
                    <KeywordsField
                      label="Ключевые слова EN"
                      values={criterion.keywords_en_json}
                      onChange={(values) => updateCriterion(index, "keywords_en_json", values)}
                    />
                  </div>
                  <div className="settingsCardFoot">
                    <button type="button" className="ghostButton dangerButton" onClick={() => void removeCriterion(index)}>
                      Удалить
                    </button>
                  </div>
                </div>
              ))}
              <div>
                <button type="button" className="ghostButton" onClick={addCriterion}>
                  + Добавить параметр
                </button>
              </div>
            </div>
          )}
        </div>
      </section>
    </section>
  );
}

// Ключи — столбиком или через запятую, как на экране «Теги» (mergeKeywords): пустые куски
// отбрасываются, повтор без учёта регистра не добавляется. Пока поле в фокусе, в нём набранный
// текст как есть: разбор на каждом нажатии съедал запятую и перевод строки в конце, и новое
// слово было не начать. Список при этом обновляется сразу — «Сохранить» видит его без ухода из поля.
function KeywordsField(props: { label: string; values: string[] | null; onChange: (values: string[]) => void }) {
  const [draft, setDraft] = useState<string | null>(null);
  return (
    <label className="field fieldWide">
      <span>{props.label}</span>
      <textarea
        value={draft ?? (props.values || []).join(", ")}
        onChange={(event) => {
          setDraft(event.target.value);
          props.onChange(mergeKeywords([], event.target.value));
        }}
        onBlur={() => setDraft(null)}
      />
    </label>
  );
}

function InlineLoader(props: { label: string }) {
  return (
    <div className="loadingOverlay">
      <div className="spinnerReact" />
      <span>{props.label}</span>
    </div>
  );
}

function LoadingState(props: { label: string }) {
  return (
    <div className="loadingStateReact">
      <div className="spinnerReact" />
      <span>{props.label}</span>
    </div>
  );
}
