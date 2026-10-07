from datetime import date

import pytest

from briefing import curate
from briefing.curate import select
from briefing.pipeline import run_pipeline

EP = date(2026, 10, 12)


def _s(id, total, vendor="v", kind="launch"):
    return {"id": id, "total": total, "vendor": vendor, "kind": kind}


def test_episode_has_between_min_and_max_stories(cfg):
    out = run_pipeline(cfg, EP)["curate"]
    ep = cfg.settings.episode
    assert ep.stories_min <= len(out["selected"]) <= ep.stories_max


def test_nothing_is_lost_between_selected_and_also(cfg):
    out = run_pipeline(cfg, EP)
    n_items = len(out["normalise"]["items"])
    selected_items = sum(len(s["items"]) for s in out["curate"]["selected"])
    assert selected_items + len(out["curate"]["also_this_week"]) == n_items  # singleton clusters in the fake


def test_vendor_cap():
    scored = [_s("a", 9, "openai"), _s("b", 8.5, "openai"), _s("c", 8, "openai"), _s("d", 7, "kong")]
    chosen = select(scored, stories_min=1, stories_max=8, max_per_vendor=2, min_story_score=5, min_research_score=6)
    assert [s["id"] for s in chosen] == ["a", "b", "d"]


def test_weak_stories_only_fill_up_to_minimum():
    scored = [_s("a", 9, "1"), _s("b", 4, "2"), _s("c", 3, "3"), _s("d", 2, "4")]
    chosen = select(scored, stories_min=3, stories_max=8, max_per_vendor=2, min_story_score=5, min_research_score=6)
    assert [s["id"] for s in chosen] == ["a", "b", "c"]


def test_research_guarantee_swaps_out_weakest():
    scored = [_s("a", 9, "1"), _s("b", 8, "2"), _s("c", 7, "3"), _s("r", 6.5, "hf", "research")]
    chosen = select(scored, stories_min=1, stories_max=3, max_per_vendor=2, min_story_score=5, min_research_score=6)
    assert {s["id"] for s in chosen} == {"a", "b", "r"}


def test_research_not_forced_when_weak():
    scored = [_s("a", 9, "1"), _s("b", 8, "2"), _s("r", 4, "hf", "research")]
    chosen = select(scored, stories_min=1, stories_max=2, max_per_vendor=2, min_story_score=5, min_research_score=6)
    assert {s["id"] for s in chosen} == {"a", "b"}


def test_cluster_recovers_items_the_model_dropped(cfg, monkeypatch, fake_llm):
    from briefing.llm import LLMResult

    real = fake_llm.complete

    def drops_items(task, system, prompt, max_tokens=4096, label="", schema=None):
        if label == "cluster":
            return LLMResult('{"stories": [{"headline": "Only one", "items": ["i0", "i0", "i999"]}]}', "f", "m", 1, 1)
        return real(task, system, prompt, max_tokens, label, schema)

    monkeypatch.setattr(fake_llm, "complete", drops_items)
    out = run_pipeline(cfg, EP)
    total = sum(len(s["items"]) for s in out["curate"]["selected"]) + len(out["curate"]["also_this_week"])
    assert total == len(out["normalise"]["items"])


def test_curation_never_sees_the_profile(cfg, fake_llm):
    run_pipeline(cfg, EP)
    marker = "Red Hat OpenShift AI (model serving"   # a line from the test profile
    for label in ("cluster", "score", "thread"):
        assert marker not in fake_llm.prompts[label]
    assert marker in next(v for k, v in fake_llm.prompts.items() if k.startswith("analyse:"))


def test_selection_identical_with_a_different_profile(cfg, project):
    first = [s["headline"] for s in run_pipeline(cfg, EP)["curate"]["selected"]]
    (project / "config" / "environment.md").write_text("# Environment\n- Only uses mainframes.\n")
    second = [s["headline"] for s in run_pipeline(cfg, EP, force=True)["curate"]["selected"]]
    assert first == second


def test_story_vendor_from_items(cfg):
    for s in run_pipeline(cfg, EP)["curate"]["selected"]:
        assert s["vendor"] in {i["vendor"] for i in s["items"]}
