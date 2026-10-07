import re
import threading
import urllib.error
import urllib.request
from datetime import date
from xml.etree import ElementTree as ET

import pytest

from briefing.curate import select
from briefing.feed import load_index, write_feed
from briefing.pipeline import run_pipeline
from briefing.serve import make_server

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"


def _items(cfg):
    root = ET.parse(cfg.public_dir / "feed.xml").getroot()
    return root, root.find("channel").findall("item")


def test_run_publishes_episode_to_feed(cfg):
    out = run_pipeline(cfg, date(2026, 10, 12))
    assert out["publish"]["feed_updated"] is True
    assert out["publish"]["feed_url"] == "http://192.168.1.50:8080/feed.xml"
    root, items = _items(cfg)
    assert root.find("channel/title").text == "Weekly AI Briefing"
    (item,) = items
    enc = item.find("enclosure")
    mp3 = cfg.public_dir / "episodes" / "2026-10-12-weekly-ai-briefing.mp3"
    assert enc.get("url") == "http://192.168.1.50:8080/episodes/2026-10-12-weekly-ai-briefing.mp3"
    assert enc.get("type") == "audio/mpeg" and int(enc.get("length")) == mp3.stat().st_size
    assert item.find("title").text.startswith("2026-10-12")
    assert item.find("guid").text == "ai-briefing-2026-10-12"
    assert item.find(f"{ITUNES}duration").text.count(":") == 2
    assert "<strong>PILOT</strong>" in item.find(f"{CONTENT}encoded").text


def test_rerun_replaces_not_duplicates(cfg):
    run_pipeline(cfg, date(2026, 10, 12))
    run_pipeline(cfg, date(2026, 10, 12), from_stage="publish")
    assert len(_items(cfg)[1]) == 1


def test_newest_first_and_pruned_to_keep_episodes(cfg, project):
    path = project / "config" / "config.yaml"
    path.write_text(path.read_text().replace("keep_episodes: 12", "keep_episodes: 2"))
    from briefing.config import load_config

    cfg = load_config(project)
    for d in (date(2026, 10, 12), date(2026, 10, 19), date(2026, 10, 26)):
        run_pipeline(cfg, d)
    guids = [i.find("guid").text for i in _items(cfg)[1]]
    assert guids == ["ai-briefing-2026-10-26", "ai-briefing-2026-10-19"]
    assert not (cfg.public_dir / "episodes" / "2026-10-12-weekly-ai-briefing.mp3").exists()


def test_feed_rebuild_after_base_url_change(cfg, project):
    run_pipeline(cfg, date(2026, 10, 12))
    path = project / "config" / "config.yaml"
    path.write_text(path.read_text().replace("http://192.168.1.50:8080", "http://10.0.0.7:9000/"))
    from briefing.config import load_config

    cfg2 = load_config(project)
    write_feed(cfg2, load_index(cfg2.public_dir))
    enc = _items(cfg2)[1][0].find("enclosure").get("url")
    assert enc.startswith("http://10.0.0.7:9000/episodes/")  # trailing slash normalised


def test_no_audio_means_feed_untouched(cfg, project):
    path = project / "config" / "config.yaml"
    path.write_text(re.sub(r"max_usd_per_episode: [0-9.]+", "max_usd_per_episode: 0.01", path.read_text()))
    from briefing.config import load_config

    with pytest.raises(Exception):
        run_pipeline(load_config(project), date(2026, 10, 12))
    assert not (cfg.public_dir / "feed.xml").exists()


def test_research_has_no_reserved_slot_by_default(cfg):
    assert cfg.settings.curation.min_research_score is None
    scored = [{"id": "a", "total": 9, "vendor": "1", "kind": "launch"},
              {"id": "b", "total": 8, "vendor": "2", "kind": "launch"},
              {"id": "r", "total": 6.5, "vendor": "hf", "kind": "research"}]
    chosen = select(scored, stories_min=1, stories_max=2, max_per_vendor=2, min_story_score=5,
                    min_research_score=cfg.settings.curation.min_research_score)
    assert [s["id"] for s in chosen] == ["a", "b"]


def test_preprints_are_labelled_for_the_scorer(cfg, fake_llm):
    run_pipeline(cfg, date(2026, 10, 12))
    prompt = fake_llm.prompts["score"]
    assert "preprint, NOT peer-reviewed" in prompt                  # on each research item
    assert "Research preprints" in prompt and "NOT peer-reviewed" in prompt  # in the rubric


# -- server -------------------------------------------------------------------

@pytest.fixture
def server(cfg):
    run_pipeline(cfg, date(2026, 10, 12))
    srv = make_server(cfg.public_dir, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", cfg
    srv.shutdown()
    srv.server_close()


def _get(url, headers=None):
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=5)


def test_serves_feed_with_rss_content_type(server):
    base, _ = server
    r = _get(f"{base}/feed.xml")
    assert r.status == 200 and r.headers["Content-Type"].startswith("application/rss+xml")
    assert b"<rss" in r.read()


def test_root_redirects_to_feed(server):
    base, _ = server
    r = _get(f"{base}/")
    assert r.geturl().endswith("/feed.xml")


def test_range_requests_for_resume_and_seek(server):
    base, cfg = server
    name = "2026-10-12-weekly-ai-briefing.mp3"
    full = (cfg.public_dir / "episodes" / name).read_bytes()
    r = _get(f"{base}/episodes/{name}", {"Range": "bytes=100-199"})
    assert r.status == 206 and r.headers["Content-Range"] == f"bytes 100-199/{len(full)}"
    assert r.read() == full[100:200]
    r = _get(f"{base}/episodes/{name}", {"Range": "bytes=-50"})
    assert r.read() == full[-50:]
    r = _get(f"{base}/episodes/{name}", {"Range": f"bytes={len(full) - 10}-"})
    assert r.read() == full[-10:]
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(f"{base}/episodes/{name}", {"Range": f"bytes={len(full) + 5}-"})
    assert e.value.code == 416


def test_no_directory_listing_or_escape(server):
    base, _ = server
    for path in ("/episodes/", "/../config/config.yaml", "/%2e%2e/config/config.yaml"):
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(base + path)
        assert e.value.code == 404
