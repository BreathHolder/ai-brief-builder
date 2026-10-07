"""Offline stand-ins for every configured feed, served via httpx.MockTransport.

Episode date used across tests: 2026-10-12, so the window is
2026-10-06T00:00Z to 2026-10-13T00:00Z.
"""

from __future__ import annotations

import json

import httpx

EN_PARAS = [
    "The release focuses on making enterprise deployments easier to operate and govern at scale.",
    "Teams can now route requests across multiple model providers with consistent policy enforcement.",
    "Early adopters report lower latency and simpler cost tracking compared with previous versions.",
    "The company says the feature is generally available today in all supported regions.",
]
ES_PARAS = [
    "Red Hat anunció hoy nuevas capacidades para OpenShift AI que simplifican el despliegue de modelos.",
    "Los equipos de plataforma pueden servir modelos de lenguaje con políticas de seguridad coherentes.",
    "La nueva versión mejora la observabilidad y reduce el costo de inferencia en clústeres híbridos.",
    "Según la empresa, la función estará disponible para todos los clientes a partir de este mes.",
]


def article_html(title: str, date: str | None, paras: list[str], lang: str = "en") -> str:
    meta = f'<meta property="article:published_time" content="{date}">' if date else ""
    body = "".join(f"<p>{p} {p}</p>" for p in paras)
    return (
        f'<html lang="{lang}"><head><title>{title}</title>{meta}</head>'
        f"<body><article><h1>{title}</h1>{body}</article></body></html>"
    )


