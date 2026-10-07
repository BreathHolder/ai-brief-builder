"""A small, read-only web server for data/public/ (the podcast feed and episodes).

Standard library only. Supports HTTP Range requests so podcast apps can resume
downloads and seek. Serves only the public directory; no directory listings.
"""

from __future__ import annotations

import logging
import os
import re
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("briefing")
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


class FeedHandler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".xml": "application/rss+xml; charset=utf-8",
        ".mp3": "audio/mpeg",
        ".json": "application/json",
    }

    def log_message(self, fmt, *args):  # route access logs through our logger (journald under systemd)
        log.info("%s %s", self.address_string(), fmt % args)

    def list_directory(self, path):
        self.send_error(HTTPStatus.NOT_FOUND)
        return None

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache" if self.path.endswith(".xml") else "public, max-age=86400")
        super().end_headers()

    def send_head(self):
        path = Path(self.translate_path(self.path))
        rng = self.headers.get("Range")
        if not rng or not path.is_file():
            if path.is_dir() and self.path.rstrip("/") in ("", "/"):
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/feed.xml")
                self.end_headers()
                return None
            return super().send_head()

        size = path.stat().st_size
        m = _RANGE_RE.match(rng.strip())
        if not m or (not m.group(1) and not m.group(2)):
            return super().send_head()
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:  # suffix range: last N bytes
            start, end = max(0, size - int(m.group(2))), size - 1
        if start >= size or start > end:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        end = min(end, size - 1)

        f = open(path, "rb")
        f.seek(start)
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        while remaining > 0:
            chunk = source.read(min(64 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)
        self._remaining = None


def make_server(public_dir: Path, host: str, port: int) -> ThreadingHTTPServer:
    public_dir.mkdir(parents=True, exist_ok=True)
    handler = partial(FeedHandler, directory=os.fspath(public_dir))
    return ThreadingHTTPServer((host, port), handler)
