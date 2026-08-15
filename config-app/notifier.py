"""E-Mail- und Webhook-Benachrichtigungen.

E-Mail in zwei Modi:
  - "smtp"   : Versand ueber einen konfigurierten SMTP-Host (Relay, zuverlaessig).
  - "direct" : der Server stellt selbst zu (MX-Lookup je Empfaenger-Domain,
               SMTP auf Port 25). Bequem, aber oft von Providern/Port 25 geblockt
               und ohne PTR/SPF/DKIM haeufig als Spam abgewiesen.
"""
import json
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


def recipients(n: dict) -> list:
    raw = (n.get("to_addrs") or "").replace(";", ",")
    return [a.strip() for a in raw.split(",") if a.strip()]


def _build(n: dict, subject: str, body: str, from_addr: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients(n))
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    return msg


def send_email(n: dict, subject: str, body: str) -> None:
    """Versendet eine E-Mail gemaess Konfiguration. Wirft bei Fehler."""
    to = recipients(n)
    if not to:
        raise ValueError("keine Empfaenger konfiguriert")
    from_addr = (n.get("from_addr") or "haproxy-dashboard@localhost").strip()
    msg = _build(n, subject, body, from_addr)
    if (n.get("email_mode") or "smtp").lower() == "direct":
        _send_direct(msg, from_addr, to)
    else:
        _send_smtp(n, msg, from_addr, to)


def _send_smtp(n: dict, msg: EmailMessage, from_addr: str, to: list) -> None:
    host = (n.get("smtp_host") or "").strip()
    if not host:
        raise ValueError("SMTP-Host nicht gesetzt")
    port = int(n.get("smtp_port") or 587)
    security = (n.get("smtp_security") or "starttls").lower()
    user, pw = n.get("smtp_user") or "", n.get("smtp_password") or ""
    ctx = ssl.create_default_context()
    if security == "ssl":
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=15) as s:
            if user:
                s.login(user, pw)
            s.send_message(msg, from_addr, to)
    else:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.ehlo()
            if security == "starttls":
                s.starttls(context=ctx)
                s.ehlo()
            if user:
                s.login(user, pw)
            s.send_message(msg, from_addr, to)


def _send_direct(msg: EmailMessage, from_addr: str, to: list) -> None:
    try:
        import dns.resolver  # dnspython
    except ImportError as e:
        raise RuntimeError("Direktversand benoetigt dnspython (im Image enthalten)") from e

    by_domain = {}
    for addr in to:
        by_domain.setdefault(addr.rsplit("@", 1)[-1], []).append(addr)

    errors = []
    for dom, addrs in by_domain.items():
        try:
            answers = dns.resolver.resolve(dom, "MX")
            mx = str(sorted(answers, key=lambda r: r.preference)[0].exchange).rstrip(".")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{dom}: MX-Lookup fehlgeschlagen ({e})")
            continue
        try:
            with smtplib.SMTP(mx, 25, timeout=20) as s:
                s.ehlo()
                try:
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                except smtplib.SMTPException:
                    pass  # opportunistisches TLS
                s.send_message(msg, from_addr, addrs)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{dom}: Zustellung an {mx}:25 fehlgeschlagen ({e})")
    if errors:
        raise RuntimeError("; ".join(errors))


def send_webhook(url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def dispatch(n: dict, event: str, subject: str, body: str, payload: dict) -> list:
    """Sendet je nach Konfiguration E-Mail und/oder Webhook.
    Liefert [(kanal, ok, fehlermeldung), ...]."""
    results = []
    if (n.get("events") or {}).get(event, True) is False:
        return results  # dieses Ereignis ist deaktiviert
    if recipients(n) and ((n.get("email_mode") == "direct") or n.get("smtp_host")):
        try:
            send_email(n, subject, body)
            results.append(("email", True, ""))
        except Exception as e:  # noqa: BLE001
            results.append(("email", False, str(e)))
    url = (n.get("webhook_url") or "").strip()
    if url:
        try:
            send_webhook(url, {"event": event, "subject": subject, "body": body, **payload})
            results.append(("webhook", True, ""))
        except Exception as e:  # noqa: BLE001
            results.append(("webhook", False, str(e)))
    return results
