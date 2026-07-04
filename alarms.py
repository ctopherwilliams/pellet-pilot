"""Remote alarm delivery: Pushover, ntfy, and a generic webhook.

Security model (this is the point of the module):
  - HTTPS is required for every target.
  - The generic webhook is guarded against SSRF: the destination host is
    resolved and rejected if ANY resolved address is private, loopback,
    link-local (incl. cloud metadata 169.254.169.254), reserved, multicast,
    or unspecified. Redirects are disabled so a 3xx can't bounce to an
    internal host.
  - The validated IP is pinned for the actual connection (see _pin_dns), so a
    DNS answer that changes between the check and the request -- a rebind --
    can't bypass the guard.
  - Set ALARM_ALLOW_PRIVATE=1 to permit private/LAN targets (self-hosted
    ntfy/webhook). This lowers SSRF protection — opt-in only.

Config via environment (never committed):
  PUSHOVER_TOKEN, PUSHOVER_USER
  NTFY_TOPIC            (server via NTFY_SERVER, default https://ntfy.sh)
  ALARM_WEBHOOK_URL     (POSTed JSON: {"title": ..., "message": ...})
"""
import contextlib
import ipaddress
import os
import socket
import urllib.parse

import requests

try:
    import urllib3.util.connection as _urllib3_connection
except ImportError:  # pragma: no cover - transitively vendored by requests
    _urllib3_connection = None

TIMEOUT = 10


class UnsafeURL(ValueError):
    pass


def _allow_private():
    return os.environ.get("ALARM_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")


def _resolve_safe_ips(host, port):
    """Resolve `host` and raise UnsafeURL if any resolved address is non-public.

    Returns the resolved IPs so a caller can pin the connection to one of them.
    """
    ips = []
    for *_, sockaddr in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(sockaddr[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise UnsafeURL(f"refusing request to non-public address {ip}")
        ips.append(sockaddr[0])
    return ips


def assert_safe_url(url):
    """Raise UnsafeURL unless `url` is HTTPS and resolves only to public IPs.

    Fail-fast pre-check for a clear error before building a request payload.
    The real enforcement boundary is `_post`, which re-validates and pins the
    connection immediately before sending (see _pin_dns) -- DNS could answer
    differently between this check and the request otherwise.
    """
    parts = urllib.parse.urlparse(url)
    if parts.scheme != "https":
        raise UnsafeURL(f"refusing non-HTTPS URL (scheme={parts.scheme or 'none'})")
    if not parts.hostname:
        raise UnsafeURL("URL has no host")
    if not _allow_private():
        _resolve_safe_ips(parts.hostname, parts.port or 443)
    return True


@contextlib.contextmanager
def _pin_dns(ip):
    """Force the next connection to `ip`, closing the gap between the safety
    check's DNS resolution and the actual request's (a rebinding DNS answer
    could otherwise differ between the two). TLS still validates against the
    original hostname -- only the raw TCP destination is pinned here; SNI and
    certificate hostname checks use the connection's `host`, untouched.

    Scoped narrowly around a single outbound POST; alarm delivery in this
    codebase runs synchronously from a single-threaded loop, so a brief
    process-wide patch is safe. Degrades to a no-op (falling back to the
    check-time-only guarantee) if urllib3's internals aren't importable.
    """
    if ip is None or _urllib3_connection is None:
        yield
        return
    orig = _urllib3_connection.create_connection

    def pinned(address, *args, **kwargs):
        return orig((ip, address[1]), *args, **kwargs)

    _urllib3_connection.create_connection = pinned
    try:
        yield
    finally:
        _urllib3_connection.create_connection = orig


def _post(url, **kw):
    parts = urllib.parse.urlparse(url)
    ip = None
    if parts.hostname and not _allow_private():
        ip = _resolve_safe_ips(parts.hostname, parts.port or 443)[0]
    # timeout is always set; redirects disabled so a 3xx can't reach an internal host.
    with _pin_dns(ip):
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
