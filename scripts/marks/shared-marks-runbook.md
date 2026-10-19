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

   Если в `main` уже слит допуск по коду (#113) и он ещё не выкачен — тем же способом сначала
   `scripts/admission/admission-before-deploy.sql`: без его колонки новый вход падает у всех.

2. **Новые образы и пробный прогон, старое приложение ещё работает** (в `/root/oiltech-digest`):

   ```bash
   git fetch origin && git reset --hard origin/main
   docker compose build app scheduler worker playwright-worker
   docker compose run --rm --no-deps app python -m oiltech_digest.cli migrate-shared-marks --base-user-id 4
   docker exec oiltech_app python -m oiltech_digest.cli live-ai-leases
   ```

   Пробный прогон: что переносится, что пропускается (проверочные отметки других учёток), состав
   каждого закрытого месяца — «совпадает с прежним выпуском» (по замеру 19.10 в сентябре 10 карточек радара учётки №4).
   «ОСТАНОВЛЕНО» — дальше не идти. `live-ai-leases` должен ответить «ИИ-задач в работе нет»: иначе
   `deploy-core.sh` на шаге 3 откажет, пока `app` уже остановлен, — дождаться конца задач.

3. **Перенос между остановкой старого и запуском нового:**

   ```bash
   docker compose stop app
   docker compose run --rm --no-deps app python -m oiltech_digest.cli migrate-shared-marks --base-user-id 4 --write
   sh scripts/deploy-core.sh --no-schema app scheduler worker playwright-worker
   ```

   Пока `app` остановлен, отметки никто не ставит; планировщик и воркеры читают отметки (защиты,
   пересчёт, выгрузка) — их выкатываем тем же вызовом. **Если `deploy-core.sh` отказал после
   переноса — старый `app` не поднимать:** он пишет отметки в личные таблицы, а перенос уже сделан и
   повторно их не возьмёт. Разобрать отказ и повторить `deploy-core.sh`.

4. **Сверка на проде только чтением:**

   ```bash
   docker exec oiltech_app python -m oiltech_digest.cli migrate-shared-marks --check
   ```

   Ожидание — те же месяцы и числа, что в шаге 2, у каждого «совпадает с прежним выпуском». Сверка
   берёт месяцы, закрытые на момент переноса: её можно повторять и после 11.11. Повторный
   `--write` ничего не меняет («перенос уже сделан»).

**Откат кода** — прежний коммит через `deploy-core.sh`: прежний код читает личные таблицы, они не
менялись; отметки, поставленные после переноса, останутся только в общих.
