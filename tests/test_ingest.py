from datetime import date

import httpx
import pytest

from briefing import ingest
from briefing.config import Feed, IngestConfig
from briefing.http import Fetcher
from briefing.ingest.extract import explicit_date
from briefing.ingest.fetchers import extract_links, matches_keywords
from briefing.pipeline import run_pipeline
from briefing.store import Store
from briefing.urls import canonical_url
from tests.fixtures import article_html

EP = date(2026, 10, 12)


def _ingest(cfg):
    return run_pipeline(cfg, EP, only=None)["ingest"]


def test_every_source_contributes(cfg):
    out = _ingest(cfg)
    counts = {k: v["items"] for k, v in out["per_source"].items()}
    assert counts == {"hf-daily-papers": 1, "openai": 1, "infoq-openai": 1, "anthropic": 1, "microsoft-ai": 2,
                      "redhat-openshift-ai": 2, "kong-ai-gateway": 1}
    assert all(v["status"] == "ok" for v in out["per_source"].values())


def test_window_filter_drops_old_items(cfg):
    titles = {i["title"] for i in _ingest(cfg)["items"]}
    assert "An old announcement" not in titles


def test_keyword_scoping_on_broad_feeds(cfg):
    titles = {i["title"] for i in _ingest(cfg)["items"]}
    assert "Kafka monthly digest" not in titles          # Red Hat, not AI
    assert "Azure Storage pricing update" not in titles  # Azure, not AI
    # mentions AI only in its last paragraph: scoped on title + summary, so out
    assert "SQL Server on Azure Local is generally available" not in titles
    assert "OpenShift AI 3.5 brings Models-as-a-Service" in titles


def test_hf_papers_filtered_by_upvotes(cfg):
    papers = [i for i in _ingest(cfg)["items"] if i["source_id"] == "hf-daily-papers"]
    assert [p["extra"]["upvotes"] for p in papers] == [52]
    assert papers[0]["url"] == "https://huggingface.co/papers/2610.01234"


def test_scraped_source_gets_text_and_date(cfg):
    item = next(i for i in _ingest(cfg)["items"] if i["source_id"] == "anthropic")
    assert item["title"] == "Claude platform update"
    assert item["published"].startswith("2026-10-07T12:00")
    assert "audit logging" in item["text"]


def test_site_suffix_stripped_from_scraped_titles(cfg):
    item = next(i for i in _ingest(cfg)["items"] if i["source_id"] == "anthropic")
    assert item["title"] == "Claude platform update"


def test_scraped_link_redirecting_off_the_blog_is_skipped(cfg):
    out = _ingest(cfg)
    titles = {i["title"] for i in out["items"]}
    assert not any("Best practices" in t for t in titles)
    eng = out["per_source"]["anthropic"]["feeds"]["https://www.anthropic.com/engineering"]
    assert eng["redirect_skipped"] == 1


def test_release_titles_get_prefix(cfg):
    titles = {i["title"] for i in _ingest(cfg)["items"]}
    assert "Kong Gateway 3.14.0" in titles


def test_full_text_fetched_not_just_summary(cfg):
    item = next(i for i in _ingest(cfg)["items"] if i["url"] == "https://devblogs.microsoft.com/foundry/agent-mcp/")
    assert "managed identity" in item["text"]


def test_articles_cached_between_runs(cfg, monkeypatch):
    run_pipeline(cfg, EP)
    calls = []
    real = ingest.fetch_article
    monkeypatch.setattr(ingest, "fetch_article", lambda f, u, m: calls.append(u) or real(f, u, m))
    run_pipeline(cfg, EP, from_stage="ingest")
    assert calls == []  # everything came from the SQLite cache


def test_one_failing_feed_does_not_sink_the_run(cfg, monkeypatch):
    real = ingest._list_entries

    def flaky(fetcher, feed, since, until):
        if "openai.com" in feed.url:
            raise httpx.ConnectError("boom")
        return real(fetcher, feed, since, until)

    monkeypatch.setattr(ingest, "_list_entries", flaky)
    out = _ingest(cfg)
    assert out["per_source"]["openai"]["status"] == "error"
    assert out["per_source"]["anthropic"]["items"] == 1


def test_all_sources_failing_aborts(cfg, monkeypatch):
    monkeypatch.setattr(ingest, "_list_entries", lambda *a: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    with pytest.raises(RuntimeError, match="every source failed"):
        run_pipeline(cfg, EP)


def test_health_warning_after_consecutive_silent_runs(cfg, monkeypatch):
    real = ingest._list_entries
    monkeypatch.setattr(
        ingest, "_list_entries",
        lambda f, feed, s, u: [] if "openai.com" in feed.url else real(f, feed, s, u),
    )
    first = run_pipeline(cfg, date(2026, 10, 12))["ingest"]["health_warnings"]
    second = run_pipeline(cfg, date(2026, 10, 19))["ingest"]["health_warnings"]
    assert first == []
    assert any(w.startswith("OpenAI: no items for 2 consecutive runs") for w in second)


def test_undated_scrape_items_wait_for_baseline(tmp_path):
    from datetime import datetime, timezone

    from briefing.context import RunContext
    from briefing.config import Source
    from briefing.logging_setup import get_logger

    pages = {
        "https://example.com/blog": '<a href="/blog/post-one">1</a>',
        "https://example.com/blog/post-one": article_html("Post one", None, ["Text " * 40] * 3),
    }
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=pages[str(r.url)]) if str(r.url) in pages else httpx.Response(404)))
    fetcher = Fetcher(IngestConfig(window_days=7, request_timeout_s=5, user_agent="t"), client=client, sleep=lambda s: None)
    store = Store(tmp_path / "s.sqlite")
    feed = Feed(kind="scrape", url="https://example.com/blog", link_pattern=r"^/blog/[a-z-]+$")
    source = Source(id="ex", name="Ex", vendor="ex", tier="first_party", feeds=[feed])
    ctx = RunContext(config=None, episode_date=EP, run_dir=tmp_path, logger=get_logger(), stage="ingest")  # type: ignore[arg-type]
    ctx.config = type("C", (), {"settings": type("S", (), {"ingest": fetcher.cfg})()})()
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    until = datetime(2100, 1, 1, tzinfo=timezone.utc)

    items, stats = ingest.collect_feed(ctx, fetcher, store, source, feed, since, until)
    assert items == [] and stats["undated_skipped"] == 1   # first run: archive, not news

    pages["https://example.com/blog"] += '<a href="/blog/post-two">2</a>'
    pages["https://example.com/blog/post-two"] = article_html("Post two", None, ["Words " * 40] * 3)
    items, _ = ingest.collect_feed(ctx, fetcher, store, source, feed, since, until)
    assert [i.title for i in items] == ["Post one", "Post two"]  # first-seen dates now trusted


