FROM python:3.13-slim

WORKDIR /app

# Зависимости (отдельным слоем для кэша)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения и статика админки
COPY oiltech_digest ./oiltech_digest
COPY web ./web

EXPOSE 8000

CMD ["uvicorn", "oiltech_digest.api:app", "--host", "0.0.0.0", "--port", "8000"]
