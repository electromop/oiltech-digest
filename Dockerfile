FROM python:3.13-slim

WORKDIR /app

# Зависимости (отдельным слоем для кэша)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения и статика админки
COPY oiltech_digest ./oiltech_digest
COPY web ./web
COPY scripts ./scripts
COPY 1_Список_источников_для_дайджеста.xlsx .
COPY 2_Направления_и_ключевые_слова.xlsx .

RUN mkdir -p /app/exports \
    && chmod +x ./scripts/docker-scheduler.sh

EXPOSE 8000

CMD ["uvicorn", "oiltech_digest.api:app", "--host", "0.0.0.0", "--port", "8000"]
