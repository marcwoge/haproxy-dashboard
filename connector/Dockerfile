FROM python:3.12-slim

# Version stamp (set by the release action with the release tag)
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY connector.py .

# Run as a non-root user; the connector needs no privileges and no writable FS.
RUN useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 connector
USER connector

# Graceful shutdown so the connector can deregister on `docker stop`.
STOPSIGNAL SIGTERM

ENTRYPOINT ["python", "connector.py"]
