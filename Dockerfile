FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRICE_BOT_ROOT=/app \
    MAPPING_PATH=/data/mapping.txt \
    REPORTS_DIR=/data/reports

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["sh", "/app/deployment/start.sh"]
