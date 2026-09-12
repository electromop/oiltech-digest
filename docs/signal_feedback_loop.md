# Signal feedback loop

ОС по найденным сигналам не дообучает модель через fine-tuning. Она сохраняется как явная память агента и применяется уже в следующих прогонах радара.

## Что сохраняется

- Сырой комментарий пользователя: `signal_feedback_events`.
- Snapshot каждого решения signal-agent: `signal_generation_runs` и `signal_training_examples`.
- Словарь терминов: `signal_agent_memory.memory_type = signal_glossary`.
- Правила качества и отбора: `signal_agent_memory.memory_type = signal_quality_rule`.
- Поисковые углы: `signal_agent_memory.memory_type = signal_query_hint`.
- Предпочтительные источники: `signal_agent_memory.memory_type = signal_source_preference`.
- Исправления заголовков: `signal_agent_memory.memory_type = signal_title_correction`.

Запись ОС может быть привязана к `article_id`, `signal_id`, `signal_evidence_id` или только к `source_url`, если сигнал найден через web-поиск и не имеет локальной статьи.

## Как применяется

- `signal_query_hint` добавляется в начало web-запросов, перед дефолтными query seeds.
- `signal_source_preference` добавляет мягкие `site:`-подсказки, но не ограничивает поиск только этими источниками.
- `signal_quality_rule` и `signal_glossary` добавляются в prompt оценки сигнала.
- `signal_glossary` дополнительно применяется к русским полям карточки после генерации.
- Новая ОС привязывается к последним `signal_training_examples` того же `signal_id`, чтобы затем выгружать пары "вход -> ответ модели -> правка человека".

## Экспорт для eval/training

По умолчанию выгружаются только примеры, к которым уже привязана человеческая ОС:

```bash
python -m oiltech_digest.cli export-signal-training-jsonl
```

Выгрузить все snapshot'ы, включая еще не оцененные:

```bash
python -m oiltech_digest.cli export-signal-training-jsonl --all
```

JSONL пишется в `exports/signal_training_examples.jsonl`. Если файл пустой, значит после добавления `signal_training_examples` еще не было полного `discover-signals --no-dry-run` и новой админской ОС по найденным сигналам.

## Перенос контекста на сервер

Для переноса уже найденных сигналов, evidence, тем радара, памяти signal-agent и ОС:

```bash
python -m oiltech_digest.cli export-signal-context --path exports/signal_context_bundle.json
```

На сервере сначала проверить, что будет импортировано:

```bash
python -m oiltech_digest.cli import-signal-context exports/signal_context_bundle.json --dry-run
```

Применить:

```bash
python -m oiltech_digest.cli import-signal-context exports/signal_context_bundle.json --no-dry-run
```

Импорт не использует локальные `id` сигналов: связи восстанавливаются по стабильному `signal_key`, поэтому bundle можно безопасно применять к серверной БД с другой нумерацией.

## Импорт из Google Sheets CSV

Сначала dry-run:

```bash
python -m oiltech_digest.cli import-signal-feedback "path/to/feedback.csv" --dry-run
```

Запись в БД:

```bash
python -m oiltech_digest.cli import-signal-feedback "path/to/feedback.csv" --no-dry-run
```

## API для фронта

```http
POST /api/signals/feedback
```

Минимальный payload:

```json
{
  "signal_id": 123,
  "source_url": "https://example.com/news",
  "signal_title": "Closed-loop drilling",
  "comment": "closed-loop control -> управление с замкнутым контуром"
}
```

Память signal-agent можно читать отдельно от памяти агента источников:

```http
GET /api/signals/memory
```
