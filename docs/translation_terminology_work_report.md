# Отчет по нефтегазовой терминологии

## Итог

- Проверено примеров: 100
- Успешно: 100
- Ошибок: 0

## Что сделано

- Нефтегазовый словарь вынесен в `oiltech_digest/processing/domain_glossary.json`.
- Summary и перевод заголовков получают компактный блок релевантных терминов в prompt.
- После ответа модели включен детерминированный слой исправления плохих терминов.
- Сборка дайджеста дополнительно нормализует старые summary/title перед PDF/DOCX/HTML.
- Добавлены команды `validate-terminology`, `audit-terminology`, `repair-terminology`, `eval-terminology`.
- Добавлены golden-кейсы и автоматическая проверка словаря в тестах.

## Примеры

| # | Кейс | Оригинал EN | Было плохо | Стало хорошо | Статус |
|---:|---|---|---|---|---|
| 1 | ГРП / fracking | Electric frac fleet expands hydraulic fracturing operations. | Компания расширила фракинг. | Компания расширила ГРП. | ok |
| 2 | КРС / workover | The workover program improved production. | Ворковер увеличил добычу. | КРС увеличил добычу. | ok |
| 3 | Заканчивание / completion | New well completion system launched. | Завершение скважины ускорило ввод. | Заканчивание скважины ускорило ввод. | ok |
| 4 | Пласт / reservoir | Reservoir output improved after optimization. | Добыча из резервуара выросла. | Добыча из пласта выросла. | ok |
| 5 | Интенсификация / stimulation | Well stimulation improved output. | Оператор провёл стимуляцию скважины. | Оператор провёл интенсификацию притока. | ok |
| 6 | Шельф / offshore | Offshore drilling expands. | Проект на оффшоре расширился. | Проект на шельфе расширился. | ok |
| 7 | Буровой раствор / drilling mud | Drilling mud system reduced losses. | Новая буровая грязь снизила потери. | Новый буровой раствор снизил потери. | ok |
| 8 | Механизированная добыча / artificial lift | Artificial lift system improves production. | Искусственный лифт повысил добычу. | Механизированная добыча повысила добычу. | ok |
| 9 | СПГ / LNG | Liquefied natural gas capacity grows. | Мощности LNG выросли. | Мощности СПГ выросли. | ok |
| 10 | LNG в названии компании | Power LNG develops LNG infrastructure. | Power LNG развивает LNG-инфраструктуру. | Power LNG развивает СПГ-инфраструктуру. | ok |
| 11 | КНБК / BHA | Bottomhole assembly optimization improved ROP. | Оптимизация bottomhole assembly повысила ROP. | Оптимизация КНБК повысила механическую скорость проходки. | ok |
| 12 | hydraulic fracturing / фракинг | The article discusses hydraulic fracturing in oil and gas operations. | Материал использует термин фракинг в описании технологии. | Материал использует термин ГРП в описании технологии. | ok |
| 13 | hydraulic fracturing / фракинг | The article discusses hydraulic fracturing in oil and gas operations. | В заголовке остался плохой перевод: фракинг. | В заголовке остался плохой перевод: ГРП. | ok |
| 14 | hydraulic fracturing / фракинг | The article discusses hydraulic fracturing in oil and gas operations. | Для дайджеста нужно заменить фракинг на отраслевой термин. | Для дайджеста нужно заменить ГРП на отраслевой термин. | ok |
| 15 | hydraulic fracturing / фракинг | The article discusses hydraulic fracturing in oil and gas operations. | AI-суть содержит некорректную формулировку фракинг. | AI-суть содержит некорректную формулировку ГРП. | ok |
| 16 | well completion / завершение скважины | The article discusses well completion in oil and gas operations. | Материал использует термин завершение скважины в описании технологии. | Материал использует термин заканчивание скважины в описании технологии. | ok |
| 17 | well completion / завершение скважины | The article discusses well completion in oil and gas operations. | В заголовке остался плохой перевод: завершение скважины. | В заголовке остался плохой перевод: заканчивание скважины. | ok |
| 18 | well completion / завершение скважины | The article discusses well completion in oil and gas operations. | Для дайджеста нужно заменить завершение скважины на отраслевой термин. | Для дайджеста нужно заменить заканчивание скважины на отраслевой термин. | ok |
| 19 | well completion / завершение скважины | The article discusses well completion in oil and gas operations. | AI-суть содержит некорректную формулировку завершение скважины. | AI-суть содержит некорректную формулировку заканчивание скважины. | ok |
| 20 | well completion / завершения скважины | The article discusses well completion in oil and gas operations. | Материал использует термин завершения скважины в описании технологии. | Материал использует термин заканчивание скважины в описании технологии. | ok |
