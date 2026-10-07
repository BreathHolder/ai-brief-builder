import json
from datetime import date

import pytest

from briefing import pipeline
from briefing.context import ProfileAccessError, RunContext
from briefing.logging_setup import get_logger
from briefing.pipeline import PipelineError, run_pipeline, run_status
from briefing.synthesise import spoken_date

EP = date(2026, 10, 12)


def test_full_run_writes_all_checkpoints(cfg):
    out = run_pipeline(cfg, EP)
    run_dir = cfg.runs_dir / "2026-10-12"
    assert [s for s, st in run_status(cfg, EP) if st == "done"] == pipeline.STAGE_NAMES
    assert (run_dir / "show-notes.md").exists()
    assert (run_dir / "run.log").stat().st_size > 0
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert all(v["status"] == "done" for v in manifest["stages"].values())
    assert out["publish"]["show_notes"].endswith("show-notes.md")


def test_episode_opens_with_the_date(cfg):
    out = run_pipeline(cfg, EP)
    first = out["synthesise"]["script"]["lines"][0]
    assert first == {"speaker": "lead", "text": "It's Monday, October 12th, 2026.", "source_refs": [], "segment": "open"}
    assert out["synthesise"]["script"]["title"].startswith("2026-10-12")


@pytest.mark.parametrize(
    "d,expected",
    [(date(2026, 10, 1), "1st"), (date(2026, 10, 2), "2nd"), (date(2026, 10, 3), "3rd"),
     (date(2026, 10, 11), "11th"), (date(2026, 10, 13), "13th"), (date(2026, 10, 22), "22nd")],
)
def test_spoken_date_ordinals(d, expected):
    assert f" {expected}," in spoken_date(d)


def test_rerun_skips_completed_stages(cfg):
    run_pipeline(cfg, EP)
    cp = cfg.runs_dir / "2026-10-12" / "00-ingest.json"
    before = cp.stat().st_mtime_ns
    run_pipeline(cfg, EP)
    assert cp.stat().st_mtime_ns == before


def test_from_stage_reruns_downstream_only(cfg):
    run_pipeline(cfg, EP)
    run_dir = cfg.runs_dir / "2026-10-12"
    ingest_t = (run_dir / "00-ingest.json").stat().st_mtime_ns
    curate_t = (run_dir / "02-curate.json").stat().st_mtime_ns
    run_pipeline(cfg, EP, from_stage="curate")
    assert (run_dir / "00-ingest.json").stat().st_mtime_ns == ingest_t
    assert (run_dir / "02-curate.json").stat().st_mtime_ns > curate_t


def test_only_requires_upstream_checkpoints(cfg):
    with pytest.raises(PipelineError, match="no checkpoint"):
        run_pipeline(cfg, EP, only="audio")


def test_failed_stage_resumes_without_redoing_earlier_work(cfg, monkeypatch):
    calls = {"n": 0}

    def flaky(ctx, inputs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated ElevenLabs outage")
        return {"audio_path": None, "characters": 0, "status": "stub"}

    stages = [pipeline.Stage(s.name, flaky if s.name == "audio" else s.fn, s.profile_allowed) for s in pipeline.STAGES]
    monkeypatch.setattr(pipeline, "STAGES", stages)

    with pytest.raises(RuntimeError):
        run_pipeline(cfg, EP)
    run_dir = cfg.runs_dir / "2026-10-12"
    synth_t = (run_dir / "03-synthesise.json").stat().st_mtime_ns
    assert not (run_dir / "04-audio.json").exists()

    run_pipeline(cfg, EP)
    assert (run_dir / "03-synthesise.json").stat().st_mtime_ns == synth_t
    assert (run_dir / "05-publish.json").exists()


@pytest.mark.parametrize("stage", ["ingest", "normalise", "curate", "audio", "publish"])
def test_only_synthesis_may_read_profile(cfg, stage):
    ctx = RunContext(config=cfg, episode_date=EP, run_dir=cfg.runs_dir, logger=get_logger())
    with pytest.raises(ProfileAccessError):
        ctx.for_stage(stage, profile_allowed=False).read_profile()


def test_pipeline_grants_profile_to_synthesis_only():
    allowed = {s.name for s in pipeline.STAGES if s.profile_allowed}
    assert allowed == {"synthesise"}
