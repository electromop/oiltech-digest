# OilTech Digest — handoff на 2026-06-06

_Документ самодостаточный. Коротко фиксирует текущее состояние проекта после большого блока работ по новой админке, дайджесту и UX._

---

## 1. Главное

Собрана и закоммичена новая **React-админка** поверх текущего backend API.  
Основной коммит:

- `0b243b7` — `Build React admin and polish digest workflows`

Рабочее дерево после коммита было чистым.

---

## 2. Что сделано

### React-админка

Переведены на React + Vite + TypeScript основные экраны:

- `Сигналы`
- `Месячный дайджест`
- `Источники`
- `Скоринг`
- `Теги`

FastAPI отдаёт собранный `frontend/dist`. Старый `web/app.html` остаётся fallback.

### Сигналы

- `Статьи` переименованы в `Сигналы`
- фильтры переработаны:
  - 1 строка: поиск + тег
  - 2 строка: источник + статус + сортировка
- `Тег` и `Источник` сделаны как searchable custom dropdown
- раскрытие сигнала через стрелку
- scoring details в раскрытом сигнале scrollable
- добавлены preloaders
- верхние метрики переработаны под редакторскую воронку:
  - `Всего сигналов`
  - `Новые`
  - `На проверке`
  - `В дайджест`
  - `Обработано`

### Метрика `Обработано`

Сначала была собрана на фронте, затем вынесена в backend `/api/stats` как `processed_articles`.

Смысл:

- есть `summary`
- есть `relevance`
- и дальше либо статья признана нерелевантной,
- либо дошла до tag/score-слоя

### Источники

- упрощены карточки источников
- сохранены advanced-настройки и диагностика
- добавлен operational summary по health:
  - `Все`
  - `0 статей`
  - `Застой`
  - `ОК`
  - `Выкл`
- список сортируется так, чтобы проблемные источники были выше

### Дайджест

- собран новый toolbar действий
- фильтры переработаны по той же логике, что и в `Сигналах`
- экспорт `PDF / DOCX / HTML` теперь скачивается **в фоне**, без пустой вкладки
- исправлен баг шаблона экспорта из-за CSS braces в `str.format`
- улучшен экспортный шаблон:
  - уменьшены шансы разрыва карточек между страницами
  - добавлены print-правила в HTML template

### Auth

- вход и регистрация разделены на два понятных сценария
- по умолчанию показывается экран входа
- под формой есть текстовый переход:
  - `Нет аккаунта? Зарегистрироваться`
  - `Уже есть аккаунт? Войти`

### Навигация / адаптив

- переработан десктопный сайдбар:
  - группы `Работа / Настройки`
  - сервисные действия внизу
  - выход внизу
  - сворачивание внизу
- на мобильном сайдбар убран
- вместо него сделано **нижнее меню**

### Бэклог

`BACKLOG.md` синхронизирован с реальным состоянием проекта.

---

## 3. Что осталось живым

Текущий актуальный хвост:

1. `P2` — **Источники: надёжность парсинга**
   - проблемные источники
   - JS-WAF
   - telegram-дубли / telegram coverage
   - RU-селекторы
   - stale / no-articles

2. `P3` — **Дайджест: довести экспортный шаблон**
   - редизайн ближе к референсу
   - стр. 1: `Главное за период`
   - далее по 3 статьи на страницу
   - добить разрывы и выравнивание текста

3. `P3` — **Теги: переработать редактор**
   - мысль уже запаркована
   - желаемое направление: tree / master-detail вместо плоской inline-формы

---

## 4. Последний сделанный шаг по P2

Для экрана `Источники` уже сделан первый operational слой:

- summary по health-вердиктам
- быстрый фильтр по проблемным группам
- сортировка, поднимающая наверх `0 статей` и `застой`

Это ещё не чинит парсинг автоматически, но делает triage заметно проще.

Логичный следующий шаг:

- показывать в карточке **тип проблемы / рекомендованное действие**
  - `selector fix`
  - `telegram`
  - `playwright/js`
  - `stale, нужен форс-парсинг`

---

## 5. Важные файлы

### Frontend

- [frontend/src/app/App.tsx](/Users/g.fink/Documents/GitHub/libsolution_org/oiltech-digest/frontend/src/app/App.tsx)
- [frontend/src/styles/globals.css](/Users/g.fink/Documents/GitHub/libsolution_org/oiltech-digest/frontend/src/styles/globals.css)
- [frontend/src/features/articles/ArticlesPage.tsx](/Users/g.fink/Documents/GitHub/libsolution_org/oiltech-digest/frontend/src/features/articles/ArticlesPage.tsx)
- [frontend/src/features/digest/DigestPage.tsx](/Users/g.fink/Documents/GitHub/libsolution_org/oiltech-digest/frontend/src/features/digest/DigestPage.tsx)
- [frontend/src/features/sources/SourcesPage.tsx](/Users/g.fink/Documents/GitHub/libsolution_org/oiltech-digest/frontend/src/features/sources/SourcesPage.tsx)
- [frontend/src/features/sources/SourceCard.tsx](/Users/g.fink/Documents/GitHub/libsolution_org/oiltech-digest/frontend/src/features/sources/SourceCard.tsx)
- [frontend/src/features/tags/TagsPage.tsx](/Users/g.fink/Documents/GitHub/libsolution_org/oiltech-digest/frontend/src/features/tags/TagsPage.tsx)

### Backend

- [oiltech_digest/api.py](/Users/g.fink/Documents/GitHub/libsolution_org/oiltech-digest/oiltech_digest/api.py)
- [oiltech_digest/db/repository.py](/Users/g.fink/Documents/GitHub/libsolution_org/oiltech-digest/oiltech_digest/db/repository.py)
- [oiltech_digest/processing/digest.py](/Users/g.fink/Documents/GitHub/libsolution_org/oiltech-digest/oiltech_digest/processing/digest.py)
- [oiltech_digest/processing/digest_email_template.html](/Users/g.fink/Documents/GitHub/libsolution_org/oiltech-digest/oiltech_digest/processing/digest_email_template.html)
- [BACKLOG.md](/Users/g.fink/Documents/GitHub/libsolution_org/oiltech-digest/BACKLOG.md)

---

## 6. Деплой без потери базы

Безопасное обновление на сервере:

```bash
git pull
docker compose up -d --build
```

Почему база не сбрасывается:

- Postgres хранится в named volume `pgdata`
- простая пересборка контейнеров volume не удаляет

Чего **не делать**, если не хотите потерять данные:

```bash
docker compose down -v
```

Перед обновлением полезно сделать backup:

```bash
docker compose exec -T db pg_dump -U oiltech -d oiltech_digest > backup-$(date +%F-%H%M).sql
```

---

## 7. Рекомендуемый следующий шаг

Если продолжать с того места, где остановились, я бы шёл так:

1. `Источники` — добавить более явный triage проблем
2. `Источники` — точечно разбирать request / telegram / js-проблемы
3. затем вернуться к экспортному шаблону дайджеста

