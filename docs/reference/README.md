# Пакет для выпуска email-дайджеста v1

## Файлы

1. `email_template.html`  
   Финальный HTML-шаблон письма. Его нельзя редактировать агенту Claude.

2. `digest_content.json`  
   Единственный файл, который должен заполнять Claude.

3. `claude_prompt.md`  
   Промпт для Claude, чтобы он не ломал дизайн и менял только контент.

4. `render_email.py`  
   Мини-скрипт для сборки готового HTML из шаблона и JSON.

5. `email_ready.html`  
   Появится после запуска `render_email.py`.

## Как работать

1. Отдать Claude файл `digest_content.json` и промпт `claude_prompt.md`.
2. Claude возвращает обновлённый JSON.
3. Сохранить результат в `digest_content.json`.
4. Запустить:

```bash
python render_email.py
```

5. Полученный файл `email_ready.html` использовать для тестовой отправки в корпоративной почте.

## Важное правило

Дизайн живёт в `email_template.html`.  
Контент живёт в `digest_content.json`.  
Claude не должен редактировать HTML.
