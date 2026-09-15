FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
CMD ["uvicorn", "photo_server.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
