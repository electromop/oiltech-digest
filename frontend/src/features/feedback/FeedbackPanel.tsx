import { useEffect, useRef, useState } from "react";
import { getFeedback, listFeedbackReasons, saveFeedback } from "../../api/feedback";
import type { FeedbackEntry, FeedbackReason } from "../../api/types";

/**
 * Панель обратной связи по сигналу или по источнику.
 *
 * Главное требование — БЫСТРО. Заказчик размечает ленту десятками карточек за заход,
 * и форма с кнопкой «Отправить» съела бы эту работу. Поэтому причина и любая оценка
 * сохраняются СРАЗУ по клику, а комментарий — по уходу фокуса. Ничего подтверждать не надо.
 *
 * Набор полей не выдуман: ровно так заказчик уже пишет ОС руками (чат 10.09) —
 * «Корректировка названия… Статья интересная и актуальная. Источник отличный.
 * Перевод: walking island rig → шагающая буровая…». Отсюда три шкалы и свободный текст.
 *
 * Оценка НЕ перетирается частичным сохранением: бэкенд сливает поля через COALESCE,
 * поэтому «поставил звезду» не стирает уже написанный комментарий.
 */

type Props = {
  articleId?: number;
  sourceId?: number;
  /** Показывать шкалу перевода: для источника она бессмысленна. */
  withTranslation?: boolean;
  onSaved?: (entry: FeedbackEntry) => void;
  onError?: (error: unknown) => void;
};

// Формулировки заказчика (13.09): официальный тон для корпоративного портала ГПН.
const SCALES: Array<{ key: "usefulness" | "translation" | "source_quality"; label: string; hint: string }> = [
  { key: "usefulness", label: "Практическая ценность сигнала", hint: "1 — не применимо, 5 — берём в дайджест" },
  { key: "translation", label: "Качество заголовка и перевода", hint: "1 — смысл искажён, 5 — править нечего" },
  { key: "source_quality", label: "Надёжность источника", hint: "1 — исключить, 5 — держать в фокусе" },
];

export function FeedbackPanel(props: Props) {
  const { articleId, sourceId, withTranslation = true } = props;
  const [reasons, setReasons] = useState<FeedbackReason[]>([]);
  const [entry, setEntry] = useState<FeedbackEntry | null>(null);
  const [comment, setComment] = useState("");
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  // Чтобы не слать запрос, когда человек открыл комментарий и ничего не поменял.
  const lastSavedComment = useRef("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [reasonList, current] = await Promise.all([
          listFeedbackReasons(),
          getFeedback({ articleId, sourceId }),
        ]);
        if (cancelled) return;
        // Жёсткая защита от нештатного ответа. Панель живёт ВНУТРИ карточки статьи, и
        // необработанный `reasons.map is not a function` уронил бы всю ленту, а не себя.
        setReasons(Array.isArray(reasonList) ? reasonList : []);
        const loaded = current && typeof current === "object" ? current.entry : null;
        setEntry(loaded ?? null);
        setComment(loaded?.comment || "");
        lastSavedComment.current = loaded?.comment || "";
      } catch (error) {
        // Не даём панели утащить за собой карточку: показываем пустую и сообщаем наверх.
        if (cancelled) return;
        setReasons([]);
        props.onError?.(error);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [articleId, sourceId]);

  async function push(patch: Record<string, unknown>) {
    try {
      setSaving(true);
      const payload = { ...(articleId ? { article_id: articleId } : {}), ...(sourceId ? { source_id: sourceId } : {}), ...patch };
      const result = await saveFeedback(payload);
      if (!result?.entry) throw new Error("Сервер не вернул сохранённую обратную связь");
      setEntry(result.entry);
      setSavedAt(Date.now());
      props.onSaved?.(result.entry);
    } catch (error) {
      props.onError?.(error);
    } finally {
      setSaving(false);
    }
  }

  const scales = withTranslation ? SCALES : SCALES.filter((scale) => scale.key !== "translation");

  return (
    <div className="feedbackPanel">
      <div className="feedbackHead">
        <span className="feedbackTitle">Обратная связь</span>
        {saving ? <span className="metaText">сохраняю…</span> : null}
        {!saving && savedAt ? <span className="metaText">сохранено</span> : null}
      </div>

      <div className="feedbackReasons">
        {reasons.map((reason) => (
          <button
            key={reason.value}
            type="button"
            className={entry?.reason === reason.value ? "feedbackChip active" : "feedbackChip"}
            disabled={saving}
            // Повторный клик снимает причину — иначе ошибочный тег не отменить.
            onClick={() => void push({ reason: entry?.reason === reason.value ? null : reason.value })}
          >
            {reason.label}
          </button>
        ))}
      </div>

      {scales.map((scale) => (
        <div key={scale.key} className="feedbackScale">
          <span className="feedbackScaleLabel" title={scale.hint}>
            {scale.label}
          </span>
          <span className="feedbackStars">
            {[1, 2, 3, 4, 5].map((value) => {
              const current = Number(entry?.[scale.key] || 0);
              return (
                <button
                  key={value}
                  type="button"
                  className={value <= current ? "feedbackStar on" : "feedbackStar"}
                  disabled={saving}
                  aria-label={`${scale.label}: ${value} из 5`}
                  onClick={() => void push({ [scale.key]: value })}
                >
                  ★
                </button>
              );
            })}
          </span>
        </div>
      ))}

      <textarea
        className="feedbackComment"
        rows={3}
        placeholder="Что поправить: формулировка заголовка, термины перевода, чего не хватает. Текстом, как в чате."
        value={comment}
        onChange={(event) => setComment(event.target.value)}
        onBlur={() => {
          const next = comment.trim();
          if (next === lastSavedComment.current) return;
          lastSavedComment.current = next;
          void push({ comment: next });
        }}
      />
    </div>
  );
}
