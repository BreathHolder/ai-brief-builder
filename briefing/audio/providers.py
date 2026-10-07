"""Text-to-speech providers. Each returns 24 kHz 16-bit mono PCM for one chunk of text."""

from __future__ import annotations

import re
from typing import Protocol

from briefing.config import AppConfig

MAX_CHARS = 1500  # comfortably inside gpt-4o-mini-tts's 2,000-token input limit

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


class TTSProvider(Protocol):
    name: str
    model: str

    def synthesize(self, text: str, voice: str, instructions: str) -> bytes: ...


def split_text(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """Split long lines at sentence boundaries (then words) so each request fits the model limit."""
    if len(text) <= max_chars:
        return [text]
    chunks, current = [], ""
    for sentence in _SENTENCE_RE.split(text):
        while len(sentence) > max_chars:  # a single monster sentence: split on words
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if len(current) + len(sentence) + 1 > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return [c for c in chunks if c]


class OpenAITTS:
    name = "openai"

    def __init__(self, config: AppConfig):
        key = config.secrets.openai_api_key
        if not key:
            raise RuntimeError("OPENAI_API_KEY is required for audio (audio.provider: openai)")
        import openai

        self.client = openai.OpenAI(api_key=key, max_retries=4, timeout=180)
        self.model = config.settings.audio.model

    def synthesize(self, text: str, voice: str, instructions: str) -> bytes:
        resp = self.client.audio.speech.create(
            model=self.model, voice=voice, input=text, instructions=instructions, response_format="pcm",
        )
        data = resp.read() if hasattr(resp, "read") else resp.content
        if len(data) % 2:  # PCM16 must be whole samples
            data = data[:-1]
        return data


def make_provider(config: AppConfig) -> TTSProvider:
    """Factory, patched in tests."""
    provider = config.settings.audio.provider
    if provider == "openai":
        return OpenAITTS(config)
    raise ValueError(f"unknown audio provider: {provider}")
