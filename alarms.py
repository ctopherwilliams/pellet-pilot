"""Remote alarm delivery: Pushover, ntfy, and a generic webhook.

Security model (this is the point of the module):
  - HTTPS is required for every target.
  - The generic webhook is guarded against SSRF: the destination host is
    resolved and rejected if ANY resolved address is private, loopback,
    link-local (incl. cloud metadata 169.254.169.254), reserved, multicast,
    or unspecified. Redirects are disabled so a 3xx can't bounce to an
    internal host.
  - Set ALARM_ALLOW_PRIVATE=1 to permit private/LAN targets (self-hosted
    ntfy/webhook). This lowers SSRF protection — opt-in only.

Config via environment (never committed):
  PUSHOVER_TOKEN, PUSHOVER_USER
  NTFY_TOPIC            (server via NTFY_SERVER, default https://ntfy.sh)
  ALARM_WEBHOOK_URL     (POSTed JSON: {"title": ..., "message": ...})
"""
import ipaddress
import os
import socket
import urllib.parse

import requests

TIMEOUT = 10


class UnsafeURL(ValueError):
    pass


def _allow_private():
    return os.environ.get("ALARM_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")


def assert_safe_url(url):
    """Raise UnsafeURL unless `url` is HTTPS and resolves only to public IPs."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme != "https":
        raise UnsafeURL(f"refusing non-HTTPS URL (scheme={parts.scheme or 'none'})")
    host = parts.hostname
    if not host:
        raise UnsafeURL("URL has no host")
    if _allow_private():
        return True
    for *_, sockaddr in socket.getaddrinfo(host, parts.port or 443,
                                           proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(sockaddr[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise UnsafeURL(f"refusing request to non-public address {ip}")
    return True


def _post(url, **kw):
    # timeout is always set; redirects disabled so a 3xx can't reach an internal host.
    return requests.post(url, timeout=TIMEOUT, allow_redirects=False, **kw)


def pushover(title, message):
    token, user = os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER")
    if not (token and user):
        return False
    _post("https://api.pushover.net/1/messages.json",
          data={"token": token, "user": user, "title": title, "message": message})
    return True


def ntfy(title, message):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    url = f"{server}/{topic}"
    assert_safe_url(url)
    _post(url, data=message.encode(), headers={"Title": title})
    return True


def webhook(title, message):
    url = os.environ.get("ALARM_WEBHOOK_URL")
    if not url:
        return False
    assert_safe_url(url)
    _post(url, json={"title": title, "message": message})
    return True


def notify_remote(title, message):
    """Deliver to every configured provider. Best-effort; never raises."""
    sent = []
    for name, fn in (("pushover", pushover), ("ntfy", ntfy), ("webhook", webhook)):
        try:
            if fn(title, message):
                sent.append(name)
        except Exception as e:  # UnsafeURL, network errors, etc.
            print(f"  remote alarm ({name}) skipped: {e}")
    return sent
