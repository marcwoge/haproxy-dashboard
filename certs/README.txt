SSL-Zertifikat hier ablegen
===========================

Lege deine beiden Dateien in DIESEN Ordner:

  - Zertifikat:  platform.crt   (oder fullchain.pem / *.crt / *.pem)
  - Schluessel:  platform.key   (oder privkey.pem / *.key)

Beim Start des haproxy-Containers wird daraus automatisch das von HAProxy
benoetigte kombinierte PEM gebaut (Zertifikat + Chain + privater Schluessel).
Du musst NICHTS manuell zusammenfuegen.

Wird KEIN Zertifikat gefunden, erzeugt der Container automatisch ein
selbstsigniertes Zertifikat fuer deine Domain (nur fuer Tests).

Wichtig:
  - Hat dein Zertifikat eine Zwischenkette (CA-Bundle), nutze am besten eine
    fullchain.pem (Server-Zert + Zwischenzertifikate in EINER Datei) als .crt.
  - Nach dem Austausch des Zertifikats den haproxy-Container neu starten:
        docker compose restart haproxy
