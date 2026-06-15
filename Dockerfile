# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Cloud Run sends traffic to $PORT; serve the HTTP entrypoint.
CMD ["sh", "-c", "uvicorn edca.entrypoints.handler:app --host 0.0.0.0 --port ${PORT}"]
