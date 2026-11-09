import { useEffect, useState } from "react";
import { getFeedWindow } from "../../api/stats";
import type { FeedWindowPayload } from "../../api/types";

// Окно месяца на фронте: подписи и загрузка списка месяцев. Само правило живёт на сервере
// (oiltech_digest/feed_window.py; ADR 0001: lowbrains/oiltech-agents, docs/adr/0001-single-contour.md).

// Открытые и прошлые месяцы, при открытии экрана и при возврате на вкладку: вкладку,
// оставленную на ночь, день смены окна застал бы со старым окном, и конструктор выпуска показал бы
// кнопку сохранения у уже закрытого месяца (сервер всё равно ответит 409). Сбой не мешает
// работе с текущим периодом: остаётся прежнее значение, без него переключатель не появится.
export function useFeedWindow(): FeedWindowPayload | null {
  const [payload, setPayload] = useState<FeedWindowPayload | null>(null);
  useEffect(() => {
    let cancelled = false;
    function load() {
      getFeedWindow()
        .then((result) => {
          if (!cancelled) setPayload(result);
        })
        .catch(() => undefined);
    }
    function onVisible() {
      if (!document.hidden) load();
    }
    load();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
  return payload;
}

const MONTHS = [
  "январь", "февраль", "март", "апрель", "май", "июнь",
  "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
];

// «2026-08» → «август 2026». Незнакомый формат возвращается как есть.
export function monthLabel(month: string): string {
  const match = /^(\d{4})-(\d{2})$/.exec(month);
  const name = match ? MONTHS[Number(match[2]) - 1] : undefined;
  return match && name ? `${name} ${match[1]}` : month;
}

// Подсказка у неактивной «В дайджест» / «Убрать» карточки радара закрытого месяца (решение
// владельца 29.09). Месяц карточки: по дате поступления на радар; закрыт ли он, решает сервер.
export function radarDigestLockedText(month: string): string {
  return `Карточка в архиве за ${monthLabel(month)} (по дате поступления): отметку «в дайджест» не поменять — архив только для просмотра.`;
}
