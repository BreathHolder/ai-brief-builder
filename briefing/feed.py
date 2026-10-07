"""Podcast RSS feed for AntennaPod (or any podcast app) on the home network.

Layout under data/public/:
  feed.xml                  the podcast feed
  episodes.json             index of published episodes (source of truth for the feed)
  episodes/<slug>.mp3       audio files
  cover.<ext>               optional artwork
"""

from __future__ import annotations

import html
import json
import os
import shutil
from datetime import datetime, time, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from briefing.config import AppConfig

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT = "http://purl.org/rss/1.0/modules/content/"
ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES)
ET.register_namespace("content", CONTENT)
ET.register_namespace("atom", ATOM)

REC_LABEL = {"pilot": "PILOT", "evaluate": "EVALUATE", "ignore": "IGNORE"}


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def load_index(public: Path) -> list[dict[str, Any]]:
    try:
        return json.loads((public / "episodes.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []


def episode_html(syn: dict[str, Any], cur: dict[str, Any]) -> str:
    """Show notes as simple HTML: AntennaPod renders this under the episode."""
    parts = []
    thread = cur.get("thread")
    if thread:
        parts.append(f"<p><em>{html.escape(thread['summary'])}</em></p>")
    for n, a in enumerate(syn.get("analyses") or [], 1):
        rec = REC_LABEL.get(a.get("recommendation", ""), a.get("recommendation", "").upper())
        parts.append(f"<h3>{n}. {html.escape(a['headline'])}</h3>")
        parts.append(f"<p><strong>{rec}</strong>: {html.escape(a.get('recommendation_reason', ''))}</p>")
        if a.get("so_what"):
            parts.append(f"<p>{html.escape(a['so_what'])}</p>")
        links = " · ".join(f'<a href="{html.escape(s["url"])}">{html.escape(s["source_id"])}</a>' for s in a["sources"])
        parts.append(f"<p>Sources: {links}</p>")
    also = (cur.get("also_this_week") or [])[:10]
    if also:
        parts.append("<h3>Also this week</h3><ul>")
        parts += [f'<li><a href="{html.escape(x["url"])}">{html.escape(x["headline"])}</a></li>' for x in also]
        parts.append("</ul>")
    return "\n".join(parts)


def episode_summary(syn: dict[str, Any]) -> str:
    heads = [a["headline"] for a in syn.get("analyses") or [] if a.get("voiced", True)]
    return "This week: " + "; ".join(heads) + "." if heads else "This week's AI platform briefing."


def publish_episode(config: AppConfig, episode_date, audio: dict[str, Any], syn: dict[str, Any],
                    cur: dict[str, Any]) -> dict[str, Any]:
    public = config.public_dir
    (public / "episodes").mkdir(parents=True, exist_ok=True)
    src = Path(audio["audio_path"])
    dest = public / "episodes" / src.name
    tmp = dest.with_suffix(".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)

    entry = {
        "guid": f"ai-briefing-{episode_date.isoformat()}",
        "date": episode_date.isoformat(),
        "title": syn["script"]["title"],
        "file": f"episodes/{dest.name}",
        "bytes": dest.stat().st_size,
        "duration_s": audio.get("duration_s") or 0,
        "summary": episode_summary(syn),
        "html": episode_html(syn, cur),
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    index = [e for e in load_index(public) if e["guid"] != entry["guid"]] + [entry]
    index.sort(key=lambda e: e["date"], reverse=True)

    keep = config.settings.feed.keep_episodes
    for old in index[keep:]:
        (public / old["file"]).unlink(missing_ok=True)
    index = index[:keep]

    _atomic_write(public / "episodes.json", json.dumps(index, indent=2))
    write_feed(config, index)
    return entry


def _cover(config: AppConfig) -> str | None:
    path = config.settings.feed.cover_image
    if not path:
        return None
    src = Path(path).expanduser()
    if not src.exists():
        return None
    dest = config.public_dir / f"cover{src.suffix.lower()}"
    if not dest.exists() or dest.stat().st_mtime < src.stat().st_mtime:
        shutil.copyfile(src, dest)
    return dest.name


def write_feed(config: AppConfig, index: list[dict[str, Any]]) -> Path:
    s = config.settings
    base = s.feed.base_url
    rss = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = s.episode.title
    ET.SubElement(ch, "link").text = f"{base}/"
    ET.SubElement(ch, "description").text = s.feed.description or s.episode.title
    ET.SubElement(ch, "language").text = "en-us"
    ET.SubElement(ch, f"{{{ATOM}}}link", {"href": f"{base}/feed.xml", "rel": "self", "type": "application/rss+xml"})
    ET.SubElement(ch, f"{{{ITUNES}}}author").text = s.feed.author
    ET.SubElement(ch, f"{{{ITUNES}}}explicit").text = "false"
    ET.SubElement(ch, f"{{{ITUNES}}}category", {"text": "Technology"})
    if cover := _cover(config):
        ET.SubElement(ch, f"{{{ITUNES}}}image", {"href": f"{base}/{cover}"})
        img = ET.SubElement(ch, "image")
        ET.SubElement(img, "url").text = f"{base}/{cover}"
        ET.SubElement(img, "title").text = s.episode.title
        ET.SubElement(img, "link").text = f"{base}/"
    if index:
        ET.SubElement(ch, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))

    for e in index:
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text = e["title"]
        ET.SubElement(it, "guid", {"isPermaLink": "false"}).text = e["guid"]
        d = datetime.fromisoformat(e["date"])
        pub = datetime.combine(d.date(), time(5, 0), tzinfo=timezone.utc)
        ET.SubElement(it, "pubDate").text = format_datetime(pub)
        ET.SubElement(it, "enclosure", {"url": f"{base}/{e['file']}", "length": str(e["bytes"]), "type": "audio/mpeg"})
        ET.SubElement(it, "description").text = e["summary"]
        ET.SubElement(it, f"{{{CONTENT}}}encoded").text = e["html"]
        secs = int(e["duration_s"])
        ET.SubElement(it, f"{{{ITUNES}}}duration").text = f"{secs // 3600:d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
        ET.SubElement(it, f"{{{ITUNES}}}summary").text = e["summary"]

    ET.indent(rss)
    path = config.public_dir / "feed.xml"
    config.public_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n")
    return path
