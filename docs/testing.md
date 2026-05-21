# Тестирование OilTech Digest

Этот чеклист нужен перед привязкой backend к frontend.

## 1. Локальные unit-тесты

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
```

Ожидаемо: все тесты зелёные.

## 2. Smoke test БД без OpenAI

Нужен запущенный Docker daemon.

```bash
cp .env.example .env
docker compose up -d db
source .venv/bin/activate

python -m oiltech_digest.cli init-db
python -m oiltech_digest.cli seed-sources
python -m oiltech_digest.cli seed-tags
python -m oiltech_digest.cli seed-scoring
python -m oiltech_digest.cli stats
```

Ожидаемо:

- схема создаётся без ошибок;
- `seed-sources` загружает каталог источников;
- `seed-tags` создаёт 18 верхнеуровневых тегов D01-D18;
- `seed-scoring` создаёт 4 критерия, сумма весов 100.

## 3. Smoke test ingestion

```bash
python -m oiltech_digest.cli discover-rss --workers 10 --timeout 4
python -m oiltech_digest.cli parse --workers 10
python -m oiltech_digest.cli stats
```

Если нужно проверить пайплайн быстро, можно идти батчами:

```bash
python -m oiltech_digest.cli discover-rss --limit 20 --timeout 3
python -m oiltech_digest.cli parse --workers 10
```

Ожидаемо:

- есть RSS-источники с `parse_strategy=rss`;
- статьи появляются в `articles`;
- повторный `parse` добавляет 0 или мало новых статей из-за дедупликации URL.

## 4. Offline AI smoke test

Проверяет связи таблиц без расходов OpenAI.

```bash
python -m oiltech_digest.cli process --offline --limit 5
python -m oiltech_digest.cli ai-cost-report
python -m oiltech_digest.cli ai-article-cost-report
```

Ожидаемо:

- появляются записи в `article_cards`;
- появляются `article_tags`;
- появляются `article_scores` и `article_score_items`;
- `ai-cost-report` показывает этапы `summary`, `tagging`, `scoring`.
- `ai-article-cost-report` показывает стоимость полного цикла на 1 статью.

## 5. OpenAI API pilot

Перед запуском вписать в `.env`:

```bash
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5-nano
```

Пилотный прогон:

```bash
python -m oiltech_digest.cli summarize --limit 3
python -m oiltech_digest.cli tag --limit 3
python -m oiltech_digest.cli score --limit 3
python -m oiltech_digest.cli ai-cost-report
python -m oiltech_digest.cli ai-article-cost-report
```

Проверить вручную:

- summary: 2-3 русских предложения, без выдуманных фактов;
- tag: тег соответствует D01-D18;
- score: объяснение не противоречит статье;
- токены и стоимость записались в `ai_processing_runs`.
- средняя стоимость полного прогона 1 статьи понятна и зафиксирована.

## 6. RU/EN проверка для issues #3 и #10

Сделать две выборки по `articles.language`: `ru` и `en`, прогнать одинаковый лимит
через `summarize`, `tag`, `score`, затем сравнить `ai-cost-report`.
Для оценки экономики одной статьи использовать `ai-article-cost-report`.

Минимальный критерий готовности:

- для RU и EN есть успешные записи всех трёх этапов;
- нет систематического смещения тегов в один и тот же D-код;
- средние токены/стоимость по RU и EN записаны и понятны.

## 7. Черновик дайджеста

```bash
python -m oiltech_digest.cli digest-content 2026-05 --output digest_content.generated.json
```

Ожидаемо: JSON содержит `month`, `title`, `items[]` с title/source/url/tag/score/summary.
