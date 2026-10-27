FROM haproxy:3.4.7

USER root

# openssl wird fuer das selbstsignierte Fallback-Zertifikat benoetigt.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

# #25: feste Non-Root-UID/GID (1000). Der Entrypoint schreibt das kombinierte PEM
# nach /usr/local/etc/haproxy (deshalb hier anlegen + uebereignen) und bindet
# :80/:443 als Non-Root ueber das Sysctl net.ipv4.ip_unprivileged_port_start=0,
# das der Container setzt (docker-compose.yml). Niedrige Ports brauchen dann KEINE
# Capability -> cap_drop: ALL bleibt vollstaendig.
RUN groupadd -g 1000 app && useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin app \
    && mkdir -p /usr/local/etc/haproxy \
    && chown -R 1000:1000 /usr/local/etc/haproxy

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Ab hier als Non-Root laufen.
USER 1000:1000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
