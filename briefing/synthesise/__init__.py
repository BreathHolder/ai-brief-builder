"""Stage 4: turn the selected stories into a two-voice episode script.

The only stage allowed to read the environment profile, and it uses it only in
the analysis pass, to write the "so what" and the recommendation.

Passes:
  1. analyse   one call per story: facts, significance, for/against, so-what
  2. segments  intro, one segment per story, outro, each as dialogue lines
  3. factcheck each story segment checked against its sources; fixes applied
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from briefing.context import RunContext
from briefing import schemas
from briefing.llm import LLMClient, Memo, complete_json, usage_cost
from briefing.models import Script, ScriptLine
from briefing.prompts import render, split

RECOMMENDATIONS = {"evaluate", "pilot", "ignore"}
SPEAKERS = {"lead", "counterpoint"}


def make_llm(ctx: RunContext) -> LLMClient:
    """Factory, patched in tests."""
    return LLMClient(ctx.config)


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def spoken_date(d: date) -> str:
    """'Monday, October 12th, 2026' — the episode's first words."""
    return f"{d.strftime('%A')}, {d.strftime('%B')} {_ordinal(d.day)}, {d.year}"


def word_count(lines: list[ScriptLine]) -> int:
    return sum(len(l.text.split()) for l in lines)


# -- source packing -----------------------------------------------------------

