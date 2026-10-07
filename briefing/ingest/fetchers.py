"""Per-kind feed readers. Each returns RawEntry records; no full text yet."""

from __future__ import annotations

import calendar
import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser

from briefing.config import Feed
from briefing.http import Fetcher

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class RawEntry:
    url: str
    title: str = ""
    published: datetime | None = None
    author: str | None = None
    summary: str = ""          # feed-provided summary or content, plain text
    extra: dict[str, Any] = field(default_factory=dict)
    needs_article: bool = True  # fetch the page for full text


def strip_html(value: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value or ""))).strip()


def _struct_to_dt(st) -> datetime | None:
    if not st:
        return None
    return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)


def matches_keywords(text: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    low = text.lower()
    for kw in keywords:
        k = kw.lower()
        # whole-word match for short tokens like "AI", substring for phrases
        if len(k) <= 3:
            if re.search(rf"\b{re.escape(k)}\b", low):
                return True
        elif k in low:
            return True
    return False


# -- RSS / Atom -----------------------------------------------------------

def fetch_rss(fetcher: Fetcher, feed: Feed) -> list[RawEntry]:
    resp = fetcher.get(feed.url)
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"unparseable feed: {parsed.bozo_exception}")
    out = []
    for e in parsed.entries:
        link = e.get("link")
        if not link:
            continue
        content = ""
        if e.get("content"):
            content = " ".join(c.get("value", "") for c in e.content)
        summary = strip_html(content or e.get("summary", ""))
        out.append(
            RawEntry(
                url=link,
                title=strip_html(e.get("title", "")),
                published=_struct_to_dt(e.get("published_parsed") or e.get("updated_parsed")),
                author=e.get("author"),
                summary=summary,
            )
        )
    return out


# -- Hugging Face Daily Papers --------------------------------------------

def fetch_hf_daily_papers(fetcher: Fetcher, feed: Feed, since: date, until: date) -> list[RawEntry]:
    """One request per day in [since, until); keep the most-upvoted papers per day."""
    out: list[RawEntry] = []
    day = since
    while day < until:
        resp = fetcher.get(feed.url, params={"date": day.isoformat(), "limit": 100})
        rows = resp.json() or []
        papers = []
        for row in rows:
            paper = row.get("paper") or {}
            pid = paper.get("id")
            upvotes = int(paper.get("upvotes") or 0)
            if not pid or upvotes < feed.min_upvotes:
                continue
            papers.append((upvotes, pid, row, paper))
        papers.sort(key=lambda p: p[0], reverse=True)
        for upvotes, pid, row, paper in papers[: feed.top_per_day]:
            authors = [a.get("name") for a in paper.get("authors") or [] if a.get("name")]
            ai_summary = paper.get("ai_summary") or ""
            abstract = paper.get("summary") or row.get("summary") or ""
            out.append(
                RawEntry(
                    url=f"https://huggingface.co/papers/{pid}",
                    title=(paper.get("title") or row.get("title") or "").strip(),
                    published=datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc),
                    author=", ".join(authors[:5]) or None,
                    summary=(abstract + ("\n\nSummary: " + ai_summary if ai_summary else "")).strip(),
                    extra={"upvotes": upvotes, "arxiv_id": pid},
                    needs_article=False,  # the abstract is the content
                )
            )
        day += timedelta(days=1)
    return out


# -- Listing-page scrape --------------------------------------------------

class _LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


def extract_links(base_url: str, page_html: str, link_pattern: str, limit: int) -> list[str]:
    parser = _LinkCollector()
    parser.feed(page_html)
    base_host = urlsplit(base_url).netloc.lower()
    pattern = re.compile(link_pattern)
    seen, out = set(), []
    for href in parser.links:
        absolute = urljoin(base_url, href.split("#")[0])
        parts = urlsplit(absolute)
        if parts.netloc.lower() != base_host:
            continue
        if not pattern.search(parts.path):
            continue
        if parts.path.rstrip("/") == urlsplit(base_url).path.rstrip("/"):
            continue
        key = absolute.rstrip("/")
        if key not in seen:
            seen.add(key)
            out.append(absolute)
        if len(out) >= limit:
            break
    return out


def fetch_listing(fetcher: Fetcher, feed: Feed) -> list[RawEntry]:
    resp = fetcher.get(feed.url)
    links = extract_links(feed.url, resp.text, feed.link_pattern or "", feed.max_items)
    return [RawEntry(url=link) for link in links]
