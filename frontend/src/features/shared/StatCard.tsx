// Плитка-счётчик над списком — одна на «Бизнес-сигналы» и «Технологический радар»
// (документ заказчика 19.09: сводку радара сделать «как в Бизнес-сигналах»).
export function StatCard(props: { label: string; value: number }) {
  return (
    <div className="statCardReact">
      <div className="statValueReact">{props.value}</div>
      <div className="metaText">{props.label}</div>
    </div>
  );
}
