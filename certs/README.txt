Place your SSL certificate here
===============================

Put your two files into THIS folder:

  - Certificate:  platform.crt   (or fullchain.pem / *.crt / *.pem)
  - Private key:  platform.key   (or privkey.pem / *.key)

When the haproxy container starts, it automatically builds the combined PEM
that HAProxy needs (certificate + chain + private key). You do NOT have to
concatenate anything manually.

If NO certificate is found, the container automatically generates a
self-signed certificate for your domain (for testing only).

Important:
  - If your certificate has an intermediate chain (CA bundle), it is best to use
    a fullchain.pem (server cert + intermediates in ONE file) as the .crt.
  - After replacing the certificate, restart the haproxy container:
        docker compose restart haproxy