@pytest.mark.parametrize("url,expected", [
    ("https://www.Example.com/a/b/?utm_source=rss&id=3#frag", "https://example.com/a/b?id=3"),
    ("http://example.com/", "https://example.com/"),
    ("https://example.com/x?ref=hn", "https://example.com/x"),
])
def test_canonical_url(url, expected):
    assert canonical_url(url) == expected


def test_extract_links_filters_host_pattern_and_self():
    html = ('<a href="/news">self</a><a href="/news/a-post">a</a><a href="/news/a-post#x">dup</a>'
            '<a href="https://other.com/news/b">ext</a><a href="/careers">no</a>')
    assert extract_links("https://site.com/news", html, r"^/news/[a-z-]+$", 10) == ["https://site.com/news/a-post"]


def test_keyword_matching_short_tokens_are_whole_words():
    assert matches_keywords("New AI features", ["AI"])
    assert not matches_keywords("Maintenance window", ["AI"])
    assert matches_keywords("Running vLLM on OpenShift", ["vLLM"])


@pytest.mark.parametrize("html,expected", [
    ('<meta property="article:published_time" content="2026-10-07T12:00:00Z">', "2026-10-07T12:00:00+00:00"),
    ('<meta content="2026-10-03" itemprop="datePublished">', "2026-10-03T00:00:00+00:00"),
    ('<script type="application/ld+json">{"@graph":[{"datePublished":"2026-10-04T08:00:00+02:00"}]}</script>',
     "2026-10-04T08:00:00+02:00"),
    ('<time datetime="2026-10-05">Oct 5</time>', "2026-10-05T00:00:00+00:00"),
])
def test_explicit_date_sources(html, expected):
    assert explicit_date(html).isoformat() == expected


def test_fetcher_retries_then_succeeds():
    hits = {"n": 0}

    def handler(request):
        hits["n"] += 1
        return httpx.Response(503) if hits["n"] < 3 else httpx.Response(200, text="ok")

    f = Fetcher(IngestConfig(window_days=7, request_timeout_s=5, user_agent="t", max_retries=3),
                client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    assert f.get("https://x.test/").text == "ok" and hits["n"] == 3


def test_fetcher_gives_up_after_max_retries():
    f = Fetcher(IngestConfig(window_days=7, request_timeout_s=5, user_agent="t", max_retries=2),
                client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))), sleep=lambda s: None)
    with pytest.raises(httpx.HTTPStatusError):
        f.get("https://x.test/")


def test_blocked_host_is_not_hammered(cfg, monkeypatch):
    import httpx as _httpx
    from tests import fixtures

    hits = []
    real = fixtures.handler

    def handler(request):
        if request.url.host == "devblogs.microsoft.com" and request.url.path != "/foundry/feed/":
            hits.append(str(request.url))
            return _httpx.Response(403)
        return real(request)

    from briefing import ingest as _ingest
    from briefing.http import Fetcher
    monkeypatch.setattr(fixtures, "handler", handler)
    monkeypatch.setattr(_ingest, "make_fetcher", lambda c: Fetcher(
        c.settings.ingest, client=_httpx.Client(transport=_httpx.MockTransport(handler), follow_redirects=True),
        sleep=lambda s: None))
    # add a second item from the same host so we can see the breaker trip
    page = fixtures.PAGES["https://devblogs.microsoft.com/foundry/feed/"]
    monkeypatch.setitem(fixtures.PAGES, "https://devblogs.microsoft.com/foundry/feed/", (page[0], fixtures.rss([
        ("Foundry Agent Service adds MCP tools", "https://devblogs.microsoft.com/foundry/agent-mcp/",
         "Thu, 08 Oct 2026 10:00:00 GMT", "Agents get MCP tools"),
        ("Foundry evaluations update", "https://devblogs.microsoft.com/foundry/evals/",
         "Thu, 08 Oct 2026 11:00:00 GMT", "New model evaluations"),
    ])))
    out = run_pipeline(cfg, EP)["ingest"]
    ms = [i for i in out["items"] if "devblogs" in i["url"]]
    assert len(ms) == 2                       # both kept, on feed text
    assert len(hits) == 1                     # second page never requested
    assert out["per_source"]["microsoft-ai"]["feeds"]["https://devblogs.microsoft.com/foundry/feed/"]["feed_text_only"] == 1


def test_fetch_articles_false_uses_feed_text(cfg):
    item = next(i for i in _ingest(cfg)["items"] if i["source_id"] == "openai")
    assert item["text"] == "Routing news"     # from the RSS description; no page fetch
