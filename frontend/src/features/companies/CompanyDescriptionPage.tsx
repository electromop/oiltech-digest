import { useEffect, useState } from "react";
import { SkeletonList } from "../shared/Skeleton";
import { ApiError } from "../../api/client";
import { getCompanyDescription, saveCompanyDescription, type CompanyDescription } from "../../api/companyDescription";

type ToastWriter = (text: string, tone?: "default" | "error") => void;

type Props = {
  onUnauthorized: () => void;
  showToast: ToastWriter;
};

// Предел сервера (api.CompanyDescriptionIn).
const MAX_TEXT = 4000;

function formatMoment(value: string | null): string {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("ru-RU", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Moscow",
  });
}

// Описание Компании для агента «Гипотеза для Компании» (#162, устроено как «Дочерние общества»): текст, кто и когда
// правил. Текст уходит внешней модели, поэтому вносится только публичная версия, утверждённая заказчиком (ответ
// эксперта 03.11); внутренняя версия сюда не вносится. Новое описание уже написанные гипотезы не пересчитывает.
export function CompanyDescriptionPage({ onUnauthorized, showToast }: Props) {
  const [description, setDescription] = useState<CompanyDescription | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getCompanyDescription()
      .then((loaded) => {
        setDescription(loaded);
        setDraft(loaded.text);
      })
      .catch((error) => handleError(error, "Не удалось загрузить описание Компании"));
  }, []);

  function handleError(error: unknown, fallback: string) {
    if (error instanceof ApiError && error.status === 401) {
      onUnauthorized();
      return;
    }
    showToast(error instanceof Error && error.message ? error.message : fallback, "error");
  }

  async function save() {
    try {
      setSaving(true);
      const saved = await saveCompanyDescription(draft);
      setDescription(saved);
      setDraft(saved.text);
      showToast("Описание Компании сохранено");
    } catch (error) {
      handleError(error, "Не удалось сохранить описание Компании");
    } finally {
      setSaving(false);
    }
  }

  const changed = description !== null && draft.trim() !== description.text;
  return (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <h1>Описание Компании</h1>
          <p>
            По этому тексту агент «Гипотеза для Компании» понимает, для кого пишет гипотезу к материалу выпуска. Новое
            описание уже написанные гипотезы не меняет.
          </p>
        </div>
      </header>

      <section className="panel">
        <div className="documentAlert warn" role="note">
          Текст уходит внешней модели. Вносите только версию из публичных сведений, утверждённую заказчиком для
          внешней модели; внутреннюю версию описания сюда не вносите.
        </div>
        <ul className="metaText">
          <li>Чем Компания занимается, какие услуги и где оказывает, кто её заказчики.</li>
          <li>Называйте функции (бурение, ТКРС, геологоразведка, промышленная безопасность), а не подразделения.</li>
          <li>Без внутренних проектов и проблем. Отдельно: чего Компании не предлагать.</li>
        </ul>
        {description === null ? (
          <SkeletonList rows={3} label="Загружаем описание Компании" />
        ) : (
          <>
            <label className="field">
              <span>Описание</span>
              <textarea
                rows={10}
                value={draft}
                maxLength={MAX_TEXT}
                onChange={(event) => setDraft(event.target.value)}
              />
            </label>
            <div className="metaText" role="status">
              {description.updated_at
                ? `Последняя правка: ${description.updated_by || "учётка удалена"}, ${formatMoment(description.updated_at)}`
                : "Описания ещё нет."}
            </div>
            <div className="digestActionGroup">
              <button type="button" className="primaryButton" disabled={saving || !changed} onClick={() => void save()}>
                {saving ? "Сохраняем…" : "Сохранить"}
              </button>
              <button type="button" className="ghostButton" disabled={saving || !changed}
                onClick={() => setDraft(description.text)}>
                Отменить правку
              </button>
            </div>
          </>
        )}
      </section>

      {description && description.history.length > 1 ? (
        <section className="panel">
          <div className="panelHeader">
            <h2>Кто и когда правил</h2>
          </div>
          <div className="jobsTableWrap">
            <table className="jobsTable">
              <thead>
                <tr>
                  <th scope="col">Когда</th>
                  <th scope="col">Кто</th>
                  <th scope="col">Знаков</th>
                </tr>
              </thead>
              <tbody>
                {description.history.map((row, index) => (
                  <tr key={`${row.updated_at}-${index}`}>
                    <td>{formatMoment(row.updated_at)}</td>
                    <td>{row.updated_by || "учётка удалена"}</td>
                    <td className="tabularNums">{row.chars}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
    </section>
  );
}
