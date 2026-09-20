FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY server ./server
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "server.cloudflare_media:create_media_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
