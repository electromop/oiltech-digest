import type { DocumentDetails } from "../../api/types";
import { anchorText, asList, formatSize, statusLabel, toText } from "./documentUtils";

type Props = {
  details: DocumentDetails;
};

/**
 * Карточка документа. Главное здесь — таблица фактов: число, которое модель НЕ нашла
 * в тексте документа, нельзя показывать так же, как проверенное. Неподтверждённая
 * строка помечена и цветом, и словами, и сводным предупреждением над таблицей —
 * пользователь переносит эти числа в отчёт, и молчаливая ошибка уходит заказчику.
 */
export function DocumentCardPanel({ details }: Props) {
  const document = details.document;
  const card = details.card;
  const facts = Array.isArray(details.facts) ? details.facts : [];
  const unverified = facts.filter((fact) => !fact.verified);
  const summary = asList(card?.summary_json);
  const claims = asList(card?.claims_json);

  return (
    <div className="documentCard">
      <div className="documentPassport">
        <PassportItem label="Тип документа" value={toText(card?.doc_type ?? document.doc_type)} />
        <PassportItem label="Издатель" value={toText(card?.publisher ?? document.publisher)} />
        <PassportItem label="Дата документа" value={toText(card?.doc_date)} />
        <PassportItem label="Язык" value={toText(card?.language)} />
        <PassportItem label="Формат" value={toText(document.kind)} />
        <PassportItem label="Размер" value={formatSize(document.size_bytes)} />
        <PassportItem
          label="Якорей"
          value={document.anchor_count ? `${document.anchor_count} (${document.anchor_unit || "блок"})` : "—"}
        />
        <PassportItem label="Знаков текста" value={document.text_chars ? String(document.text_chars) : "—"} />
        {document.empty_anchors ? (
          <PassportItem
            label="Без текста"
            value={`${document.empty_anchors} (${document.anchor_unit || "блок"})`}
            warn
          />
        ) : null}
        <PassportItem label="Состояние" value={statusLabel(String(document.status))} />
      </div>

      {document.error_message ? (
        <div className="documentAlert bad">{document.error_message}</div>
      ) : null}

      <div className="documentBlock">
        <h3>Суть</h3>
        <p className="documentEssence">{toText(card?.essence ?? document.essence)}</p>
      </div>

      <div className="documentBlock">
        <h3>Сводка</h3>
        {summary.length ? (
          <ul className="documentList">
            {summary.map((item, index) => (
              <li key={`summary-${index}`}>{item}</li>
            ))}
          </ul>
        ) : (
          <p className="metaText">Сводка пуста.</p>
        )}
      </div>

      <div className="documentBlock">
        <h3>Заявления</h3>
        {claims.length ? (
          <ul className="documentList">
            {claims.map((item, index) => (
              <li key={`claim-${index}`}>{item}</li>
            ))}
          </ul>
        ) : (
          <p className="metaText">Заявлений не выделено.</p>
        )}
      </div>

      <div className="documentBlock">
        <h3>Факты</h3>
        <p className="metaText">
          Всего фактов: {facts.length} · не подтверждено: {unverified.length}
        </p>
        {unverified.length ? (
          <div className="documentAlert warn">
            {unverified.length} из {facts.length} чисел не найдены в тексте документа при сверке. Не переносите их в
            отчёт, пока не проверите по оригиналу.
          </div>
        ) : null}
        {facts.length ? (
          <div className="jobsTableWrap">
            <table className="jobsTable documentFactsTable">
              <thead>
                <tr>
                  <th>Значение</th>
                  <th>Единица</th>
                  <th>Контекст</th>
                  <th>Где в документе</th>
                  <th>Сверка</th>
                </tr>
              </thead>
              <tbody>
                {facts.map((fact, index) => (
                  <tr key={`fact-${index}`} className={fact.verified ? undefined : "factRowUnverified"}>
                    <td>
                      <span className={fact.verified ? "factValue" : "factValue unverified"}>{toText(fact.value)}</span>
                    </td>
                    <td>{toText(fact.unit)}</td>
                    <td className="factContext">{toText(fact.context)}</td>
                    <td>{anchorText(fact.anchor ?? null, document.anchor_unit)}</td>
                    <td>
                      {fact.verified ? (
                        <span className="factFlag ok">подтверждён</span>
                      ) : (
                        <span className="factFlag bad">не подтверждён</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="metaText">Фактов не извлечено.</p>
        )}
      </div>
    </div>
  );
}

function PassportItem(props: { label: string; value: string; warn?: boolean }) {
  return (
    <div className={props.warn ? "documentPassportItem warn" : "documentPassportItem"}>
      <span className="documentPassportLabel">{props.label}</span>
      <span className="documentPassportValue">{props.value}</span>
    </div>
  );
}
