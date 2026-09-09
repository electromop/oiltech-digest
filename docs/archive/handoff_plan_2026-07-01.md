# Handoff 2026-07-01 — релиз (скоринг + T2/H1/H2 + IDOR + дайджест) и активация после инцидента NL

**Кому:** коллеге на утро. **Контекст в одну строку:** весь код в `main`, ядро РФ задеплоено, единственный блокер — **NL-воркер недоступен из-за инцидента с охлаждением в ДЦ Timeweb** (SSH/ping timeout). Ниже — что запустить парой команд.

---

## TL;DR — что делать

1. **Демо-фикс баллов БЕЗ NL (можно прямо сейчас, на ядре РФ):** задеплоить ядро (если не сделано) и выполнить `rescore-recompute` — пересчитает «бизнес-эффект» из уже сохранённых AI-баллов по новой формуле. Хорошие статьи снова станут 70+. **NL для этого НЕ нужен.**
2. **Когда NL поднимется** (`ping 85.234.107.233` отвечает): задеплоить воркер + пустить полный AI-перепрогон (новая модель+промпт, лучшее качество).

---

## Состояние (всё в `origin/main`, верхушка — PR #20/#21 + rescore)

| Сделано и смержено | Где работает |
|---|---|
| Скоринг: AI-первичный балл (фикс «бизнес-эффект просел до ~30») | ядро + воркер |
| T2: гонка двойного AI-расхода закрыта | ядро |
| H1: идемпотентный биллинг (ноль дублей цены) | ядро |
| H2/M1: восстановление/видимость `finalizing` | ядро |
| IDOR `/api/jobs` закрыт (коллега) | ядро |
| Дайджест-билдер + пагинация email (доведён до зелёного) | ядро |
| `rescore-recompute` (пересчёт баллов без OpenAI) | ядро |

Не-БД тесты зелёные (48). `test_api`/БД-интеграция локально не гонялись (нет httpx/PG; CI нет) — проверять на сервере.

---

## 1) Демо-фикс баллов БЕЗ NL — на ядре РФ

> Применяет НОВЫЙ блендинг (`final = max(ai, 0.2·kw + 0.8·ai)`) к уже сохранённым `ai_score` в `article_score_items`. Без OpenAI, без воркера. Статья, которой AI ставил 80 (а из-за старого keyword-якоря показывалось ~52), станет 80. `ai_score` не трогается → можно гонять повторно и поверх сделать полный AI-перепрогон.

На **ядре (РФ, СПб)**:
```bash
cd ~/oiltech-digest
git fetch origin && git reset --hard origin/main && docker compose up -d --build
docker exec oiltech_app python -m oiltech_digest.cli rescore-recompute
```
Ожидаемый вывод: `{"recomputed_article_scores": <N>, "keyword_weight": 0.2, "ai_weight": 0.8}`.

**Проверка распределения** (должны появиться 70+, средний выше прежних ~30):
```bash
docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest <<'SQL'
SELECT count(*) n, round(avg(total_score),1) avg, round(max(total_score),1) max,
       count(*) FILTER (WHERE total_score>=80) high,
       count(*) FILTER (WHERE total_score>=65) above_65
FROM article_scores;
SQL
```
Откатывать не нужно: исходные `ai_score`/`keyword_score` сохранены; полный AI-перепрогон позже перезапишет всё заново.

---

## 2) Когда NL-воркер поднимется — полный AI-перепрогон (лучшее качество)

Сначала дождись, что сервер отвечает (монитор у себя на маке):
```bash
until ping -c1 85.234.107.233 >/dev/null 2>&1; do echo "NL ещё лежит $(date +%H:%M)"; sleep 30; done; echo "NL ОТВЕЧАЕТ"
```

**Деплой воркера** (SSH на NL — команды по одной, без хвостов-комментариев):
```bash
ssh root@85.234.107.233
cd ~/oiltech-digest
git fetch origin && git reset --hard origin/main
grep OPENAI_SCORE .env.external-worker || printf 'OPENAI_SCORE_MODEL=gpt-5-mini\nOPENAI_SCORE_REASONING=medium\n' >> .env.external-worker
docker compose -f docker-compose.external-worker.yml up -d --build
docker logs --tail 20 oiltech_external_worker
```

**Пилот** (на ядре — поставит задачу в очередь → воркер NL обработает новым кодом+моделью):
```bash
docker exec oiltech_app python -m oiltech_digest.cli enqueue-process --limit 20
```
Через пару минут — распределение свежих:
```bash
docker exec -i oiltech_pg psql -U oiltech -d oiltech_digest <<'SQL'
SELECT COALESCE(model,'-') model, count(*) n, round(avg(total_score),1) avg,
       count(*) FILTER (WHERE total_score>=65) above_65
FROM article_scores WHERE created_at > now() - interval '15 min'
GROUP BY 1 ORDER BY n DESC;
SQL
```
Ждём: `model = gpt-5-mini`, хорошие статьи 70+. Если ок — пилот подтверждает, что воркер на правильной модели.

> Полный перепрогон всей базы через воркер (дорого, AI по каждой статье) — **только после пилота и решения Михаила**. Двойного счёта не будет (T2+H1). Готовой одной команды «пере-скорить всю базу» в CLI нет (score/enqueue-process берут только статьи без балла) — для существующих хватает `rescore-recompute` из п.1; полный AI-перепрогон делать прицельно (по article_id через `process-articles`/`enqueue-process` с id) или сбросив баллы — согласовать с Михаилом.

---

## Важное / подводные камни

- **Деплой-урок:** первый деплой объединённой схемы падал на `init-db` — `column "user_id" does not exist` (индекс по новой колонке стоял в CREATE-секции до идемпотентного ALTER). Исправлено (PR #21). Правило: любой `CREATE INDEX`/`CONSTRAINT`/`UPDATE` по НОВОЙ колонке — только ПОСЛЕ её `ALTER ADD COLUMN` в идемпотентной секции. `init-db` гонит `schema.sql` одной транзакцией → при падении откат целиком, повтор безопасен.
- **Если снова упадёт bootstrap:** `docker logs oiltech_bootstrap --tail 60`. Возможный кандидат — `monthly_digests` UNIQUE(month) WHERE user_id IS NULL при дублях-месяцах (маловероятно, таблица пустовала).
- **Очередь:** если Михаил уже ставил `enqueue-process` пока NL лежал — задачи висят в `background_jobs`, догонятся когда воркер вернётся. Вреда нет.

## Открытые задачи (НЕ блокеры демо)
- **T3 (recheck):** баг — `external_worker_complete` читает `job.get("payload")` вместо `payload_json` → `--mark`/`--dry-run` игнорируются, recheck всегда удаляет физически. **Чинить ДО любого relevance-recheck.**
- **T4:** экран AI-затрат считает один прайс на все модели (враньё дисплея, не дубль).
- T7 (`/api/process` admin-only), T8 (cookie Secure) — мелочи.

## Прод-карта
- Ядро РФ (СПб 109.68.213.12): БД (`oiltech_pg`) + app (`oiltech_app`) + UI + домен. Деплой: `git fetch && git reset --hard origin/main && docker compose up -d --build`.
- Воркер NL (ams 85.234.107.233): OpenAI через task-API. Деплой: `docker compose -f docker-compose.external-worker.yml up -d --build`. **Сейчас НЕДОСТУПЕН (инцидент охлаждения ДЦ Timeweb).**
