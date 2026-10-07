"""Full-text and metadata extraction from article pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import json
import re

import trafilatura

from briefing.http import Fetcher


@dataclass
class Article:
    text: str
    title: str | None
    author: str | None
    published: datetime | None
    final_url: str | None = None  # after redirects


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_META_DATE_RE = re.compile(
    r'<meta[^>]+(?:property|name|itemprop)=["\'](?:article:published_time|og:published_time|'
    r'datePublished|pubdate|publish-date|date|dc\.date|DC\.date\.issued)["\'][^>]*content=["\']([^"\']+)["\']',
    re.I,
)
_META_DATE_REV_RE = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name|itemprop)=["\'](?:article:published_time|'
    r'og:published_time|datePublished)["\']',
    re.I,
)
_TIME_RE = re.compile(r'<time[^>]+datetime=["\']([^"\']+)["\']', re.I)
_JSONLD_RE = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.I | re.S)


def _jsonld_date(page_html: str) -> str | None:
    for block in _JSONLD_RE.findall(page_html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if isinstance(node.get("datePublished"), str):
                    return node["datePublished"]
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)
    return None


def explicit_date(page_html: str) -> datetime | None:
    """Publication date the page states outright (meta tags, JSON-LD, <time>)."""
    for candidate in (
        *(m.group(1) for m in (_META_DATE_RE.search(page_html), _META_DATE_REV_RE.search(page_html)) if m),
        _jsonld_date(page_html),
        *(m.group(1) for m in [_TIME_RE.search(page_html)] if m),
    ):
        dt = _parse_date(candidate.replace("Z", "+00:00") if candidate else None)
        if dt:
            return dt
    return None


def parse_article(page_html: str, url: str, max_chars: int) -> Article:
    text = trafilatura.extract(
        page_html, url=url, include_comments=False, include_tables=True, favor_precision=True
    ) or ""
    meta = trafilatura.extract_metadata(page_html, default_url=url)
    return Article(
        text=text[:max_chars],
        title=(meta.title if meta else None),
        author=(meta.author if meta else None),
        # Prefer what the page states; trafilatura's date guessing is the fallback.
        published=explicit_date(page_html) or _parse_date(meta.date if meta else None),
    )


def fetch_article(fetcher: Fetcher, url: str, max_chars: int) -> Article:
    resp = fetcher.get(url)
    article = parse_article(resp.text, url, max_chars)
    article.final_url = str(resp.url)
    return article
