"""Stage 2: detect language, translate to English, and remove duplicates.

Dedupe merges the SAME article seen twice (syndication, tracking-param URLs,
mirrored posts). Grouping different articles about the same story is
curation's job, not this stage's.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any

from langdetect import DetectorFactory, LangDetectException, detect_langs

from briefing.context import RunContext
from briefing.llm import LLMClient, usage_cost
from briefing.models import Item
from briefing.store import Store

DetectorFactory.seed = 0  # deterministic detection

TIER_PRIORITY = {"first_party": 0, "stack": 1, "trusted_analysis": 2, "curated_research": 3}
_WORD_RE = re.compile(r"[a-z0-9]+")

TRANSLATE_SYSTEM = (
    "You translate technical news articles into clear, natural English. "
    "Preserve product names, version numbers, code, and technical terms exactly. "
    "Do not summarise, add commentary, or omit content. "
    "Respond with exactly <title>...</title><body>...</body> and nothing else."
)


def make_llm(ctx: RunContext) -> LLMClient:
    """Factory, patched in tests."""
    return LLMClient(ctx.config)


def open_store(ctx: RunContext) -> Store:
    return Store(ctx.config.cache_dir / "briefing.sqlite")


# -- language ---------------------------------------------------------------

def detect_language(item: Item) -> str:
    sample = f"{item.title}\n{item.text[:1500]}".strip()
    if len(sample) < 40:
        return "en"
    try:
        best = detect_langs(sample)[0]
    except LangDetectException:
        return "en"
    return best.lang if best.prob >= 0.90 else "en"


def parse_translation(raw: str) -> tuple[str | None, str]:
    title = re.search(r"<title>(.*?)</title>", raw, re.S)
    body = re.search(r"<body>(.*?)(?:</body>|$)", raw, re.S)
    return (title.group(1).strip() if title else None, (body.group(1) if body else raw).strip())


def translate_item(item: Item, lang: str, llm: LLMClient, store: Store, max_chars: int) -> Item:
    cached = store.get_translation(item.url_canon)
    if cached:
        title_en, text_en, model = cached["title_en"], cached["text_en"], cached["model"]
    else:
        prompt = (
            f"Source language: {lang}\n\n<title>{item.title}</title>\n<body>{item.text[:max_chars]}</body>"
        )
        result = llm.complete("translation", TRANSLATE_SYSTEM, prompt, max_tokens=8000)
        title_en, text_en = parse_translation(result.text)
        title_en = title_en or item.title
        model = f"{result.provider}/{result.model}"
        store.save_translation(item.url_canon, lang, title_en, text_en, model)
    return item.model_copy(update={
        "original_title": item.title, "title": title_en, "text": text_en,
        "original_language": lang, "language": "en",
    })


# -- dedupe -----------------------------------------------------------------

def _norm_title(t: str) -> str:
    return " ".join(_WORD_RE.findall(t.lower()))


def _shingles(text: str, k: int = 5) -> set[tuple[str, ...]]:
    words = _WORD_RE.findall(text.lower())
    return {tuple(words[i : i + k]) for i in range(max(0, len(words) - k + 1))}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def dedupe(items: list[Item], title_threshold: float, text_threshold: float) -> tuple[list[Item], int]:
    """Keep one item per duplicate group: first-party over others, then the earliest
    published (the original, not the syndicated copy), then the longest text."""
    ranked = sorted(items, key=lambda i: (TIER_PRIORITY.get(i.tier, 9), i.published, -len(i.text)))
    kept: list[Item] = []
    kept_meta: list[tuple[str, set]] = []
    removed = 0
    for item in ranked:
        nt = _norm_title(item.title)
        sh = _shingles(item.text) if len(item.text) >= 500 else set()
        match = None
        for idx, k in enumerate(kept):
            if k.url_canon == item.url_canon:
                match = idx
                break
            k_title, k_sh = kept_meta[idx]
            if SequenceMatcher(None, nt, k_title).ratio() >= title_threshold:
                match = idx
                break
            if sh and k_sh and _jaccard(sh, k_sh) >= text_threshold:
                match = idx
                break
        if match is None:
            kept.append(item)
            kept_meta.append((nt, sh))
            continue
        removed += 1
        primary = kept[match]
        extra_urls = [u for u in [item.url, *item.also_reported_by] if u != primary.url and u not in primary.also_reported_by]
        kept[match] = primary.model_copy(update={"also_reported_by": primary.also_reported_by + extra_urls})
    kept.sort(key=lambda i: i.published, reverse=True)
    return kept, removed


# -- stage ------------------------------------------------------------------

def run(ctx: RunContext, inputs: dict[str, Any]) -> dict[str, Any]:
    settings = ctx.config.settings.normalise
    items = [Item.model_validate(i) for i in inputs["ingest"]["items"]]
    ctx.log(f"{len(items)} items in")

    translated, failures = 0, []
    llm: LLMClient | None = None
    store = open_store(ctx)
    try:
        out: list[Item] = []
        for item in items:
            lang = detect_language(item)
            if lang == "en" or not settings.translate_non_english:
                out.append(item.model_copy(update={"language": lang}) if lang != "en" else item)
                continue
            try:
                llm = llm or make_llm(ctx)
                out.append(translate_item(item, lang, llm, store, settings.translation_max_chars))
                translated += 1
                ctx.log(f"translated {lang} -> en: {item.url}")
            except Exception as exc:
                failures.append({"url": item.url, "language": lang, "error": str(exc)})
                ctx.log(f"translation failed for {item.url}: {exc}", logging.WARNING)
                out.append(item.model_copy(update={"language": lang}))
    finally:
        store.close()

    deduped, removed = dedupe(out, settings.title_similarity, settings.text_similarity)
    ctx.log(f"{len(deduped)} items out ({removed} duplicates merged, {translated} translated)")
    return {
        "items": [i.model_dump(mode="json") for i in deduped],
        "duplicates_removed": removed,
        "translated": translated,
        "translation_failures": failures,
        "llm_usage": llm.usage if llm else [],
        "cost_usd": usage_cost(llm.usage, ctx.config.settings.llm.prices)[0] if llm else 0.0,
    }
