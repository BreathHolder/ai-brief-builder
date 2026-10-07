from datetime import date

import pytest

from briefing.llm import parse_json, usage_cost
from briefing.pipeline import run_pipeline
from tests.fixtures import FakeLLM

EP = date(2026, 10, 12)


def _syn(cfg):
    return run_pipeline(cfg, EP)["synthesise"]


def test_script_opens_with_date_then_intro(cfg):
    lines = _syn(cfg)["script"]["lines"]
    assert lines[0]["text"] == "It's Monday, October 12th, 2026." and lines[0]["segment"] == "open"
    assert lines[1]["segment"] == "intro"
    assert lines[-1]["segment"] == "outro"


def test_every_selected_story_has_a_segment_in_order(cfg):
    out = run_pipeline(cfg, EP)
    expected = [f"story:{s['id']}" for s in out["curate"]["selected"]]
    seen = []
    for l in out["synthesise"]["script"]["lines"]:
        if l["segment"].startswith("story:") and l["segment"] not in seen:
            seen.append(l["segment"])
    assert seen == expected


def test_lines_are_sanitised(cfg):
    lines = _syn(cfg)["script"]["lines"]
    assert all(l["speaker"] in {"lead", "counterpoint"} for l in lines)        # "narrator" coerced
    assert all(l["text"].strip() for l in lines)                                # blank line dropped
    assert not any("made-up.example" in r for l in lines for r in l["source_refs"])  # invented refs stripped


def test_recommendations_normalised_and_facts_grounded(cfg):
    for a in _syn(cfg)["analyses"]:
        assert a["recommendation"] == "pilot"                                  # "PILOT" lower-cased
        assert all("not-a-source" not in f["source_url"] for f in a["key_facts"])
        assert a["sources"]


def test_factcheck_fix_applied_and_reported(cfg, monkeypatch):
    from briefing import synthesise

    monkeypatch.setattr(synthesise, "make_llm", lambda ctx: FakeLLM(factcheck_issue=True))
    out = _syn(cfg)
    assert out["factcheck_issues"]
    assert any(l["text"] == "It shipped recently." for l in out["script"]["lines"])


def test_factcheck_can_be_disabled(cfg, project, fake_llm):
    path = project / "config" / "config.yaml"
    path.write_text(path.read_text().replace("factcheck: true", "factcheck: false"))
    from briefing.config import load_config

    run_pipeline(load_config(project), EP)
    assert not any(l.startswith("factcheck:") for l in fake_llm.labels)


def test_invalid_json_is_retried_once(cfg, monkeypatch):
    from briefing import curate

    llm = FakeLLM(bad_json_once=True)
    monkeypatch.setattr(curate, "make_llm", lambda ctx: llm)
    run_pipeline(cfg, EP)
    assert "cluster:retry" in llm.labels


def test_show_notes_and_transcript(cfg):
    out = run_pipeline(cfg, EP)["publish"]
    notes = open(out["show_notes"]).read()
    assert "## This week's thread" in notes
    assert "**PILOT**" in notes and "Sources:" in notes
    assert "(LLM $" in notes
    transcript = open(out["transcript"]).read()
    assert "**Alex:** It's Monday, October 12th, 2026." in transcript
    assert "**Jordan:**" in transcript


def test_costs_recorded(cfg):
    out = run_pipeline(cfg, EP)
    assert out["curate"]["cost_usd"] > 0 and out["synthesise"]["cost_usd"] > 0


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Here you go: {"a": [1, 2]} hope that helps', {"a": [1, 2]}),
])
def test_parse_json_tolerates_wrapping(text, expected):
    assert parse_json(text) == expected


def test_usage_cost_and_unpriced_models():
    usage = [{"model": "m1", "input_tokens": 1_000_000, "output_tokens": 100_000},
             {"model": "mystery", "input_tokens": 5, "output_tokens": 5}]
    cost, unpriced = usage_cost(usage, {"m1": (2.0, 10.0)})
    assert cost == 3.0 and unpriced == ["mystery"]


def test_no_prompt_leaves_a_placeholder_unfilled(cfg, fake_llm):
    run_pipeline(cfg, EP)
    for label, prompt in fake_llm.prompts.items():
        assert "{{" not in prompt, f"unfilled placeholder in {label}"
