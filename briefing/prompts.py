"""Prompt templates live as Markdown next to each stage, so they can be tuned without code.

Placeholders are {{name}}; JSON braces in templates are left alone.
"""

from __future__ import annotations

from pathlib import Path


def render(stage_file: str, name: str, **values: object) -> str:
    path = Path(stage_file).parent / "prompts" / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def split(rendered: str) -> tuple[str, str]:
    """Templates hold a system part and a user part separated by a line '---USER---'."""
    system, _, user = rendered.partition("\n---USER---\n")
    return system.strip(), user.strip()
