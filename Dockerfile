FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY algobot ./algobot
COPY config ./config
COPY scripts ./scripts

RUN pip install --no-cache-dir .

RUN mkdir -p /app/data/cache

ENV PYTHONUNBUFFERED=1 TZ=Asia/Kolkata

# Entrypoint selected per docker-compose service:
#   engine    -> python -m algobot.engine.scheduler
#   api       -> uvicorn algobot.api.main:app --host 0.0.0.0 --port 8000
#   dashboard -> streamlit run algobot/dashboard/app.py --server.port 8501 --server.address 0.0.0.0
CMD ["python", "-m", "algobot.engine.scheduler"]
