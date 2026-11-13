import type { ExpertRulesOverview } from "../../api/feedbackJournal";
import { formatMoment } from "./JournalEntryCard";

// Правила эксперта по агентам (#190, ADR 0007): действующая версия набора каждого агента, её правила и прежние версии.
// Видят все допущенные; откатить действующую версию к прежней может администратор (флаг can_rollback от сервера).

type Props = {
  rules: ExpertRulesOverview | null;
  busy: boolean;
  onRollback: (actionId: number) => void;
};

export function ExpertRulesPanel({ rules, busy, onRollback }: Props) {
  if (!rules) return null;
  const withRules = rules.agents.filter((agent) => agent.rules.length > 0);
  const without = rules.agents.filter((agent) => agent.rules.length === 0);
  return (
    <section className="panel" aria-label="Правила эксперта">
      <div className="panelHeader">
        <h2>Правила эксперта</h2>
      </div>
      <p className="metaText">
        Согласованные правила, которые платформа добавляет к инструкции агента при каждом вызове; не больше {rules.limit}{" "}
        у агента. Новое правило действует для новых сигналов, прошлые не пересчитываются.
      </p>
      {withRules.map((agent) => {
        const previous = agent.history.find((one) => one.current)?.previous_version ?? 0;
        return (
          <section key={agent.agent} className="ruleAgent" aria-label={`Правила: ${agent.label}`}>
            <h3 className="journalLabel">{agent.label}: версия {agent.version}, правил {agent.rules.length}</h3>
            <ol className="journalLines">
              {agent.rules.map((rule) => (
                <li key={rule.id}>
                  <span className="metaText">
                    {[rule.step_label, rule.category_label ? `категория «${rule.category_label}»` : null,
                      rule.topic ? `тема «${rule.topic}»` : null].filter(Boolean).join(" · ")}:{" "}
                  </span>
                  {rule.text}
                  <span className="metaText"> · №{rule.id}, {rule.approved_by ?? "без имени"}, {formatMoment(rule.approved_at)}</span>
                </li>
              ))}
            </ol>
            {rules.can_rollback && agent.action_id ? (
              <button type="button" className="ghostButton compactButton" disabled={busy}
                onClick={() => onRollback(Number(agent.action_id))}>
                Откатить к версии {previous}
              </button>
            ) : null}
          </section>
        );
      })}
      {without.length ? (
        <p className="metaText">Правил нет: {without.map((agent) => agent.label).join(", ")}.</p>
      ) : null}
    </section>
  );
}
