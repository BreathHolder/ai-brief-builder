"""Stage 6: write show notes and a readable transcript. (Private feed in Phase 4.)"""

from __future__ import annotations

from typing import Any

import logging

from briefing.context import RunContext
from briefing.feed import publish_episode

REC_LABEL = {"pilot": "PILOT", "evaluate": "EVALUATE", "ignore": "IGNORE"}


def show_notes(inputs: dict[str, Any]) -> str:
    syn = inputs["synthesise"]
    cur = inputs["curate"]
    script = syn["script"]
    out = [f"# {script['title']}", ""]

    thread = cur.get("thread")
    if thread:
        out += ["## This week's thread", "", thread["summary"], ""]

    out += ["## Stories", ""]
    if not syn.get("analyses"):
        out += ["(none this week)", ""]
    for n, a in enumerate(syn.get("analyses") or [], 1):
        out += [f"### {n}. {a['headline']}", ""]
        if a.get("voiced") is False:
            out += ["_Not in the audio this week: the script segment failed to generate._", ""]
        elif a.get("factcheck_error"):
            out += ["_Fact-check didn't run for this segment; treat its specifics with care._", ""]
        out += [f"**{REC_LABEL.get(a['recommendation'], a['recommendation'].upper())}**: "
                f"{a.get('recommendation_reason', '')}", ""]
        if a.get("so_what"):
            out += [a["so_what"], ""]
        out += ["Sources:"] + [f"- [{s['title']}]({s['url']}) ({s['source_id']})" for s in a["sources"]] + [""]

    also = cur.get("also_this_week") or []
    if also:
        out += ["## Also this week", ""]
        out += [f"- [{a['headline']}]({a['url']}) · score {a['total']}" for a in also[:15]]
        if len(also) > 15:
            out.append(f"- …and {len(also) - 15} more")
        out.append("")

    issues = syn.get("factcheck_issues") or []
    if issues:
        out += ["## Fact-check corrections", ""]
        out += [f"- {i['problem']}" for i in issues] + [""]

    fallback = sorted({f"{u['label'] or u['task']}" for stage in ("normalise", "curate", "synthesise")
                       for u in inputs.get(stage, {}).get("llm_usage") or [] if u.get("fallback")})
    if fallback:
        out += ["## Fallback model used", "",
                f"The primary model failed for {len(fallback)} call(s), so the fallback provider wrote them: "
                + ", ".join(fallback[:12]) + ("…" if len(fallback) > 12 else ""), ""]

    warnings = inputs.get("ingest", {}).get("health_warnings") or []
    if warnings:
        out += ["## Source health", ""] + [f"- {w}" for w in warnings] + [""]

    audio = inputs.get("audio") or {}
    for w in audio.get("warnings") or []:
        out += [f"> **Audio:** {w}", ""]

    llm_cost = round((cur.get("cost_usd") or 0) + (syn.get("cost_usd") or 0)
                     + (inputs.get("normalise", {}).get("cost_usd") or 0), 3)
    audio_cost = audio.get("cost_usd") or 0
    footer = f"Script: {syn.get('words', 0)} words"
    if audio.get("duration_s"):
        d = audio["duration_s"]
        footer += f" · audio {int(d // 60)}:{int(d % 60):02d} ({audio.get('provider')})"
    footer += (f" · cost ${llm_cost + audio_cost:.3f} (LLM ${llm_cost:.3f} + audio ${audio_cost:.3f})"
               f" · profile `{script['profile_sha256'][:12]}`")
    out += ["---", footer, ""]
    return "\n".join(out)


def transcript(inputs: dict[str, Any], hosts: dict[str, str]) -> str:
    script = inputs["synthesise"]["script"]
    out = [f"# {script['title']} — transcript", ""]
    last_segment = None
    for line in script["lines"]:
        if line["segment"] != last_segment and line["segment"].startswith("story:"):
            out.append("---\n")
        last_segment = line["segment"]
        out.append(f"**{hosts.get(line['speaker'], line['speaker'])}:** {line['text']}\n")
    return "\n".join(out)


def run(ctx: RunContext, inputs: dict[str, Any]) -> dict[str, Any]:
    hosts = ctx.config.settings.episode.hosts
    notes_path = ctx.run_dir / "show-notes.md"
    notes_path.write_text(show_notes(inputs), encoding="utf-8")
    script_path = ctx.run_dir / "transcript.md"
    script_path.write_text(transcript(inputs, {"lead": hosts.lead, "counterpoint": hosts.counterpoint}), encoding="utf-8")
    ctx.log(f"show notes: {notes_path}")
    ctx.log(f"transcript: {script_path}")

    audio = inputs.get("audio") or {}
    feed_updated, feed_url = False, None
    feed = ctx.config.settings.feed
    if feed.enabled and audio.get("audio_path"):
        entry = publish_episode(ctx.config, ctx.episode_date, audio, inputs["synthesise"], inputs["curate"])
        feed_updated, feed_url = True, f"{feed.base_url}/feed.xml"
        ctx.log(f"feed updated: {entry['title']} -> {feed_url}")
    elif feed.enabled:
        ctx.log("no audio this run; feed left unchanged", logging.WARNING)
    return {"show_notes": str(notes_path), "transcript": str(script_path),
            "audio": audio.get("audio_path"), "feed_updated": feed_updated, "feed_url": feed_url}
