"""Stage 1: pull the week's items from every enabled source, with full text.

Output items are in-window, keyword-scoped (where a feed is broad) and carry
full article text. Translation and deduplication happen in `normalise`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from briefing.config import AppConfig, Feed, Source
from briefing.context import RunContext
from briefing.http import Fetcher
from briefing.ingest.extract import fetch_article
from briefing.ingest.fetchers import (
    RawEntry,
    fetch_hf_daily_papers,
    fetch_listing,
    fetch_rss,
    matches_keywords,
)
from briefing.models import Item
from briefing.store import Store
from briefing.urls import canonical_url
from urllib.parse import urlsplit


def clean_title(title: str, feed: Feed) -> str:
    for pat in feed.title_strip:
        title = re.sub(pat, "", title)
    return title.strip()


def left_the_listing(feed: Feed, final_url: str | None) -> bool:
    """A scraped link that redirects off the blog (e.g. to docs) is not a post."""
    if feed.kind != "scrape" or not final_url:
        return False
    listing, final = urlsplit(feed.url), urlsplit(final_url)
    same_host = final.netloc.lower().removeprefix("www.") == listing.netloc.lower().removeprefix("www.")
    return not (same_host and re.search(feed.link_pattern or "", final.path))


def make_fetcher(config: AppConfig) -> Fetcher:
    """Factory, patched in tests to inject a mock transport."""
    return Fetcher(config.settings.ingest)


def open_store(config: AppConfig) -> Store:
    return Store(config.cache_dir / "briefing.sqlite")


def window_bounds(ctx: RunContext) -> tuple[datetime, datetime]:
    d = ctx.episode_date
    until = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)
    return until - timedelta(days=ctx.config.settings.ingest.window_days), until


def _list_entries(fetcher: Fetcher, feed: Feed, since: datetime, until: datetime) -> list[RawEntry]:
    if feed.kind == "rss":
        return fetch_rss(fetcher, feed)[: feed.max_items * 3]
    if feed.kind == "hf_daily_papers":
        return fetch_hf_daily_papers(fetcher, feed, since.date(), until.date())
    if feed.kind == "scrape":
        return fetch_listing(fetcher, feed)
    raise ValueError(f"unknown feed kind {feed.kind}")


BLOCKING_STATUSES = {401, 403}


def collect_feed(
    ctx: RunContext, fetcher: Fetcher, store: Store, source: Source, feed: Feed,
    since: datetime, until: datetime, blocked_hosts: set[str] | None = None,
) -> tuple[list[Item], dict[str, int]]:
    max_chars = ctx.config.settings.ingest.max_text_chars
    stats = {"entries": 0, "in_window": 0, "keyword_skipped": 0, "fetched": 0, "fetch_errors": 0,
             "undated_skipped": 0, "redirect_skipped": 0, "feed_text_only": 0}
    blocked_hosts = blocked_hosts if blocked_hosts is not None else set()
    baseline = store.has_baseline(feed.url)
    items: list[Item] = []

    entries = _list_entries(fetcher, feed, since, until)
    stats["entries"] = len(entries)

    for entry in entries:
        canon = canonical_url(entry.url)
        cached = store.get_article(canon)
        if cached and cached["skip_reason"]:
            continue  # known non-post (e.g. redirects to docs); decided on an earlier run
        cached_pub = datetime.fromisoformat(cached["published"]) if cached and cached["published"] else None
        published = entry.published or cached_pub

        # Known date outside the window: skip before spending a fetch.
        if published and not (since <= published < until):
            store.save_article(url_canon=canon, url=entry.url, source_id=source.id, feed_url=feed.url,
                               title=entry.title or None, text=None, published=published,
                               author=entry.author, fetched=False)
            continue

        # Cheap keyword scoping on what the feed gave us, before fetching.
        if entry.title and feed.include_keywords and not matches_keywords(
            f"{entry.title} {entry.summary}", feed.include_keywords
        ):
            stats["keyword_skipped"] += 1
            continue

        title, text, author = entry.title, entry.summary, entry.author
        host = urlsplit(entry.url).netloc.lower()
        wants_article = entry.needs_article and (feed.fetch_articles or feed.kind == "scrape")
        if wants_article and host in blocked_hosts and not (cached and cached["text"]):
            wants_article = False  # this host refused us earlier in the run; don't keep knocking
        if not wants_article and entry.needs_article:
            stats["feed_text_only"] += 1
        if wants_article:
            if cached and cached["text"]:
                text = cached["text"]
                title = title or cached["title"] or ""
            else:
                try:
                    art = fetch_article(fetcher, entry.url, max_chars)
                    stats["fetched"] += 1
                    if left_the_listing(feed, art.final_url):
                        stats["redirect_skipped"] += 1
                        ctx.log(f"{source.id}: {entry.url} redirects to {art.final_url}, not a post")
                        store.save_article(url_canon=canon, url=entry.url, source_id=source.id, feed_url=feed.url,
                                           title=None, text=None, published=None, author=None, fetched=True,
                                           skip_reason=f"redirects to {art.final_url}")
                        continue
                    text = art.text or entry.summary
                    title = title or art.title or ""
                    author = author or art.author
                    published = published or art.published
                except Exception as exc:
                    stats["fetch_errors"] += 1
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status in BLOCKING_STATUSES:
                        blocked_hosts.add(host)
                        ctx.log(f"{source.id}: {host} refused automated access ({status}); "
                                f"using feed text for the rest of this run", logging.WARNING)
                    else:
                        ctx.log(f"{source.id}: article fetch failed for {entry.url}: {exc}", logging.WARNING)

        first_seen = store.save_article(
            url_canon=canon, url=entry.url, source_id=source.id, feed_url=feed.url,
            title=title or None, text=text or None, published=published, author=author,
            fetched=bool(text),
        )

        if published is None:
            # Undated (some scraped pages): trust first-seen only once the feed
            # has a baseline, otherwise the first run would flood in the archive.
            if not baseline:
                stats["undated_skipped"] += 1
                continue
            published = datetime.fromisoformat(first_seen)

        if not (since <= published < until):
            continue
        title = clean_title(title or "", feed)
        if not title:
            continue
        # Scope on what the item is ABOUT: title and summary (or the opening of a
        # scraped page). A passing mention deep in the body doesn't count.
        about = f"{title} {entry.summary}" if entry.summary else f"{title} {text[:600]}"
        if feed.include_keywords and not matches_keywords(about, feed.include_keywords):
            stats["keyword_skipped"] += 1
            continue

        if feed.title_prefix and not title.lower().startswith(feed.title_prefix.lower()):
            title = f"{feed.title_prefix} {title}"
        stats["in_window"] += 1
        items.append(
            Item(
                source_id=source.id, vendor=source.vendor, tier=source.tier, feed_url=feed.url,
                title=title.strip(), url=entry.url, url_canon=canon, published=published,
                author=author, text=text or "", summary=(entry.summary[:1000] or None),
                extra=entry.extra,
            )
        )

    if feed.kind == "scrape":
        store.set_baseline(feed.url)
    return items, stats


def health_warnings(store: Store, sources: list[Source], n: int) -> list[str]:
    out = []
    for s in sources:
        if not s.enabled:
            continue
        runs = store.recent_source_runs(s.id, n)
        if len(runs) == n and all(r["items"] == 0 for r in runs):
            last_err = next((r["error"] for r in runs if r["error"]), None)
            msg = f"{s.name}: no items for {n} consecutive runs"
            out.append(msg + (f" (last error: {last_err})" if last_err else ""))
    return out


def run(ctx: RunContext, inputs: dict[str, Any]) -> dict[str, Any]:
    cfg = ctx.config
    since, until = window_bounds(ctx)
    store = open_store(cfg)
    all_items: list[Item] = []
    per_source: dict[str, dict[str, Any]] = {}
    blocked_hosts: set[str] = set()

    try:
        with make_fetcher(cfg) as fetcher:
            for source in cfg.sources:
                if not source.enabled:
                    per_source[source.id] = {"status": "disabled", "items": 0}
                    continue
                src_items: list[Item] = []
                feed_stats, errors = {}, []
                for feed in source.feeds:
                    try:
                        items, stats = collect_feed(ctx, fetcher, store, source, feed, since, until, blocked_hosts)
                        src_items += items
                        feed_stats[feed.url] = stats
                    except Exception as exc:
                        errors.append(f"{feed.url}: {exc}")
                        feed_stats[feed.url] = {"error": str(exc)}
                        ctx.log(f"{source.id}: feed failed {feed.url}: {exc}", logging.WARNING)

                status = "ok" if not errors else ("partial" if len(errors) < len(source.feeds) else "error")
                per_source[source.id] = {"status": status, "items": len(src_items), "feeds": feed_stats}
                store.record_source_run(ctx.run_id, source.id, status, len(src_items), "; ".join(errors) or None)
                ctx.log(f"{source.id}: {len(src_items)} items ({status})")
                all_items += src_items

        enabled = [s for s in cfg.sources if s.enabled]
        if enabled and all(per_source[s.id]["status"] == "error" for s in enabled):
            raise RuntimeError("every source failed; check network access and `briefing sources --probe`")

        warnings = health_warnings(store, cfg.sources, cfg.settings.health.silent_runs_warning)
        for w in warnings:
            ctx.log(f"health: {w}", logging.WARNING)
    finally:
        store.close()

    all_items.sort(key=lambda i: i.published, reverse=True)
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "items": [i.model_dump(mode="json") for i in all_items],
        "per_source": per_source,
        "health_warnings": warnings,
    }
