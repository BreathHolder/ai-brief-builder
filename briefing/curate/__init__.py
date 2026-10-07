"""Stage 3: cluster items into stories, score importance, select the episode.

This stage never reads the environment profile (RunContext enforces it).
What makes the episode is decided by industry importance alone; the profile
only shapes how a story is discussed, in `synthesise`.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from briefing.context import RunContext
from briefing import schemas
from briefing.llm import LLMClient, Memo, complete_json, usage_cost
from briefing.models import Item
from briefing.prompts import render, split

RUBRIC = ("impact", "novelty", "breadth", "signal")
KINDS = {"launch", "release", "research", "policy", "security", "customer_story", "opinion", "other"}


def make_llm(ctx: RunContext) -> LLMClient:
    """Factory, patched in tests."""
    return LLMClient(ctx.config)


# -- clustering ---------------------------------------------------------------

def item_line(idx: int, item: Item, chars: int) -> str:
    preview = " ".join(item.text.split())[:chars]
    return f"[i{idx}] ({item.vendor}, {item.published.date().isoformat()}) {item.title} :: {preview}"


def cluster(ctx: RunContext, llm: LLMClient, items: list[Item], memo: Memo | None = None) -> list[dict[str, Any]]:
    chars = ctx.config.settings.curation.item_preview_chars
    system, user = split(render(__file__, "cluster", items="\n".join(item_line(i, it, chars) for i, it in enumerate(items))))
    raw = complete_json(llm, "curation", "cluster", system, user, max_tokens=8000, schema=schemas.CLUSTER, memo=memo)

    assigned: set[int] = set()
    stories: list[dict[str, Any]] = []
    for s in (raw.get("stories") if isinstance(raw, dict) else None) or []:
        idxs = []
        for ref in s.get("items") or []:
            try:
                n = int(str(ref).lstrip("i"))
            except ValueError:
                continue
            if 0 <= n < len(items) and n not in assigned:
                idxs.append(n)
                assigned.add(n)
        if idxs:
            stories.append({"headline": (s.get("headline") or items[idxs[0]].title).strip(), "idx": idxs})
    # Anything the model dropped becomes its own story; nothing is silently lost.
    for n in range(len(items)):
        if n not in assigned:
            stories.append({"headline": items[n].title, "idx": [n]})
    return stories


# -- scoring ------------------------------------------------------------------

def story_vendor(items: list[Item]) -> str:
    return Counter(i.vendor for i in items).most_common(1)[0][0]


def story_block(cid: str, headline: str, items: list[Item], chars: int) -> str:
    lines = [f"[{cid}] {headline}"]
    for it in items[:5]:
        preview = " ".join(it.text.split())[: chars * 2]
        extra = f", {it.extra['upvotes']} upvotes" if "upvotes" in it.extra else ""
        if it.tier == "curated_research":
            extra += ", preprint, NOT peer-reviewed"
        lines.append(f"  - {it.source_id}{extra}: {it.title} :: {preview}")
    if len(items) > 5:
        lines.append(f"  - ...and {len(items) - 5} more sources")
    return "\n".join(lines)


def _clamp(v: Any) -> float:
    try:
        return max(1.0, min(10.0, float(v)))
    except (TypeError, ValueError):
        return 1.0


def score(ctx: RunContext, llm: LLMClient, stories: list[dict[str, Any]], items: list[Item],
          memo: Memo | None = None) -> list[dict[str, Any]]:
    cur = ctx.config.settings.curation
    blocks = [
        story_block(f"c{n}", s["headline"], [items[i] for i in s["idx"]], cur.item_preview_chars)
        for n, s in enumerate(stories)
    ]
    system, user = split(render(__file__, "score", stories="\n\n".join(blocks)))
    raw = complete_json(llm, "curation", "score", system, user, max_tokens=12000, schema=schemas.SCORE, memo=memo)
    by_id = {}
    for row in (raw.get("scores") if isinstance(raw, dict) else None) or []:
        by_id[str(row.get("story", ""))] = row

    scored = []
    for n, s in enumerate(stories):
        row = by_id.get(f"c{n}", {})
        sub = {k: _clamp(row.get(k)) for k in RUBRIC}
        total = round(sum(sub[k] * cur.weights.get(k, 0) for k in RUBRIC), 2)
        story_items = [items[i] for i in s["idx"]]
        kind = row.get("kind") if row.get("kind") in KINDS else "other"
        if all(i.tier == "curated_research" for i in story_items):
            kind = "research"
        scored.append({
            "id": f"c{n}",
            "headline": s["headline"],
            "vendor": story_vendor(story_items),
            "kind": kind,
            "scores": sub,
            "total": total,
            "rationale": (row.get("rationale") or "").strip(),
            "unscored": not row,
            "items": [i.model_dump(mode="json") for i in story_items],
        })
    scored.sort(key=lambda s: s["total"], reverse=True)
    return scored


# -- selection (deterministic) ------------------------------------------------

def select(scored: list[dict[str, Any]], *, stories_min: int, stories_max: int, max_per_vendor: int,
           min_story_score: float, min_research_score: float | None) -> list[dict[str, Any]]:
    chosen: list[dict[str, Any]] = []
    per_vendor: Counter = Counter()
    for s in scored:  # already sorted by total, highest first
        if len(chosen) >= stories_max:
            break
        if per_vendor[s["vendor"]] >= max_per_vendor:
            continue
        if s["total"] < min_story_score and len(chosen) >= stories_min:
            continue
        chosen.append(s)
        per_vendor[s["vendor"]] += 1

    # Optionally reserve a slot for the best research story (off by default).
    if min_research_score is not None and not any(s["kind"] == "research" for s in chosen):
        best = next((s for s in scored if s["kind"] == "research" and s["total"] >= min_research_score), None)
        if best:
            if len(chosen) >= stories_max:
                chosen.pop(min(range(len(chosen)), key=lambda i: chosen[i]["total"]))
            chosen.append(best)
    chosen.sort(key=lambda s: s["total"], reverse=True)
    return chosen


# -- thread -------------------------------------------------------------------

def find_thread(llm: LLMClient, selected: list[dict[str, Any]], memo: Memo | None = None) -> dict[str, Any] | None:
    if len(selected) < 3:
        return None
    text = "\n".join(f"[{s['id']}] {s['headline']} — {s['rationale']}" for s in selected)
    system, user = split(render(__file__, "thread", stories=text))
    raw = complete_json(llm, "curation", "thread", system, user, max_tokens=1500, schema=schemas.THREAD, memo=memo)
    thread = raw.get("thread") if isinstance(raw, dict) else None
    if not thread or not isinstance(thread, str) or thread.strip().lower() in {"null", "none"}:
        return None
    ids = {s["id"] for s in selected}
    return {"summary": thread.strip(), "stories": [c for c in raw.get("stories") or [] if c in ids]}


# -- stage --------------------------------------------------------------------

def run(ctx: RunContext, inputs: dict[str, Any]) -> dict[str, Any]:
    cfg = ctx.config.settings
    items = [Item.model_validate(i) for i in inputs["normalise"]["items"]]
    ctx.log(f"{len(items)} items to curate")
    if not items:
        return {"selected": [], "also_this_week": [], "thread": None, "llm_usage": [], "cost_usd": 0.0}

    llm = make_llm(ctx)
    memo = Memo(ctx.run_dir / "curate.memo.json")
    stories = cluster(ctx, llm, items, memo)
    ctx.log(f"{len(stories)} stories from {len(items)} items")
    scored = score(ctx, llm, stories, items, memo)
    unscored = [s["id"] for s in scored if s["unscored"]]
    if unscored:
        ctx.log(f"scorer skipped {len(unscored)} stories; they rank at the bottom", logging.WARNING)

    selected = select(
        scored,
        stories_min=cfg.episode.stories_min,
        stories_max=cfg.episode.stories_max,
        max_per_vendor=cfg.episode.max_stories_per_vendor,
        min_story_score=cfg.curation.min_story_score,
        min_research_score=cfg.curation.min_research_score,
    )
    chosen = {s["id"] for s in selected}
    also = [s for s in scored if s["id"] not in chosen]
    thread = find_thread(llm, selected, memo)
    for s in selected:
        ctx.log(f"selected {s['total']:>4} [{s['vendor']}] {s['headline']}")

    cost, unpriced = usage_cost(llm.usage, cfg.llm.prices)
    return {
        "selected": selected,
        "also_this_week": [
            {"id": s["id"], "headline": s["headline"], "total": s["total"], "vendor": s["vendor"],
             "kind": s["kind"], "url": s["items"][0]["url"]}
            for s in also
        ],
        "thread": thread,
        "llm_usage": llm.usage,
        "cost_usd": cost,
        "unpriced_models": unpriced,
    }
