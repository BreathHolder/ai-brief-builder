import pytest

from briefing.config import ConfigError, load_config


def test_project_config_loads(cfg):
    assert cfg.settings.episode.target_minutes == 30
    assert cfg.settings.episode.open_with_date is True
    assert len(cfg.sources) == 7
    assert "ANTHROPIC_API_KEY" in cfg.secrets.missing()


def test_weights_must_sum_to_one(project):
    path = project / "config" / "config.yaml"
    path.write_text(path.read_text().replace("impact: 0.40", "impact: 0.90"))
    with pytest.raises(ConfigError, match="sum to 1.0"):
        load_config(project)


def test_duplicate_source_ids_rejected(project):
    path = project / "config" / "sources.yaml"
    path.write_text(path.read_text().replace("id: openai", "id: anthropic"))
    with pytest.raises(ConfigError, match="duplicate source ids"):
        load_config(project)


def test_missing_profile_rejected(project):
    (project / "config" / "environment.md").unlink()
    with pytest.raises(ConfigError, match="environment profile"):
        load_config(project)


def test_audience_env_override(project, monkeypatch):
    monkeypatch.setenv("BRIEFING_AUDIENCE", "a test listener")
    assert load_config(project).settings.episode.audience == "a test listener"
