"""Raw PCM handling and the final ffmpeg encode.

Everything is assembled as 24 kHz, 16-bit, mono little-endian PCM (what the
TTS API returns), so stitching is plain byte concatenation and durations are
exact. ffmpeg is used once, at the end, for loudness normalisation, MP3
encoding, ID3 tags and chapter markers. (No pydub: it doesn't run on Python 3.13+.)
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2  # bytes
CHANNELS = 1
BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS


class FFmpegError(RuntimeError):
    pass


def silence(ms: int) -> bytes:
    n = int(SAMPLE_RATE * ms / 1000) * SAMPLE_WIDTH * CHANNELS
    return b"\x00" * n


def duration_s(pcm: bytes) -> float:
    return len(pcm) / BYTES_PER_SECOND


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def require_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise FFmpegError("ffmpeg not found; install it with: sudo apt install ffmpeg")
    return exe


def decode_to_pcm(path: Path) -> bytes:
    """Any audio file (e.g. an intro sting) to our PCM format."""
    exe = require_ffmpeg()
    proc = subprocess.run(
        [exe, "-v", "error", "-i", str(path), "-f", "s16le", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"could not decode {path}: {proc.stderr.decode(errors='replace')[-500:]}")
    return proc.stdout


@dataclass
class Chapter:
    title: str
    start_ms: int
    end_ms: int


def _meta_escape(value: str) -> str:
    for ch in ("\\", "=", ";", "#", "\n"):
        value = value.replace(ch, "\\" + ch)
    return value


def ffmetadata(tags: dict[str, str], chapters: list[Chapter]) -> str:
    out = [";FFMETADATA1"] + [f"{k}={_meta_escape(v)}" for k, v in tags.items()]
    for ch in chapters:
        out += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={ch.start_ms}", f"END={ch.end_ms}",
                f"title={_meta_escape(ch.title)}"]
    return "\n".join(out) + "\n"


def encode_mp3(pcm: bytes, out_path: Path, *, tags: dict[str, str], chapters: list[Chapter],
               loudness_lufs: float, bitrate: str, workdir: Path) -> None:
    exe = require_ffmpeg()
    workdir.mkdir(parents=True, exist_ok=True)
    raw = workdir / "episode.pcm"
    meta = workdir / "episode.ffmeta"
    raw.write_bytes(pcm)
    meta.write_text(ffmetadata(tags, chapters), encoding="utf-8")
    tmp = out_path.with_suffix(".tmp.mp3")
    cmd = [
        exe, "-v", "error", "-y",
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-i", str(raw),
        "-i", str(meta), "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1",
        "-af", f"loudnorm=I={loudness_lufs}:TP=-1.5:LRA=11",
        "-ar", "44100", "-c:a", "libmp3lame", "-b:a", bitrate, "-id3v2_version", "3",
        str(tmp),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg encode failed: {proc.stderr.decode(errors='replace')[-800:]}")
    tmp.replace(out_path)
    raw.unlink(missing_ok=True)
