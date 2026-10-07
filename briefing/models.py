"""Shared data records passed between stages."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Item(BaseModel):
    """One normalised article, paper or release note."""

    source_id: str
    vendor: str
    tier: str
    feed_url: str
    title: str
    url: str
    url_canon: str
    published: datetime
    author: str | None = None
    language: str = "en"
    original_language: str | None = None  # set when translated
    original_title: str | None = None
    text: str = ""
    summary: str | None = None
    also_reported_by: list[str] = Field(default_factory=list)  # merged duplicate URLs
    extra: dict[str, Any] = Field(default_factory=dict)  # e.g. upvotes for papers


class StoryCluster(BaseModel):
    """Several items about the same story."""

    id: str
    headline: str
    items: list[Item]
    scores: dict[str, float] = Field(default_factory=dict)
    total_score: float = 0.0


class ScriptLine(BaseModel):
    speaker: str  # "lead" | "counterpoint"
    text: str
    source_refs: list[str] = Field(default_factory=list)  # item URLs
    segment: str = ""  # "open", "intro", "story:c3", "outro"


class Script(BaseModel):
    episode_date: str
    title: str
    lines: list[ScriptLine]
    profile_sha256: str
