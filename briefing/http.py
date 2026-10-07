"""HTTP client with retries, backoff and per-host politeness."""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import httpx

from briefing.config import IngestConfig

RETRY_STATUS = {429, 500, 502, 503, 504}


class Fetcher:
    """Thin wrapper over httpx.Client. Tests inject a client with a MockTransport."""

    def __init__(self, cfg: IngestConfig, client: httpx.Client | None = None, sleep=time.sleep):
        self.cfg = cfg
        self.client = client or httpx.Client(
            timeout=cfg.request_timeout_s,
            follow_redirects=True,
            headers={"User-Agent": cfg.user_agent, "Accept-Language": "en,*;q=0.5"},
            http2=False,
        )
        self._sleep = sleep
        self._last_hit: dict[str, float] = {}

    def _polite(self, url: str) -> None:
        host = urlsplit(url).netloc
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.cfg.request_delay_s - (time.monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_hit[host] = time.monotonic()

    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        attempt = 0
        while True:
            self._polite(url)
            try:
                resp = self.client.get(url, params=params)
            except httpx.TransportError:
                if attempt >= self.cfg.max_retries:
                    raise
            else:
                if resp.status_code not in RETRY_STATUS or attempt >= self.cfg.max_retries:
                    resp.raise_for_status()
                    return resp
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    self._sleep(min(int(retry_after), 60))
            attempt += 1
            self._sleep(min(2 ** attempt, 30))

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