def story_sources(story: dict[str, Any], budget: int) -> str:
    items = story["items"][:5]
    share = max(1500, budget // max(1, len(items)))
    blocks = []
    for n, it in enumerate(items, 1):
        body = " ".join((it.get("text") or it.get("summary") or "").split())[:share]
        blocks.append(
            f"[S{n}] {it['source_id']} | {it['title']} | {it['url']} | published {it['published'][:10]}\n{body}"
        )
    return "\n\n".join(blocks)


# -- pass 1: analysis ---------------------------------------------------------

def analyse(ctx: RunContext, llm: LLMClient, story: dict[str, Any], profile: str, memo: Memo | None = None) -> dict[str, Any]:
    sources = story_sources(story, ctx.config.settings.synthesis.story_source_chars)
    system, user = split(render(
        __file__, "analyse", audience=ctx.config.settings.episode.audience, profile=profile,
        headline=story["headline"],
        rationale=story.get("rationale") or "", sources=sources,
    ))
    raw = complete_json(llm, "synthesis", f"analyse:{story['id']}", system, user, max_tokens=4000,
                        schema=schemas.ANALYSIS, memo=memo)
    if not isinstance(raw, dict):
        raise ValueError(f"analysis for {story['id']} was not an object")
    rec = str(raw.get("recommendation", "")).strip().lower()
    raw["recommendation"] = rec if rec in RECOMMENDATIONS else "evaluate"
    raw["contested"] = bool(raw.get("contested"))
    valid_urls = {it["url"] for it in story["items"]}
    raw["key_facts"] = [
        f for f in raw.get("key_facts") or []
        if isinstance(f, dict) and f.get("fact") and f.get("source_url") in valid_urls
    ]
    raw["story_id"] = story["id"]
    raw["headline"] = story["headline"]
    raw["sources"] = [{"title": it["title"], "url": it["url"], "source_id": it["source_id"]} for it in story["items"]]
    return raw


# -- pass 2: dialogue ---------------------------------------------------------

def _lines(raw: Any, segment: str, valid_urls: set[str] | None = None) -> list[ScriptLine]:
    out = []
    for row in (raw.get("lines") if isinstance(raw, dict) else raw) or []:
        if not isinstance(row, dict):
            continue
        text = " ".join(str(row.get("text", "")).split())
        if not text:
            continue
        speaker = row.get("speaker") if row.get("speaker") in SPEAKERS else "lead"
        refs = [r for r in row.get("refs") or [] if not valid_urls or r in valid_urls]
        out.append(ScriptLine(speaker=speaker, text=text, source_refs=refs, segment=segment))
    if not out:
        raise ValueError(f"segment {segment} produced no lines")
    return out


def intro_segment(ctx, llm, selected, thread, memo=None) -> list[ScriptLine]:
    ep = ctx.config.settings.episode
    system, user = split(render(
        __file__, "segment_intro", lead=ep.hosts.lead, counterpoint=ep.hosts.counterpoint, title=ep.title,
        audience=ep.audience, words=ctx.config.settings.synthesis.intro_words,
        thread=(thread or {}).get("summary") or "No single theme this week; just preview the stories.",
        stories="\n".join(f"{n}. {s['headline']}" for n, s in enumerate(selected, 1)),
    ))
    return _lines(complete_json(llm, "synthesis", "segment:intro", system, user, max_tokens=3000,
                                schema=schemas.LINES, memo=memo), "intro")


def story_segment(ctx, llm, analysis, position, count, words, memo=None) -> list[ScriptLine]:
    ep = ctx.config.settings.episode
    public = {k: analysis[k] for k in (
        "what_happened", "why_it_matters", "case_for", "case_against", "so_what", "key_facts"
    ) if k in analysis}
    debate = (
        f"The debate: {ep.hosts.lead} makes the case for, {ep.hosts.counterpoint} makes the case against, "
        "and they genuinely push on each other."
        if analysis["contested"]
        else f"{ep.hosts.counterpoint} probes the weak spots and asks the skeptical question; "
             f"{ep.hosts.lead} answers it."
    )
    system, user = split(render(
        __file__, "segment_story", lead=ep.hosts.lead, counterpoint=ep.hosts.counterpoint, audience=ep.audience,
        position=position, count=count, words=words,
        transition_note=" from the previous story" if position > 1 else " from the intro",
        debate_instruction=debate, recommendation=analysis["recommendation"],
        recommendation_reason=analysis.get("recommendation_reason", ""),
        analysis=json.dumps(public, indent=1),
    ))
    valid = {s["url"] for s in analysis["sources"]}
    raw = complete_json(llm, "synthesis", f"segment:{analysis['story_id']}", system, user, max_tokens=4000,
                        schema=schemas.LINES, memo=memo)
    return _lines(raw, f"story:{analysis['story_id']}", valid)


def outro_segment(ctx, llm, analyses, also, memo=None) -> list[ScriptLine]:
    ep = ctx.config.settings.episode
    recs = "\n".join(
        f"- {a['headline']}: {a['recommendation']} — {a.get('recommendation_reason', '')}" for a in analyses
    )
    also_txt = "\n".join(f"- {a['headline']}" for a in also[:5]) or "- (none; skip this part)"
    system, user = split(render(
        __file__, "segment_outro", lead=ep.hosts.lead, counterpoint=ep.hosts.counterpoint, title=ep.title,
        words=ctx.config.settings.synthesis.outro_words, recommendations=recs, also=also_txt,
    ))
    return _lines(complete_json(llm, "synthesis", "segment:outro", system, user, max_tokens=3000,
                                schema=schemas.LINES, memo=memo), "outro")


# -- pass 3: fact-check -------------------------------------------------------

def factcheck(ctx, llm, story, lines: list[ScriptLine], memo=None) -> tuple[list[ScriptLine], list[dict]]:
    sources = story_sources(story, ctx.config.settings.synthesis.story_source_chars)
    numbered = "\n".join(f"{n}. [{l.speaker}] {l.text}" for n, l in enumerate(lines))
    system, user = split(render(__file__, "factcheck", sources=sources, lines=numbered))
    raw = complete_json(llm, "factcheck", f"factcheck:{story['id']}", system, user, max_tokens=3000,
                        schema=schemas.FACTCHECK, memo=memo)
    issues = []
    fixed = list(lines)
    for row in (raw.get("issues") if isinstance(raw, dict) else None) or []:
        try:
            idx = int(row.get("line"))
        except (TypeError, ValueError):
            continue
        fix = " ".join(str(row.get("fix", "")).split())
        if 0 <= idx < len(fixed) and fix:
            issues.append({"story": story["id"], "original": fixed[idx].text, "problem": row.get("problem", ""), "fix": fix})
            fixed[idx] = fixed[idx].model_copy(update={"text": fix})
    return fixed, issues


# -- stage --------------------------------------------------------------------

def run(ctx: RunContext, inputs: dict[str, Any]) -> dict[str, Any]:
    cfg = ctx.config.settings
    ep = cfg.episode
    curate = inputs["curate"]
    selected: list[dict[str, Any]] = curate["selected"]
    profile, profile_sha = ctx.read_profile()

    opening = []
    if ep.open_with_date:
        opening.append(ScriptLine(speaker="lead", text=f"It's {spoken_date(ctx.episode_date)}.", segment="open"))

    if not selected:
        ctx.log("no stories selected; writing a placeholder episode", logging.WARNING)
        lines = opening + [ScriptLine(speaker="lead", text=f"This is the {ep.title}. No stories this week.", segment="intro")]
        script = Script(episode_date=ctx.episode_date.isoformat(), title=f"{ctx.episode_date.isoformat()} · {ep.title}",
                        lines=lines, profile_sha256=profile_sha)
        return {"script": script.model_dump(), "analyses": [], "factcheck_issues": [], "words": word_count(lines),
                "llm_usage": [], "cost_usd": 0.0}

    llm = make_llm(ctx)
    memo = Memo(ctx.run_dir / "synthesise.memo.json")

    analyses = []
    for story in selected:
        analyses.append(analyse(ctx, llm, story, profile, memo))
        ctx.log(f"analysed {story['id']}: {analyses[-1]['recommendation']} — {story['headline']}")

    syn = cfg.synthesis
    story_words = max(250, (ep.target_words - syn.intro_words - syn.outro_words) // len(selected))
    body: list[ScriptLine] = intro_segment(ctx, llm, selected, curate.get("thread"), memo)
    issues: list[dict] = []
    for n, (story, analysis) in enumerate(zip(selected, analyses), 1):
        try:
            seg = story_segment(ctx, llm, analysis, n, len(selected), story_words, memo)
        except Exception as exc:
            # One bad segment shouldn't sink an unattended weekly run: drop it
            # from the audio, keep it in the show notes, and say so.
            analysis["voiced"] = False
            analysis["voice_error"] = str(exc)[:300]
            ctx.log(f"segment for {story['id']} failed, leaving it out of the script: {exc}", logging.WARNING)
            continue
        analysis["voiced"] = True
        if syn.factcheck:
            try:
                seg, found = factcheck(ctx, llm, story, seg, memo)
                issues += found
                if found:
                    ctx.log(f"fact-check fixed {len(found)} line(s) in {story['id']}")
            except Exception as exc:
                analysis["factcheck_error"] = str(exc)[:300]
                ctx.log(f"fact-check for {story['id']} failed; segment kept unchecked: {exc}", logging.WARNING)
        body += seg
    if not any(a.get("voiced") for a in analyses):
        raise RuntimeError("every story segment failed; see warnings above")
    body += outro_segment(ctx, llm, analyses, curate.get("also_this_week") or [], memo)

    lines = opening + body
    words = word_count(lines)
    ctx.log(f"script: {len(lines)} lines, {words} words (target {ep.target_words}), "
            f"~{round(words / 150)} minutes")
    if words < ep.target_words * 0.7:
        ctx.log(f"script is short of target ({words} < 70% of {ep.target_words})", logging.WARNING)

    script = Script(
        episode_date=ctx.episode_date.isoformat(),
        title=f"{ctx.episode_date.isoformat()} · {ep.title}",
        lines=lines,
        profile_sha256=profile_sha,
    )
    cost, unpriced = usage_cost(llm.usage, cfg.llm.prices)
    return {
        "script": script.model_dump(),
        "analyses": analyses,
        "factcheck_issues": issues,
        "words": words,
        "llm_usage": llm.usage,
        "cost_usd": cost,
        "unpriced_models": unpriced,
    }
