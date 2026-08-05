FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

ENV LITE_CONFIG=/config/config.yaml \
    PYTHONUNBUFFERED=1

VOLUME ["/config", "/data"]
EXPOSE 8900

CMD ["python", "-m", "src.main", "--config", "/config/config.yaml"]
