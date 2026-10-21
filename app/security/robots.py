"""robots.txt checks (cached per origin). Fails open only when robots.txt is absent (404)."""

from __future__ import annotations

import logging
import threading
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)


class RobotsPolicy:
    def __init__(self, user_agent: str, timeout: float = 10.0):
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def _load(self, origin: str) -> RobotFileParser | None:
        rp = RobotFileParser()
        try:
            r = httpx.get(f"{origin}/robots.txt", timeout=self.timeout, follow_redirects=True,
                          headers={"User-Agent": self.user_agent})
        except httpx.HTTPError as e:
            log.warning("robots.txt unreachable for %s (%s); treating as allowed", origin, e)
            return None
        if r.status_code in (401, 403):
            rp.disallow_all = True
            return rp
        if r.status_code >= 400:
            return None  # no robots.txt => no restrictions
        rp.parse(r.text.splitlines())
        return rp

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self._lock:
            if origin not in self._cache:
                self._cache[origin] = self._load(origin)
            rp = self._cache[origin]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        p = urlparse(url)
        rp = self._cache.get(f"{p.scheme}://{p.netloc}")
        if rp is None:
            return None
        d = rp.crawl_delay(self.user_agent)
        return float(d) if d is not None else None
