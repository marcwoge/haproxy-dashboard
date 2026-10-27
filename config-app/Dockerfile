FROM python:3.14.9-slim

# Versionsstempel (wird von der GitHub Action mit dem Release-Tag gesetzt)
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION
# M1: kein Bytecode-Schreiben (read-only Root-FS) + ungepuffertes Log
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# #25: feste Non-Root-UID/GID (1000). Die App schreibt ausschliesslich in die
# gemounteten Volumes (/app/config, /app/haproxy_out, /app/update) + /tmp; diese
# Bind-Mounts muessen auf dem Host dieser UID gehoeren (siehe README "Upgrade"
# und die Startmeldung des Entrypoints bei fehlenden Rechten).
RUN groupadd -g 1000 app && useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# Ab hier als Non-Root laufen (der Code unter /app ist world-readable).
USER 1000:1000

CMD ["python", "app.py"]
