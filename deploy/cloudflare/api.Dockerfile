FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY server ./server
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "server.production_app:create_production_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
