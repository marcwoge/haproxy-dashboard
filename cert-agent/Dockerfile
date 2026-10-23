# Pinned by digest (acme.sh v3.1.7); the image's tags don't track the acme.sh version.
FROM neilpang/acme.sh@sha256:b0a3af2a3163ead9d35434fa117a94bed2717d893c0923e9c1e347e78cae4dc6

# Version stamp (set by the release action with the release tag)
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Graceful shutdown.
STOPSIGNAL SIGTERM

ENTRYPOINT ["/entrypoint.sh"]
