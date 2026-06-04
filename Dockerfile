FROM python:3.13-slim

WORKDIR /app

# Зависимости (отдельным слоем для кэша)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium для PDF-экспорта дайджеста (Playwright). Тяжёлый слой (~400 МБ): собирать
# при остановленном стеке (`docker compose down` сначала) — на 1.9 ГБ RAM иначе OOM.
RUN python -m playwright install --with-deps chromium

# Код приложения и статика админки
COPY oiltech_digest ./oiltech_digest
COPY web ./web
COPY scripts ./scripts
COPY data/seed ./data/seed

RUN mkdir -p /app/exports \
    && chmod +x ./scripts/docker-scheduler.sh

EXPOSE 8000

CMD ["uvicorn", "oiltech_digest.api:app", "--host", "0.0.0.0", "--port", "8000"]
