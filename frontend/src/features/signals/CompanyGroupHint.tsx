// Подсказка у поля «Компания» (решение владельца 20.10): фильтр «всё про компанию» показывает и дочерние
// общества компании по списку «дочка → мать» (экран «Дочерние общества»). Кого он добавил, говорит сервер
// (company_group в сводке: сама компания первой, затем дочки); дочек нет — подсказки нет.

type Props = {
  // Компания в поле сейчас: сводка могла прийти ещё по прежней, её подсказку не показываем.
  company: string;
  group: string[] | undefined;
};

// Пробелы — как у сервера в company_group: по краям нет, внутри по одному.
function spaced(text: string) {
  return text.split(/\s+/).filter(Boolean).join(" ");
}

export function CompanyGroupHint({ company, group }: Props) {
  if (!group || group.length < 2 || group[0] !== spaced(company)) return null;
  const [name, ...daughters] = group;
  return <p className="panelHint">{`${name} и дочерние: ${daughters.join(", ")}`}</p>;
}
