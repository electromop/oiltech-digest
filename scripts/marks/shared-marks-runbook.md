# Общие отметки (#111) — выкат

Что меняется: отметки статей и карточек радара и черновик выпуска — общие (таблицы
`article_marks`, `signal_marks`, черновик месяца без учётки). Личные таблицы остаются архивом.

**Окно:** до закрытия октября (11.11, 00:00 МСК), не 1–10 числа (окно отметок прошлого месяца)
и не во время ежедневного радара (00:00–02:30 МСК). Учётка-основа — эксперт, №4.

1. **Таблицы до кода** (только добавления, повтор безопасен):

   ```bash
   ssh <РФ-ядро> 'docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest -X -v ON_ERROR_STOP=1' \
     < scripts/marks/shared-marks-before-deploy.sql
   ```

2. **Новый образ, старое приложение ещё работает** (в `/root/oiltech-digest`):

   ```bash
   git fetch origin && git reset --hard origin/main && docker compose build app
   docker compose run --rm --no-deps app python -m oiltech_digest.cli migrate-shared-marks --base-user-id 4
   ```

   Пробный прогон: что переносится, что пропускается (проверочные отметки других учёток),
   сверка закрытых месяцев. «ОСТАНОВЛЕНО» — дальше не идти, разобрать расхождение.

3. **Перенос между остановкой старого и запуском нового:**

   ```bash
   docker compose stop app
   docker compose run --rm --no-deps app python -m oiltech_digest.cli migrate-shared-marks --base-user-id 4 --write
   sh scripts/deploy-core.sh --no-schema app scheduler worker playwright-worker
   ```

   Пока `app` остановлен, отметки никто не ставит; планировщик и воркеры читают отметки
   (защиты, пересчёт, выгрузка) — их выкатываем тем же вызовом.

4. **Сверка на проде только чтением** — тот же пробный прогон:

   ```bash
   docker exec oiltech_app python -m oiltech_digest.cli migrate-shared-marks --base-user-id 4
   ```

   Ожидание: в общие — 0, черновики «общий новее, не тронут», сверка закрытых месяцев сошлась
   (сентябрь — 10 карточек радара учётки №4).

**Откат кода** — прежний коммит через `deploy-core.sh`: прежний код читает личные таблицы, они не
менялись; отметки, поставленные после переноса, останутся только в общих.
