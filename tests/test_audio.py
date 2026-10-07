import re
import shutil
import subprocess
from datetime import date

import pytest

from briefing import audio as audio_stage
from briefing.audio import pcm
from briefing.audio.providers import split_text
from briefing.pipeline import run_pipeline

EP = date(2026, 10, 12)
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def _ffprobe_chapters(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-of", "compact", str(path)],
                         capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if l.startswith("chapter")]


@needs_ffmpeg
def test_episode_mp3_with_chapters(cfg, tts):
    out = run_pipeline(cfg, EP)
    a = out["audio"]
    assert a["status"] == "rendered" and a["audio_path"].endswith("2026-10-12-weekly-ai-briefing.mp3")
    titles = [c["title"] for c in a["chapters"]]
    assert titles[0] == "Intro" and titles[-1] == "Wrap-up"
    assert len(titles) == len(out["curate"]["selected"]) + 2
    assert len(_ffprobe_chapters(a["audio_path"])) == len(titles)
    assert a["duration_s"] > 2


@needs_ffmpeg
def test_each_role_gets_its_voice_and_style(cfg, tts):
    run_pipeline(cfg, EP)
    voices = {v for _, v, _ in tts.calls}
    assert voices == {"cedar", "marin"}
    lead_styles = {i for _, v, i in tts.calls if v == "cedar"}
    assert len(lead_styles) == 1 and "podcast host" in lead_styles.pop()


@needs_ffmpeg
def test_rerun_uses_cache(cfg, tts):
    run_pipeline(cfg, EP)
    first = len(tts.calls)
    tts.calls.clear()
    out = run_pipeline(cfg, EP, from_stage="audio")
    assert tts.calls == [] and out["audio"]["chunks_rendered"] == 0 and out["audio"]["chunks_cached"] >= first


def test_budget_blocks_audio_before_spending(cfg, project, tts):
    path = project / "config" / "config.yaml"
    path.write_text(re.sub(r"max_usd_per_episode: [0-9.]+", "max_usd_per_episode: 0.01", path.read_text()))
    from briefing.config import load_config

    with pytest.raises(audio_stage.BudgetExceeded, match="no audio rendered"):
        run_pipeline(load_config(project), EP)
    assert tts.calls == []


@needs_ffmpeg
def test_show_notes_report_audio_and_total_cost(cfg, tts):
    out = run_pipeline(cfg, EP)
    notes = open(out["publish"]["show_notes"]).read()
    assert "audio " in notes and "(LLM $" in notes and "+ audio $" in notes


def test_sunset_warning():
    from briefing.audio import sunset_warning

    assert sunset_warning("m", date(2027, 1, 6), date(2026, 10, 5)) is None      # 93 days out: quiet
    assert sunset_warning("m", date(2027, 1, 6), date(2026, 12, 6)) is None      # 31 days: quiet
    assert "30 days" in sunset_warning("m", date(2027, 1, 6), date(2026, 12, 7))  # 30 days: warns
    assert "shuts down" in sunset_warning("m", date(2027, 1, 6), date(2026, 12, 20))
    assert "passed" in sunset_warning("m", date(2027, 1, 6), date(2027, 1, 7))


def test_split_text_respects_limit_and_keeps_words():
    text = ("This is a sentence that goes on for a while. " * 80).strip()
    chunks = split_text(text, 500)
    assert all(len(c) <= 500 for c in chunks)
    assert " ".join(chunks).split() == text.split()


def test_split_text_handles_one_giant_sentence():
    text = "word " * 1000
    chunks = split_text(text.strip(), 300)
    assert all(len(c) <= 300 for c in chunks) and " ".join(chunks).split() == text.split()


def test_ffmetadata_escapes_special_characters():
    meta = pcm.ffmetadata({"title": "A=B; C#"}, [pcm.Chapter("x=y", 0, 10)])
    assert "title=A\\=B\\; C\\#" in meta and "title=x\\=y" in meta


def test_memo_never_stores_fallback_results(cfg, tmp_path):
    from briefing.llm import Memo, complete_json
    from tests.fixtures import FakeLLM

    llm = FakeLLM()
    real = llm.complete

    def fallback_answer(*a, **k):
        r = real(*a, **k)
        llm.usage[-1]["fallback"] = True
        return r

    llm.complete = fallback_answer
    memo = Memo(tmp_path / "m.json")
    complete_json(llm, "curation", "thread", "s", "[c0] x — y\n[c1] a — b\n[c2] c — d", memo=memo)
    assert memo.data == {}
