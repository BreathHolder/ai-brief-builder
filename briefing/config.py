"""Configuration loading and validation.

Settings come from config/config.yaml and config/sources.yaml; secrets come
from the environment (optionally via a .env file). Everything is validated
up front so a bad config fails before any API spend.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator


class HostsConfig(BaseModel):
    lead: str = "Alex"
    counterpoint: str = "Jordan"


class EpisodeConfig(BaseModel):
    title: str
    hosts: HostsConfig = HostsConfig()
    target_minutes: int = Field(gt=0)
    target_words: int = Field(gt=0)
    stories_min: int = Field(gt=0)
    stories_max: int = Field(gt=0)
    max_stories_per_vendor: int = Field(gt=0)
    open_with_date: bool = True
    audience: str = "a technology leader responsible for an organization's AI platforms"

    @model_validator(mode="after")
    def _story_bounds(self) -> "EpisodeConfig":
        if self.stories_min > self.stories_max:
            raise ValueError("stories_min must be <= stories_max")
        return self


class IngestConfig(BaseModel):
    window_days: int = Field(gt=0)
    request_timeout_s: int = Field(gt=0)
    request_delay_s: float = Field(default=1.0, ge=0)
    max_retries: int = Field(default=3, ge=0)
    max_text_chars: int = Field(default=40000, gt=0)
    user_agent: str


class NormaliseConfig(BaseModel):
    translate_non_english: bool = True
    translation_max_chars: int = Field(default=12000, gt=0)
    title_similarity: float = Field(default=0.88, gt=0, le=1)
    text_similarity: float = Field(default=0.80, gt=0, le=1)


class HealthConfig(BaseModel):
    silent_runs_warning: int = Field(default=2, gt=0)


class CurationConfig(BaseModel):
    weights: dict[str, float]
    min_story_score: float = 5.0
    min_research_score: float | None = None  # None = research competes on merit, no reserved slot
    item_preview_chars: int = Field(default=300, gt=0)

    @field_validator("weights")
    @classmethod
    def _weights_sum_to_one(cls, v: dict[str, float]) -> dict[str, float]:
        total = sum(v.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"curation weights must sum to 1.0 (got {total:.3f})")
        unknown = set(v) - {"impact", "novelty", "breadth", "signal"}
        if unknown:
            raise ValueError(f"unknown curation weights: {sorted(unknown)}")
        return v


class SynthesisConfig(BaseModel):
    story_source_chars: int = Field(default=14000, gt=0)
    intro_words: int = Field(default=300, gt=0)
    outro_words: int = Field(default=450, gt=0)
    factcheck: bool = True


Provider = Literal["anthropic", "openai"]


class ModelRoute(BaseModel):
    provider: Provider
    model: str


class LLMConfig(BaseModel):
    tasks: dict[str, ModelRoute]
    fallback: ModelRoute | None = None
    prices: dict[str, tuple[float, float]] = Field(default_factory=dict)

    @field_validator("tasks")
    @classmethod
    def _required_tasks(cls, v: dict[str, ModelRoute]) -> dict[str, ModelRoute]:
        missing = {"translation", "curation", "synthesis", "factcheck"} - set(v)
        if missing:
            raise ValueError(f"llm.tasks missing: {sorted(missing)}")
        return v


class RolesConfig(BaseModel):
    lead: str
    counterpoint: str


class PauseConfig(BaseModel):
    same_speaker: int = Field(default=250, ge=0)
    speaker_change: int = Field(default=450, ge=0)
    segment: int = Field(default=1100, ge=0)


class AudioConfig(BaseModel):
    provider: Literal["openai"]
    model: str = "gpt-4o-mini-tts"
    sunset: date | None = None
    voices: RolesConfig
    style: RolesConfig
    pause_ms: PauseConfig = PauseConfig()
    loudness_lufs: float = -16
    bitrate: str = "128k"
    concurrency: int = Field(default=4, ge=1, le=16)
    price_per_minute: float = Field(default=0.02, ge=0)
    intro_sting: str | None = None
    outro_sting: str | None = None


class BudgetConfig(BaseModel):
    max_usd_per_episode: float = Field(gt=0)


class FeedConfig(BaseModel):
    enabled: bool = True
    base_url: str = "http://localhost:8080"
    host: str = "0.0.0.0"
    port: int = Field(default=8080, gt=0, lt=65536)
    keep_episodes: int = Field(default=12, gt=0)
    author: str = "AI Briefing"
    description: str = ""
    cover_image: str | None = None

    @field_validator("base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("feed.base_url must start with http:// or https://")
        return v.rstrip("/")


class ScheduleConfig(BaseModel):
    on_calendar: str = "Mon *-*-* 05:00:00"


class Settings(BaseModel):
    episode: EpisodeConfig
    ingest: IngestConfig
    normalise: NormaliseConfig = NormaliseConfig()
    health: HealthConfig = HealthConfig()
    curation: CurationConfig
    synthesis: SynthesisConfig = SynthesisConfig()
    llm: LLMConfig
    audio: AudioConfig
    budget: BudgetConfig
    feed: FeedConfig = FeedConfig()
    schedule: ScheduleConfig = ScheduleConfig()


class Feed(BaseModel):
    kind: Literal["rss", "hf_daily_papers", "scrape"]
    url: str
    include_keywords: list[str] = Field(default_factory=list)
    max_items: int = Field(default=30, gt=0)
    title_prefix: str | None = None  # e.g. "Kong Gateway" for bare release titles like "3.14.0"
    title_strip: list[str] = Field(default_factory=list)  # regexes removed from titles (site suffixes)
    fetch_articles: bool = True  # False = use the feed's own text (site blocks automated page fetches)
    # scrape
    link_pattern: str | None = None
    # hf_daily_papers
    min_upvotes: int = Field(default=10, ge=0)
    top_per_day: int = Field(default=5, gt=0)

    @model_validator(mode="after")
    def _kind_fields(self) -> "Feed":
        for pat in self.title_strip:
            try:
                re.compile(pat)
            except re.error as exc:
                raise ValueError(f"bad title_strip pattern {pat!r}: {exc}") from exc
        if self.kind == "scrape":
            if not self.link_pattern:
                raise ValueError(f"scrape feed {self.url} needs link_pattern")
            try:
                re.compile(self.link_pattern)
            except re.error as exc:
                raise ValueError(f"bad link_pattern for {self.url}: {exc}") from exc
        return self


class Source(BaseModel):
    id: str
    name: str
    vendor: str
    tier: Literal["first_party", "stack", "trusted_analysis", "curated_research"]
    feeds: list[Feed] = Field(min_length=1)
    enabled: bool = True
    notes: str | None = None


class SourcesFile(BaseModel):
    sources: list[Source]

    @model_validator(mode="after")
    def _unique_ids(self) -> "SourcesFile":
        ids = [s.id for s in self.sources]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate source ids: {sorted(dupes)}")
        return self


class Secrets(BaseModel):
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    elevenlabs_api_key: str | None = None

    def key_for(self, provider: str) -> str | None:
        return {"anthropic": self.anthropic_api_key, "openai": self.openai_api_key}.get(provider)

    def missing(self) -> list[str]:
        out = []
        if not self.anthropic_api_key:
            out.append("ANTHROPIC_API_KEY")
        if not self.openai_api_key:
            out.append("OPENAI_API_KEY")
        if not self.elevenlabs_api_key:
            out.append("ELEVENLABS_API_KEY")
        return out


class AppConfig(BaseModel):
    root: Path
    data_dir: Path
    settings: Settings
    sources: list[Source]
    secrets: Secrets
    profile_path: Path

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def public_dir(self) -> Path:
        return self.data_dir / "public"

    def read_profile(self) -> str:
        """Environment profile, read fresh each call. Used ONLY by synthesis."""
        return self.profile_path.read_text(encoding="utf-8")


class ConfigError(Exception):
    pass


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return data


def load_config(root: Path | None = None) -> AppConfig:
    root = (root or Path(os.environ.get("BRIEFING_ROOT", Path.cwd()))).resolve()
    load_dotenv(root / ".env", override=False)

    config_dir = root / "config"
    try:
        settings = Settings.model_validate(_load_yaml(config_dir / "config.yaml"))
        sources = SourcesFile.model_validate(_load_yaml(config_dir / "sources.yaml"))
    except ConfigError:
        raise
    except Exception as exc:  # pydantic ValidationError, yaml errors
        raise ConfigError(str(exc)) from exc

    if base_url := os.environ.get("BRIEFING_FEED_BASE_URL"):
        try:
            settings.feed = FeedConfig(**{**settings.feed.model_dump(), "base_url": base_url})
        except Exception as exc:
            raise ConfigError(f"BRIEFING_FEED_BASE_URL: {exc}") from exc
    if audience := os.environ.get("BRIEFING_AUDIENCE", "").strip():
        settings.episode.audience = audience

    profile_path = config_dir / "environment.md"
    if not profile_path.exists():
        raise ConfigError(
            f"missing environment profile: {profile_path} "
            "(copy config/environment.example.md to config/environment.md and edit it)"
        )

    data_dir = Path(os.environ.get("BRIEFING_DATA_DIR", root / "data")).resolve()

    return AppConfig(
        root=root,
        data_dir=data_dir,
        settings=settings,
        sources=sources.sources,
        secrets=Secrets(
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
            elevenlabs_api_key=os.environ.get("ELEVENLABS_API_KEY") or None,
        ),
        profile_path=profile_path,
    )
