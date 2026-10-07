"""Stage 5: render the script to a two-voice MP3 with chapters.

- Each line is spoken by its role's voice with that role's delivery style.
- Rendered chunks are cached by content, so re-running after a script tweak
  only pays for the lines that changed (and a failed run resumes for free).
- Before any audio is rendered, projected spend (LLM so far + audio) is
  checked against budget.max_usd_per_episode.
"""

from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from briefing.audio import pcm
from briefing.audio.providers import make_provider, split_text
from briefing.context import RunContext

WORDS_PER_MINUTE = 150


class BudgetExceeded(RuntimeError):
    pass


def chunk_key(provider: str, model: str, voice: str, instructions: str, text: str) -> str:
    return hashlib.sha256("\x1f".join((provider, model, voice, instructions, text)).encode()).hexdigest()


def llm_spend(inputs: dict[str, Any]) -> float:
    return round(sum((inputs.get(s) or {}).get("cost_usd") or 0 for s in ("normalise", "curate", "synthesise")), 4)


def chapter_title(segment: str, headlines: dict[str, str]) -> str:
    if segment in ("open", "intro"):
        return "Intro"
    if segment == "outro":
        return "Wrap-up"
    if segment.startswith("story:"):
        return headlines.get(segment.split(":", 1)[1], "Story")
    return segment


def sunset_warning(model: str, sunset: date | None, today: date) -> str | None:
    if not sunset:
        return None
    days = (sunset - today).days
    if days < 0:
        return f"Audio model {model} passed its shutdown date ({sunset}); switch audio.model or audio.provider."
    if days <= 30:
        return f"Audio model {model} shuts down on {sunset} ({days} days); plan the switch now."
    return None


