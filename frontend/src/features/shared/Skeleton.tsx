// Скелет загрузки (решение владельца 23.10): серые полосы-заготовки на месте будущего списка вместо текста
// «Загружаем…». Текст остаётся для чтения с экрана; при «уменьшить движение» полосы не переливаются.
type Props = {
  rows?: number;
  label: string;
};

export function SkeletonList({ rows = 3, label }: Props) {
  return (
    <div className="skeletonList" role="status">
      {Array.from({ length: rows }, (_, index) => (
        <div className="skeletonCard" key={index} aria-hidden="true">
          <span className="skeletonLine skeletonLineShort" />
          <span className="skeletonLine" />
          <span className="skeletonLine skeletonLineMedium" />
        </div>
      ))}
      <span className="srOnly">{label}</span>
    </div>
  );
}
