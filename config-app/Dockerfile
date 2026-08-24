FROM python:3.14-slim

# Versionsstempel (wird von der GitHub Action mit dem Release-Tag gesetzt)
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION
# M1: kein Bytecode-Schreiben (read-only Root-FS) + ungepuffertes Log
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["python", "app.py"]
