from datetime import date, datetime, timezone

from briefing import normalise
from briefing.llm import LLMClient
from briefing.models import Item
from briefing.normalise import dedupe, parse_translation
from briefing.pipeline import run_pipeline
from tests.fixtures import FakeLLM

EP = date(2026, 10, 12)


def _item(**kw):
    base = dict(source_id="s", vendor="v", tier="first_party", feed_url="f", title="T", url="https://a.com/x",
                url_canon="https://a.com/x", published=datetime(2026, 10, 8, tzinfo=timezone.utc), text="")
    base.update(kw)
    return Item(**base)


def test_non_english_item_translated(cfg, fake_llm):
    items = run_pipeline(cfg, EP)["normalise"]["items"]
    es = next(i for i in items if i["original_language"] == "es")
    assert es["language"] == "en"
    assert es["title"] == "OpenShift AI: new inference capabilities"
    assert es["original_title"] == "OpenShift AI: nuevas capacidades de inferencia"
    assert fake_llm.labels.count("translation") == 1


def test_english_items_never_sent_to_llm(cfg, fake_llm):
    out = run_pipeline(cfg, EP)["normalise"]
    assert out["translated"] == 1 and fake_llm.labels.count("translation") == 1


def test_translation_cached_across_runs(cfg, fake_llm):
    run_pipeline(cfg, EP)
    run_pipeline(cfg, EP, from_stage="normalise")
    assert fake_llm.labels.count("translation") == 1


def test_translation_failure_keeps_item(cfg, monkeypatch):
    monkeypatch.setattr(normalise, "make_llm", lambda ctx: FakeLLM(fail=True))
    out = run_pipeline(cfg, EP)["normalise"]
    assert len(out["translation_failures"]) == 1
    es = next(i for i in out["items"] if "nuevas" in i["title"])
    assert es["language"] == "es"


def test_syndicated_copy_merged_into_original(cfg):
    items = run_pipeline(cfg, EP)["normalise"]["items"]
    routing = [i for i in items if i["title"] == "Introducing multi-provider routing"]
    assert len(routing) == 1
    assert routing[0]["source_id"] == "openai"  # earliest publication wins
    assert routing[0]["also_reported_by"] == ["https://azure.microsoft.com/en-us/blog/multi-provider-routing/"]


def test_dedupe_by_shared_body_text():
    body = " ".join(f"word{i}" for i in range(300))
    a = _item(title="Launch post", url="https://a.com/1", url_canon="https://a.com/1", text=body)
    b = _item(title="Completely different headline", url="https://b.com/2", url_canon="https://b.com/2",
              text=body + " extra", tier="stack")
    kept, removed = dedupe([a, b], 0.88, 0.80)
    assert removed == 1 and kept[0].url == "https://a.com/1"


def test_dedupe_keeps_distinct_stories():
    a = _item(title="OpenAI ships agents SDK", url="https://a.com/1", url_canon="https://a.com/1")
    b = _item(title="Kong adds token budgets", url="https://b.com/2", url_canon="https://b.com/2")
    kept, removed = dedupe([a, b], 0.88, 0.80)
    assert removed == 0 and len(kept) == 2


def test_parse_translation():
    assert parse_translation("<title>Hi</title><body>Body text</body>") == ("Hi", "Body text")
    assert parse_translation("no tags at all") == (None, "no tags at all")


def test_llm_falls_back_to_secondary_provider(cfg, monkeypatch):
    from briefing.llm import LLMResult

    client = LLMClient(cfg)

    def fake_call(route, system, prompt, max_tokens, schema=None):
        if route.provider == "anthropic":
            raise RuntimeError("overloaded")
        return LLMResult("ok", route.provider, route.model, 1, 1)

    monkeypatch.setattr(client, "_call", fake_call)
    result = client.complete("translation", "s", "p")
    assert result.provider == "openai" and client.usage[0]["provider"] == "openai"
