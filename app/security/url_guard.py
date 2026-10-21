"""Domain allowlist enforcement — the browser may only navigate to configured domains."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


class UrlNotAllowed(Exception):
    """Raised when a URL is outside the website's allowlist or otherwise unsafe."""


def host_allowed(host: str, allowed_domains: list[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    for d in allowed_domains:
        d = d.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def _is_private_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


class UrlGuard:
    """Validates navigation targets. An explicit allowlist entry is the only way to reach an IP/localhost."""

    def __init__(self, allowed_domains: list[str]):
        if not allowed_domains:
            raise ValueError("an allowlist with at least one domain is required")
        self.allowed_domains = [d.lower().lstrip(".") for d in allowed_domains]

    def check(self, url: str) -> str:
        try:
            p = urlparse(url)
        except ValueError as e:  # malformed IPv6 etc.
            raise UrlNotAllowed(f"malformed URL: {url!r}") from e
        if p.scheme not in ("http", "https"):
            raise UrlNotAllowed(f"scheme {p.scheme!r} not allowed")
        if p.username or p.password:
            raise UrlNotAllowed("credentials embedded in URLs are not allowed")
        host = (p.hostname or "").lower()
        if not host:
            raise UrlNotAllowed("URL has no host")
        explicitly_listed = host in self.allowed_domains
        if (_is_private_ip(host) or host == "localhost") and not explicitly_listed:
            raise UrlNotAllowed(f"private/loopback host {host!r} not allowed")
        if not host_allowed(host, self.allowed_domains):
            raise UrlNotAllowed(f"{host!r} is not in the allowlist {self.allowed_domains}")
        return url

    def is_allowed(self, url: str) -> bool:
        try:
            self.check(url)
            return True
        except UrlNotAllowed:
            return False