def rss(items: list[tuple[str, str, str, str]]) -> str:
    """items: (title, link, rfc822 date, description)"""
    entries = "".join(
        f"<item><title>{t}</title><link>{l}</link><pubDate>{d}</pubDate><description>{s}</description></item>"
        for t, l, d, s in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{entries}</channel></rss>'


ATOM_RELEASES = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Release notes from kong</title>
<entry><id>tag:github.com,2008:Repository/1/3.14.0</id><updated>2026-10-08T15:00:00Z</updated>
<link rel="alternate" type="text/html" href="https://github.com/Kong/kong/releases/tag/3.14.0"/>
<title>3.14.0</title><content type="html">&lt;p&gt;AI Gateway: semantic routing across providers, token budget enforcement per consumer.&lt;/p&gt;</content>
</entry></feed>"""

PAGES: dict[str, tuple[str, str]] = {
    # OpenAI: one in-window post, one old post
    "https://openai.com/news/rss.xml": ("application/rss+xml", rss([
        ("Introducing multi-provider routing", "https://openai.com/index/multi-provider-routing/?utm_source=rss",
         "Fri, 09 Oct 2026 16:00:00 GMT", "Routing news"),
        ("An old announcement", "https://openai.com/index/old-thing/", "Tue, 01 Sep 2026 16:00:00 GMT", "Old"),
    ])),
    "https://openai.com/index/multi-provider-routing/": ("text/html", article_html(
        "Introducing multi-provider routing", "2026-10-09T16:00:00Z", EN_PARAS)),

    # InfoQ: full-text OpenAI coverage
    "https://feed.infoq.com/OpenAI/news/": ("application/rss+xml", rss([
        ("OpenAI Extends the Responses API for Agents", "https://www.infoq.com/news/2026/10/openai-responses-agents/?utm_source=feed",
         "Wed, 07 Oct 2026 09:00:00 GMT", "OpenAI adds a hosted agent loop to the Responses API"),
    ])),
    "https://www.infoq.com/news/2026/10/openai-responses-agents/": ("text/html", article_html(
        "OpenAI Extends the Responses API for Agents", "2026-10-07T09:00:00Z",
        ["OpenAI extended the Responses API with a built-in agent execution loop.",
         "Developers get a hosted container workspace and a shell tool.",
         "Context compaction keeps long-running agent sessions within limits.",
         "Reusable skills let teams package common agent behaviours."])),

    # Anthropic news listing (no feed): one article link, plus noise links
    "https://www.anthropic.com/news": ("text/html",
        '<a href="/news">News</a><a href="/news/claude-platform-update">Update</a>'
        '<a href="https://twitter.com/x">x</a><a href="/careers">Careers</a>'),
    "https://www.anthropic.com/news/claude-platform-update": ("text/html", article_html(
        "Claude platform update | Anthropic", "2026-10-07T12:00:00Z",
        ["Anthropic released new administration controls for enterprise customers.",
         "The update adds audit logging, usage analytics, and fine-grained workspace permissions.",
         "Customers in regulated industries asked for these controls during the past year.",
         "The features roll out to all enterprise plans over the coming two weeks."])),
    "https://www.anthropic.com/engineering": ("text/html", '<a href="/engineering/old-guide">Guide</a>'),
    "https://docs.example-claude.com/best-practices": ("text/html", article_html(
        "Best practices - Claude Code Docs", "2026-10-08T00:00:00Z", EN_PARAS)),
    "https://claude.com/blog": ("text/html", "<html></html>"),

    # Microsoft: Foundry post in window; Azure blog has an off-topic post and a
    # syndicated copy of the OpenAI post (same title) that should be merged.
    "https://devblogs.microsoft.com/foundry/feed/": ("application/rss+xml", rss([
        ("Foundry Agent Service adds MCP tools", "https://devblogs.microsoft.com/foundry/agent-mcp/",
         "Thu, 08 Oct 2026 10:00:00 GMT", "Agents"),
    ])),
    "https://devblogs.microsoft.com/foundry/agent-mcp/": ("text/html", article_html(
        "Foundry Agent Service adds MCP tools", "2026-10-08T10:00:00Z",
        ["Microsoft added Model Context Protocol tool support to the Foundry Agent Service.",
         "Agents can now call approved MCP servers with managed identity and network isolation.",
         "Administrators can restrict which tools each agent may use through policy.",
         "The capability is in public preview and free during the preview period."])),
    "https://azure.microsoft.com/en-us/blog/feed/": ("application/rss+xml", rss([
        ("Azure Storage pricing update", "https://azure.microsoft.com/en-us/blog/storage-pricing/",
         "Wed, 07 Oct 2026 10:00:00 GMT", "Storage costs are changing for archive tiers"),
        ("Introducing multi-provider routing", "https://azure.microsoft.com/en-us/blog/multi-provider-routing/",
         "Fri, 09 Oct 2026 18:00:00 GMT", "AI routing news, syndicated"),
        ("SQL Server on Azure Local is generally available", "https://azure.microsoft.com/en-us/blog/sql-local/",
         "Thu, 08 Oct 2026 18:00:00 GMT", "Run SQL Server on your own hardware with Azure management."),
    ])),
    "https://azure.microsoft.com/en-us/blog/sql-local/": ("text/html", article_html(
        "SQL Server on Azure Local is generally available", "2026-10-08T18:00:00Z",
        ["SQL Server now runs on Azure Local with unified management.", "Backups and patching are automated.",
         "Licensing follows the existing pay-as-you-go model.", "Later this year, AI features will also arrive."])),
    "https://azure.microsoft.com/en-us/blog/multi-provider-routing/": ("text/html", article_html(
        "Introducing multi-provider routing", "2026-10-09T18:00:00Z", EN_PARAS)),

    # Red Hat: a Kafka post (out of scope), an English OpenShift AI post, a Spanish one
    "https://www.redhat.com/en/rss/blog": ("application/rss+xml", rss([
        ("Kafka monthly digest", "https://www.redhat.com/en/blog/kafka-digest", "Thu, 08 Oct 2026 10:00:00 GMT",
         "Streaming news"),
        ("OpenShift AI 3.5 brings Models-as-a-Service", "https://www.redhat.com/en/blog/openshift-ai-35",
         "Tue, 06 Oct 2026 10:00:00 GMT", "OpenShift AI release"),
    ])),
    "https://www.redhat.com/en/blog/openshift-ai-35": ("text/html", article_html(
        "OpenShift AI 3.5 brings Models-as-a-Service", "2026-10-06T10:00:00Z",
        ["Red Hat OpenShift AI 3.5 introduces Models-as-a-Service for internal platform teams.",
         "Platform owners can publish governed model endpoints with quotas and chargeback.",
         "The release also adds llm-d based distributed inference as a technology preview.",
         "Existing customers can upgrade through the standard operator channel."])),
    "https://developers.redhat.com/blog/feed": ("application/rss+xml", rss([
        ("OpenShift AI: nuevas capacidades de inferencia", "https://developers.redhat.com/articles/2026/10/10/openshift-ai-es",
         "Sat, 10 Oct 2026 10:00:00 GMT", "OpenShift AI en español"),
    ])),
    "https://developers.redhat.com/articles/2026/10/10/openshift-ai-es": ("text/html", article_html(
        "OpenShift AI: nuevas capacidades de inferencia", "2026-10-10T10:00:00Z", ES_PARAS, lang="es")),

    # Kong: release atom; AI gateway blog listing empty this week
    "https://github.com/Kong/kong/releases.atom": ("application/atom+xml", ATOM_RELEASES),
    "https://github.com/Kong/kong/releases/tag/3.14.0": ("text/html", article_html(
        "Kong 3.14.0", "2026-10-08T15:00:00Z",
        ["Kong Gateway 3.14.0 ships AI Gateway semantic routing across model providers.",
         "Token budgets can now be enforced per consumer and per route.",
         "The release also fixes several memory issues under heavy streaming load.",
         "Upgrade notes describe one breaking change to the AI proxy plugin schema."])),
    "https://konghq.com/blog/tag/ai-gateway": ("text/html", "<html></html>"),
}


def hf_response(date: str) -> list[dict]:
    if date == "2026-10-09":
        return [
            {"paper": {"id": "2610.01234", "title": "Efficient Routing for Mixture-of-Agents",
                       "summary": "We propose a router that cuts inference cost by forty percent.",
                       "upvotes": 52, "authors": [{"name": "A. Researcher"}]}},
            {"paper": {"id": "2610.09999", "title": "A paper nobody upvoted",
                       "summary": "Low traction.", "upvotes": 2, "authors": []}},
        ]
    return []


REDIRECTS = {
    "https://www.anthropic.com/engineering/old-guide": "https://docs.example-claude.com/best-practices",
}


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url in REDIRECTS:
        return httpx.Response(301, headers={"location": REDIRECTS[url]})
    if request.url.host == "huggingface.co" and request.url.path == "/api/daily_papers":
        return httpx.Response(200, json=hf_response(request.url.params.get("date", "")))
    base = url.split("?")[0]
    page = PAGES.get(base)
    if page is None:
        return httpx.Response(404, text="not found")
    ctype, body = page
    return httpx.Response(200, text=body, headers={"content-type": ctype})


def mock_client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


class FakeLLM:
    """Stands in for LLMClient in every role, deterministically, offline."""

    def __init__(self, fail: bool = False, factcheck_issue: bool = False, bad_json_once: bool = False):
        self.fail = fail
        self.factcheck_issue = factcheck_issue
        self.bad_json_once = bad_json_once
        self.calls = 0
        self.labels: list[str] = []
        self.prompts: dict[str, str] = {}
        self.schemas: dict[str, dict | None] = {}
        self.fail_labels: set[str] = set()
        self.usage: list[dict] = []

    def complete(self, task, system, prompt, max_tokens=4096, label="", schema=None):
        import re
        from briefing.llm import LLMResult

        self.calls += 1
        self.labels.append(label or task)
        self.prompts[label or task] = system + "\n" + prompt
        self.schemas[label or task] = schema
        if self.fail or (label in self.fail_labels):
            raise RuntimeError("provider down")
        model = "claude-sonnet-5-5" if task != "translation" else "claude-haiku-4-5-20251001"
        self.usage.append({"task": task, "label": label, "provider": "fake", "model": model,
                           "input_tokens": 1000, "output_tokens": 200})

        def out(obj):
            return LLMResult(json.dumps(obj), "fake", model, 1000, 200)

        if task == "translation":
            return LLMResult(
                "<title>OpenShift AI: new inference capabilities</title>"
                "<body>Red Hat announced new OpenShift AI capabilities that simplify model deployment.</body>",
                "fake", model, 10, 10)
        if self.bad_json_once and not label.endswith(":retry"):
            self.bad_json_once = False
            return LLMResult("Sure! Here is the JSON you asked for: {not json", "fake", model, 10, 10)
        label = label.removesuffix(":retry")
        if label == "cluster":
            rows = re.findall(r"^\[(i\d+)\] \([^)]*\) (.*?) :: ", prompt, re.M)
            return out({"stories": [{"headline": title, "items": [iid]} for iid, title in rows]})
        if label == "score":
            blocks = re.findall(r"^\[(c\d+)\] (.*)$", prompt, re.M)
            scores = []
            for n, (cid, head) in enumerate(blocks):
                research = "hf-daily-papers" in prompt.split(f"[{cid}]")[1].split("\n[c")[0]
                base = 9 - n * 0.6
                scores.append({"story": cid, "impact": base, "novelty": base, "breadth": base, "signal": base,
                               "kind": "research" if research else "launch", "rationale": f"Matters: {head}"})
            return out({"scores": scores})
        if label == "thread":
            ids = re.findall(r"^\[(c\d+)\]", prompt, re.M)
            return out({"thread": "Platforms are converging on governed multi-provider routing.", "stories": ids[:2]})
        if label.startswith("analyse:"):
            urls = re.findall(r"\| (https?://\S+) \| published", prompt)
            return out({
                "what_happened": "A vendor shipped a feature.", "why_it_matters": "It changes platform choices.",
                "contested": label.endswith("c0"), "case_for": "It reduces toil.", "case_against": "It adds lock-in.",
                "so_what": "Worth a look for the AI Platform team.", "recommendation": "PILOT",
                "recommendation_reason": "Low effort, clear upside.",
                "key_facts": [{"fact": "It shipped this week.", "source_url": urls[0]},
                              {"fact": "Invented fact.", "source_url": "https://not-a-source.example/"}],
            })
        if label == "segment:intro":
            return out({"lines": [{"speaker": "lead", "text": "This is the Weekly AI Briefing. Big week."},
                                  {"speaker": "counterpoint", "text": "Let's get into it."}]})
        if label.startswith("segment:c"):
            urls = re.findall(r'"source_url": "(https?://[^"]+)"', prompt)
            return out({"lines": [
                {"speaker": "lead", "text": "Here is what happened this week. " * 20, "refs": urls[:1]},
                {"speaker": "counterpoint", "text": "But what about lock-in? " * 10, "refs": ["https://made-up.example/"]},
                {"speaker": "narrator", "text": "My recommendation: pilot it."},
                {"speaker": "lead", "text": "   "},
            ]})
        if label == "segment:outro":
            return out({"lines": [{"speaker": "lead", "text": "That's the week. This has been the Weekly AI Briefing."}]})
        if label.startswith("factcheck:"):
            if self.factcheck_issue:
                return out({"issues": [{"line": 0, "problem": "Overstated launch date.", "fix": "It shipped recently."}]})
            return out({"issues": []})
        raise AssertionError(f"FakeLLM has no response for task={task} label={label}")


class FakeTTS:
    """A short tone per word, so durations scale with text and the encode has real audio."""
    name, model = "fake", "fake-tts"

    def __init__(self):
        self.calls = []

    _blocks: dict[int, bytes] = {}

    @classmethod
    def _block(cls, freq: int) -> bytes:
        """10 ms of a sine tone per word, built once per frequency (keeps test encodes fast)."""
        if freq not in cls._blocks:
            import math
            import struct
            from briefing.audio import pcm

            n = int(pcm.SAMPLE_RATE * 0.01)
            cls._blocks[freq] = b"".join(
                struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * i / pcm.SAMPLE_RATE))) for i in range(n))
        return cls._blocks[freq]

    def synthesize(self, text, voice, instructions):
        self.calls.append((text, voice, instructions))
        return self._block(220 if voice == "cedar" else 330) * max(1, len(text.split()))