def file_slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def run(ctx: RunContext, inputs: dict[str, Any]) -> dict[str, Any]:
    cfg = ctx.config.settings
    audio = cfg.audio
    syn = inputs["synthesise"]
    lines = syn["script"]["lines"]
    headlines = {a["story_id"]: a["headline"] for a in syn.get("analyses") or []}
    warnings: list[str] = []
    if w := sunset_warning(audio.model, audio.sunset, date.today()):
        warnings.append(w)
        ctx.log(w, logging.WARNING)

    cache_dir = ctx.config.cache_dir / "audio"
    cache_dir.mkdir(parents=True, exist_ok=True)
    voices = {"lead": audio.voices.lead, "counterpoint": audio.voices.counterpoint}
    styles = {"lead": audio.style.lead, "counterpoint": audio.style.counterpoint}

    # Plan every chunk and see what's already cached.
    plan: list[list[tuple[str, str, str]]] = []  # per line: [(key, text, role)]
    todo: dict[str, tuple[str, str]] = {}
    for line in lines:
        role = line["speaker"] if line["speaker"] in voices else "lead"
        chunks = []
        for text in split_text(line["text"]):
            key = chunk_key(audio.provider, audio.model, voices[role], styles[role], text)
            chunks.append((key, text, role))
            if not (cache_dir / f"{key}.pcm").exists():
                todo[key] = (text, role)
        plan.append(chunks)

    # Budget check before spending anything on audio.
    todo_words = sum(len(t.split()) for t, _ in todo.values())
    audio_estimate = round(todo_words / WORDS_PER_MINUTE * audio.price_per_minute, 4)
    llm_cost = llm_spend(inputs)
    projected = round(llm_cost + audio_estimate, 3)
    ctx.log(f"{len(todo)} chunk(s) to render, {sum(len(c) for c in plan) - len(todo)} cached; "
            f"projected spend ${projected:.3f} (LLM ${llm_cost:.3f} + audio ~${audio_estimate:.3f}) "
            f"of ${cfg.budget.max_usd_per_episode:.2f}")
    if projected > cfg.budget.max_usd_per_episode:
        raise BudgetExceeded(
            f"projected spend ${projected:.2f} exceeds budget.max_usd_per_episode "
            f"${cfg.budget.max_usd_per_episode:.2f}; no audio rendered. Raise the budget or shorten the episode."
        )

    # Render what's missing, in parallel.
    rendered_seconds = 0.0
    if todo:
        provider = make_provider(ctx.config)

        def render(item: tuple[str, tuple[str, str]]) -> float:
            key, (text, role) = item
            data = provider.synthesize(text, voices[role], styles[role])
            if not data:
                raise RuntimeError(f"empty audio for: {text[:60]}...")
            tmp = cache_dir / f"{key}.tmp"
            tmp.write_bytes(data)
            tmp.replace(cache_dir / f"{key}.pcm")
            return pcm.duration_s(data)

        with ThreadPoolExecutor(max_workers=audio.concurrency) as pool:
            for n, secs in enumerate(pool.map(render, todo.items()), 1):
                rendered_seconds += secs
                if n % 20 == 0 or n == len(todo):
                    ctx.log(f"rendered {n}/{len(todo)} chunks")

    # Assemble with pauses and chapter marks.
    parts: list[bytes] = []
    position = 0  # bytes
    chapters: list[pcm.Chapter] = []

    def add(chunk: bytes) -> None:
        nonlocal position
        parts.append(chunk)
        position += len(chunk)

    def ms() -> int:
        return int(position / pcm.BYTES_PER_SECOND * 1000)

    if audio.intro_sting:
        add(pcm.decode_to_pcm(Path(audio.intro_sting).expanduser()))
        add(pcm.silence(audio.pause_ms.segment))

    prev = None
    for line, chunks in zip(lines, plan):
        title = chapter_title(line["segment"], headlines)
        if prev is not None:
            if title != chapter_title(prev["segment"], headlines):
                add(pcm.silence(audio.pause_ms.segment))
            elif line["speaker"] != prev["speaker"]:
                add(pcm.silence(audio.pause_ms.speaker_change))
            else:
                add(pcm.silence(audio.pause_ms.same_speaker))
        if not chapters or chapters[-1].title != title:
            if chapters:
                chapters[-1].end_ms = ms()
            chapters.append(pcm.Chapter(title=title, start_ms=ms(), end_ms=0))
        for key, _, _ in chunks:
            add((cache_dir / f"{key}.pcm").read_bytes())
        prev = line

    if audio.outro_sting:
        add(pcm.silence(audio.pause_ms.segment))
        add(pcm.decode_to_pcm(Path(audio.outro_sting).expanduser()))
    if chapters:
        chapters[-1].end_ms = ms()

    episode = b"".join(parts)
    title = syn["script"]["title"]
    out_path = ctx.run_dir / f"{file_slug(title)}.mp3"
    pcm.encode_mp3(
        episode, out_path,
        tags={"title": title, "artist": cfg.episode.title, "album": cfg.episode.title,
              "date": ctx.episode_date.isoformat(), "genre": "Podcast"},
        chapters=chapters, loudness_lufs=audio.loudness_lufs, bitrate=audio.bitrate,
        workdir=ctx.run_dir / "audio-work",
    )
    duration = pcm.duration_s(episode)
    cost = round(rendered_seconds / 60 * audio.price_per_minute, 4)
    ctx.log(f"episode: {out_path.name}, {int(duration // 60)}:{int(duration % 60):02d}, "
            f"{len(chapters)} chapters, audio cost ~${cost:.3f}")
    return {
        "audio_path": str(out_path),
        "duration_s": round(duration, 1),
        "chapters": [{"title": c.title, "start_s": round(c.start_ms / 1000, 1)} for c in chapters],
        "chunks_rendered": len(todo),
        "chunks_cached": sum(len(c) for c in plan) - len(todo),
        "characters": sum(len(l["text"]) for l in lines),
        "cost_usd": cost,
        "provider": f"{audio.provider}/{audio.model}",
        "voices": voices,
        "warnings": warnings,
        "status": "rendered",
    }
