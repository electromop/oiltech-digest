import type { JournalCheck, JournalCheckSample, JournalRule } from "../../api/feedbackJournal";

// Правило эксперта и его автопроверка в строке журнала ОС (#190, спека #187): что предлагается какому агенту и шагу,
// что уточняет или сводит, и что показала проверка на эталоне ОС: к эталону, от эталона, без изменений и совет; у
// «Сути и перевода» «было / стало» рядом с текстом эксперта, у поиска сверка повторного поиска с ТОП-списком темы.

const RULE_KINDS: Record<string, string> = {
  new: "Новое правило",
  refine: "Уточнение правила",
  merge: "Сведение правил",
};

const VALUE_LABELS: Record<string, string> = {
  accept: "брать",
  reject: "не брать",
  business: "бизнес",
  technology: "технология",
  other: "другое",
  strong: "сильный",
  useful: "полезный",
  watch: "наблюдать",
  weak: "слабый",
  true: "одно событие",
  false: "разные события",
};

const CHECK_STATE: Record<string, string> = {
  queued: "Автопроверка идёт: итог появится в журнале.",
  postponed: "Автопроверка отложена.",
  failed: "Автопроверка не выполнена.",
};

export function valueLabel(value: unknown): string {
  if (value === null || value === undefined || value === "") return "нет ответа";
  const text = String(value);
  return VALUE_LABELS[text] ?? text;
}

// Где действует правило: шаг, категория, тема поиска (строка журнала и набор правил агента).
export function ruleWhere(rule: JournalRule): string {
  return [rule.step_label, rule.category_label ? `категория «${rule.category_label}»` : null,
    rule.topic ? `тема «${rule.topic}»` : null].filter(Boolean).join(" · ");
}

export function RuleSummary({ rule, ruleKind, replaced, proposed }: {
  rule: JournalRule;
  ruleKind?: string;
  replaced?: Array<JournalRule & { id: number }>;
  proposed?: JournalRule | null;
}) {
  const where = ruleWhere(rule);
  return (
    <div className="ruleSummary" aria-label="Правило">
      <p className="metaText">{RULE_KINDS[ruleKind ?? "new"] ?? ruleKind} для агента {rule.agent_label}: {where}</p>
      <p className="ruleText">{rule.text}</p>
      {proposed && proposed.text !== rule.text ? (
        <p className="metaText">Разбор ОС предлагал: {proposed.text}</p>
      ) : null}
      {replaced?.length ? (
        <ul className="journalLines" aria-label="Что уточняет или сводит">
          {replaced.map((one) => <li key={one.id}>Правило №{one.id}: {one.text}</li>)}
        </ul>
      ) : null}
    </div>
  );
}

export function CheckSummary({ check }: { check: JournalCheck | null | undefined }) {
  if (!check) return <p className="metaText">Автопроверки ещё не было.</p>;
  if (check.status !== "done") {
    return (
      <p className="metaText" aria-label="Автопроверка">
        {CHECK_STATE[check.status] ?? check.status}{check.note ? ` ${check.note}` : ""}
      </p>
    );
  }
  return (
    <div className="ruleCheck" aria-label="Автопроверка">
      <p>
        <span className={`miniPill ${check.advises_approval ? "ok" : "warn"}`}>
          {check.advice ?? "совета нет"}
        </span>
        {check.no_count ? null : (
          <span className="metaText">
            {" "}Строк эталона ОС: {check.rows} · к эталону: {check.toward} · от эталона: {check.away} · без
            изменений: {check.unchanged}
          </span>
        )}
      </p>
      {check.baseline && !check.no_count && !check.search ? (
        <p className="metaText">
          Ответ без правила: из принятых решений {check.baseline.accepted}, посчитан заново {check.baseline.computed}.
        </p>
      ) : null}
      {check.note ? <p className="metaText">{check.note}</p> : null}
      <Samples check={check} />
    </div>
  );
}

function Samples({ check }: { check: JournalCheck }) {
  if (!check.samples.length) return null;
  if (check.no_count) {
    return (
      <table className="journalChanges" aria-label="Было и стало">
        <thead><tr><th>Карточка</th><th>Текст эксперта</th><th>Было (без правила)</th><th>Стало (с правилом)</th></tr></thead>
        <tbody>
          {check.samples.map((sample, index) => (
            <tr key={`${sample.signal_id}-${sample.field ?? index}`}>
              <td>№{sample.signal_id} {sample.label}</td>
              <td>{sample.expert}</td>
              <td>{valueLabel(sample.before)}</td>
              <td>{valueLabel(sample.after)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  if (check.search) {
    return (
      <table className="journalChanges" aria-label="ТОП-список темы">
        <thead><tr><th>Карточка ТОП-списка</th><th>С подсказками</th><th>Без подсказок</th></tr></thead>
        <tbody>
          {check.samples.map((sample) => (
            <tr key={sample.signal_id}>
              <td>№{sample.signal_id} {sample.label}</td>
              <td>{sample.with ? "найдена" : "не найдена"}</td>
              <td>{sample.without ? "найдена" : "не найдена"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  return (
    <table className="journalChanges" aria-label="Решения, которые сдвинулись">
      <thead><tr><th>Карточка</th><th>Эталон</th><th>Без правила</th><th>С правилом</th><th>Сдвиг</th></tr></thead>
      <tbody>
        {check.samples.map((sample: JournalCheckSample, index) => (
          <tr key={`${sample.signal_id}-${sample.field ?? index}`}>
            <td>№{sample.signal_id} {sample.label}</td>
            <td>{valueLabel(sample.expected)}</td>
            <td>{valueLabel(sample.before)}</td>
            <td>{valueLabel(sample.after)}</td>
            <td>{sample.direction === "toward" ? "к эталону" : sample.direction === "away" ? "от эталона" : "без изменений"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
