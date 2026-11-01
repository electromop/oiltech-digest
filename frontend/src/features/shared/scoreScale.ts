// Цвет балла — одна шкала у «Бизнес-сигналов» и «Технологического радара», согласована с
// backend score_label (pipeline.py: пороги 80/65/40): «Высокая»/«Выше средней» (>=65) →
// зелёный, «Средняя» (>=40) → оранжевый, «Низкая» (<40) → красный, нет оценки → серый.
export function scoreClass(score: number) {
  if (!score) return "muted";
  if (score >= 65) return "ok";
  if (score >= 40) return "warn";
  return "bad";
}

// Словесную оценку (score_label) красим в тот же тон, что и число, — чтобы метка и
// оценка совпадали по цвету.
export function ratingClass(rating: string | null | undefined) {
  switch ((rating || "").trim()) {
    case "Высокая":
    case "Выше средней":
    // Ступени балла второго вызова «Карточки» (#132): те же пороги 80/65/40 по умолчанию.
    case "Сильный":
    case "Полезный":
      return "ok";
    case "Средняя":
    case "Наблюдать":
      return "warn";
    case "Низкая":
    case "Слабый":
      return "bad";
    default:
      return "muted";
  }
}
