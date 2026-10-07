import shutil
from pathlib import Path

import pytest

from briefing import audio, curate, ingest, normalise, synthesise
from briefing.config import load_config
from briefing.http import Fetcher
from tests.fixtures import FakeLLM, FakeTTS, mock_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# A fixed profile, so tests never depend on the (git-ignored) personal config/environment.md.
TEST_PROFILE = """# Environment profile

## Current platforms
- Red Hat OpenShift AI (model serving / ML platform)
- Kong as the AI gateway
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An isolated copy of the project config with its own data dir."""
    root = tmp_path / "proj"
    shutil.copytree(PROJECT_ROOT / "config", root / "config")
    (root / "config" / "environment.md").write_text(TEST_PROFILE)
    monkeypatch.setenv("BRIEFING_DATA_DIR", str(tmp_path / "data"))
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ELEVENLABS_API_KEY", "BRIEFING_FEED_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    return root


@pytest.fixture
def cfg(project):
    return load_config(project)


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def tts():
    return FakeTTS()


@pytest.fixture(autouse=True)
def offline(monkeypatch, fake_llm, tts):
    """No test touches the network or a real LLM."""
    monkeypatch.setattr(
        ingest, "make_fetcher", lambda config: Fetcher(config.settings.ingest, client=mock_client(), sleep=lambda s: None)
    )
    monkeypatch.setattr(normalise, "make_llm", lambda ctx: fake_llm)
    monkeypatch.setattr(curate, "make_llm", lambda ctx: fake_llm)
    monkeypatch.setattr(synthesise, "make_llm", lambda ctx: fake_llm)
    monkeypatch.setattr(audio, "make_provider", lambda config: tts)
