FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
    libjpeg62-turbo libopenjp2-7 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend backend
COPY data data
COPY templates templates
COPY frontend/public/brand frontend/public/brand
RUN mkdir -p generated/quotations uploads

EXPOSE 5005
CMD ["gunicorn", "--chdir", "backend", "--bind", "0.0.0.0:5005", "--workers", "2", "--threads", "4", "--timeout", "120", "run:app"]

