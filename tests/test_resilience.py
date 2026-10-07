"""Structured output, resume-after-failure, and graceful degradation."""

from datetime import date
from types import SimpleNamespace

import pytest

from briefing.llm import LLMClient, LLMError
from briefing.pipeline import run_pipeline

EP = date(2026, 10, 12)


def test_every_structured_call_sends_a_schema(cfg, fake_llm):
    run_pipeline(cfg, EP)
    structured = [l for l in fake_llm.schemas if l != "translation"]
    assert structured and all(fake_llm.schemas[l] for l in structured)


class BadRequest(Exception):
    status_code = 400


def _anthropic_client(cfg, respond):
    """respond(kwargs) -> response object, or raises."""
    client = LLMClient(cfg)
    calls = []

    def create(**kw):
        calls.append(kw)
        return respond(kw)

    client._client = lambda provider: SimpleNamespace(messages=SimpleNamespace(create=create))
    return client, calls


def _text(payload, stop="end_turn"):
    import json
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5), stop_reason=stop)


def _tool(payload):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=payload)],
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5), stop_reason="tool_use")


TRICKY = {"lines": [{"speaker": "lead", "text": 'They call it "Models-as-a-Service", and it\'s "GA".'}]}


def test_native_structured_output_is_tried_first(cfg):
    from briefing import schemas
    from briefing.llm import complete_json

    client, calls = _anthropic_client(cfg, lambda kw: _text(TRICKY))
    assert complete_json(client, "synthesis", "segment:c1", "sys", "p", schema=schemas.LINES) == TRICKY
    fmt = calls[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False                      # strict copy
    assert fmt["schema"]["properties"]["lines"]["items"]["additionalProperties"] is False
    assert "tools" not in calls[0]


def test_falls_back_to_tool_mode_and_remembers(cfg):
    """A model that rejects native mode uses the forced tool call, and only asks once."""
    from briefing import schemas
    from briefing.llm import complete_json

    def respond(kw):
        if "output_config" in kw:
            raise BadRequest("output_config: not supported for this model")
        return _tool(TRICKY)

    client, calls = _anthropic_client(cfg, respond)
    complete_json(client, "synthesis", "a", "sys", "p", schema=schemas.LINES)
    complete_json(client, "synthesis", "b", "sys", "p", schema=schemas.LINES)
    assert ["output_config" in c for c in calls] == [True, False, False]
    assert client.structured_mode["claude-sonnet-5-5"] == "tool"


def test_sonnet_rejecting_forced_tool_still_gets_structured_output(cfg):
    """The exact error from the first real run: tool_choice rejected. Native mode must carry it."""
    from briefing import schemas
    from briefing.llm import complete_json

    def respond(kw):
        if "tool_choice" in kw:
            raise BadRequest('tool_choice: type "tool" and "any" are not supported for this model.')
        return _text(TRICKY)

    client, calls = _anthropic_client(cfg, respond)
    assert complete_json(client, "synthesis", "a", "sys", "p", schema=schemas.LINES) == TRICKY
    assert len(calls) == 1 and "output_config" in calls[0]


def test_last_resort_is_prompted_json(cfg):
    from briefing import schemas
    from briefing.llm import complete_json

    def respond(kw):
        if "output_config" in kw or "tool_choice" in kw:
            raise BadRequest("not supported for this model")
        return _text(TRICKY)

    client, calls = _anthropic_client(cfg, respond)
    assert complete_json(client, "synthesis", "a", "sys", "p", schema=schemas.LINES) == TRICKY
    assert client.structured_mode["claude-sonnet-5-5"] == "text"
    assert "valid JSON only" in calls[-1]["system"]


def test_mode_rejection_never_triggers_provider_fallback(cfg, monkeypatch):
    from briefing import schemas

    def respond(kw):
        if "output_config" in kw:
            raise BadRequest("output_config: not supported")
        return _tool(TRICKY)

    client, _ = _anthropic_client(cfg, respond)
    client.complete("synthesis", "s", "p", schema=schemas.LINES)
    assert client.usage[-1]["provider"] == "anthropic" and client.usage[-1]["fallback"] is False


def test_truncated_structured_output_is_an_error_not_a_partial(cfg):
    client, _ = _anthropic_client(cfg, lambda kw: _text({"lines": []}, stop="max_tokens"))
    cfg.settings.llm.fallback = None
    with pytest.raises(LLMError, match="truncated"):
        client.complete("synthesis", "s", "p", schema={"type": "object"})


def test_show_notes_flag_fallback_use(cfg, monkeypatch, fake_llm):
    real = fake_llm.complete

    def mark(*a, **k):
        r = real(*a, **k)
        fake_llm.usage[-1]["fallback"] = k.get("label", "").startswith("segment:")
        return r

    monkeypatch.setattr(fake_llm, "complete", mark)
    out = run_pipeline(cfg, EP)
    assert "## Fallback model used" in open(out["publish"]["show_notes"]).read()


def test_failed_stage_resumes_without_repaying_completed_calls(cfg, fake_llm):
    fake_llm.fail_labels = {"segment:outro"}
    with pytest.raises(Exception):
        run_pipeline(cfg, EP)
    memo = cfg.runs_dir / "2026-10-12" / "synthesise.memo.json"
    assert memo.exists()
    first_run_analyses = sum(l.startswith("analyse:") for l in fake_llm.labels)

    fake_llm.fail_labels = set()
    fake_llm.labels.clear()
    run_pipeline(cfg, EP)
    assert not any(l.startswith("analyse:") for l in fake_llm.labels)   # all reused
    assert not any(l.startswith("segment:c") for l in fake_llm.labels)  # all reused
    assert fake_llm.labels.count("segment:outro") == 1                  # only the failed call reran
    assert first_run_analyses > 0
    assert not memo.exists()                                            # cleared on success


def test_rerun_after_success_gets_fresh_output(cfg, fake_llm):
    run_pipeline(cfg, EP)
    fake_llm.labels.clear()
    run_pipeline(cfg, EP, from_stage="synthesise")
    assert any(l.startswith("analyse:") for l in fake_llm.labels)


def test_one_failed_segment_is_dropped_not_fatal(cfg, fake_llm):
    out = run_pipeline(cfg, EP)
    victim = out["curate"]["selected"][1]["id"]
    fake_llm.fail_labels = {f"segment:{victim}"}
    out = run_pipeline(cfg, EP, from_stage="synthesise")
    segments = {l["segment"] for l in out["synthesise"]["script"]["lines"]}
    assert f"story:{victim}" not in segments
    assert any(s.startswith("story:") for s in segments)
    notes = open(out["publish"]["show_notes"]).read()
    assert "Not in the audio this week" in notes


def test_factcheck_failure_keeps_segment_and_flags_it(cfg, fake_llm):
    out = run_pipeline(cfg, EP)
    victim = out["curate"]["selected"][0]["id"]
    fake_llm.fail_labels = {f"factcheck:{victim}"}
    out = run_pipeline(cfg, EP, from_stage="synthesise")
    assert f"story:{victim}" in {l["segment"] for l in out["synthesise"]["script"]["lines"]}
    assert "Fact-check didn't run" in open(out["publish"]["show_notes"]).read()
