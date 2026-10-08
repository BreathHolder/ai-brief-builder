# AI Briefing

A weekly, opinionated, two-voice podcast briefing on AI platforms. It pulls
trusted sources, picks the 6–8 stories that matter, writes a script with a
point of view, and renders it as two-voice audio with OpenAI text-to-speech.

**Status: Phase 4 (delivery and automation).** Every Monday morning a systemd
timer builds the episode, and a feed server on your home network delivers it
to AntennaPod: a two-voice MP3 with a chapter per story, built from a
fact-checked script of the week's most important AI platform news.

## Setup (Linux)

Requires Python 3.11+ and ffmpeg (`sudo apt install ffmpeg`).

bash / zsh:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

fish:

```fish
python3 -m venv .venv
source .venv/bin/activate.fish
pip install -e '.[dev]'
```

Then create your local, git-ignored config:

```bash
cp .env.example .env && chmod 600 .env          # add ANTHROPIC_API_KEY and OPENAI_API_KEY
cp config/environment.example.md config/environment.md   # describe your environment
```

Set `episode.audience` in `config/config.yaml` to describe who the show is for
(e.g. "the head of an ML platform team at a mid-size bank"), or set
`BRIEFING_AUDIENCE` in `.env` to keep it private.

Verify:

```bash
pytest -q                  # 115 tests, all offline
briefing check             # config, sources, keys, profile
briefing sources --probe   # hits every real feed and reports what it returns
briefing run               # a real ingest for today's date
```

## Commands

| Command | What it does |
| --- | --- |
| `briefing check` | Validate config, list sources, report missing secrets |
| `briefing run [--date YYYY-MM-DD]` | Run every stage that has no checkpoint yet |
| `briefing run --from curate` | Rerun from a stage onward (e.g. after a prompt change) |
| `briefing run --from synthesise` | Rewrite the script only, keeping this week's story picks |
| `briefing run --only audio` | Rerun one stage using upstream checkpoints |
| `briefing run --force` | Ignore all checkpoints and rerun everything |
| `briefing status [--date …]` | Show checkpoint state per stage |
| `briefing voice-sample` | Render a short sample of each host's voice to `data/samples/` |
| `briefing voice-sample --voices cedar,ash,coral` | Audition several voices for the lead (add `--role counterpoint` for Jordan) |
| `briefing run --from audio` | Re-render audio only (cached lines are free) |
| `briefing serve` | Serve the feed and episodes on the LAN (what the feed service runs) |
| `briefing feed` | Rebuild `feed.xml` (after changing `feed.base_url`) and list episodes |
| `briefing install-systemd` | Install the weekly timer and feed server as user services (`--dry-run` to preview) |
| `briefing llm-check` | One tiny structured call per model route: mode used, fallback, errors |
| `briefing sources` | List sources and their feeds |
| `briefing sources --probe` | Fetch every feed live and report entries / newest date / errors |

## Layout

```
config/
  config.yaml       episode length, rubric weights, models, voices, budget
  sources.yaml      sources and their feeds (rss, hf_daily_papers, scrape)
  environment.example.md  template; copy to environment.md (git-ignored), YOUR living stack profile
briefing/
  ingest/ normalise/ curate/ synthesise/ audio/ publish/   one package per stage
  pipeline.py       orchestrator: order, checkpoints, locking, manifest
  context.py        per-run context; blocks profile access outside synthesis
  config.py         validated config loading
  models.py         Item, StoryCluster, Script records
  http.py           retries, backoff, per-host politeness
  store.py          SQLite cache: articles, translations, source health
  llm.py            per-task model routing, provider fallback, JSON retry, cost
  curate/prompts/   cluster, score, thread prompts (Markdown, edit freely)
  synthesise/prompts/ analyse, segment_*, factcheck prompts
data/runs/<date>/   checkpoints, run.log (JSON lines), manifest,
                    show-notes.md, transcript.md, <date>-weekly-ai-briefing.mp3
data/cache/         briefing.sqlite, audio/ (rendered lines, keyed by content)
data/samples/       voice auditions
data/public/        what the feed server serves: feed.xml, episodes/, episodes.json
```

## Design guarantees

- **Episodes open with the date.** The first line of every script is
  "It's Monday, October 12th, 2026." and titles start with the ISO date.
- **The profile never filters stories.** Only `synthesise` may read
  `environment.md`; any other stage that tries gets `ProfileAccessError`.
  Tests enforce this.
- **Failures resume cheaply.** Each stage checkpoints atomically, so if audio
  fails, the rerun reuses the curation and script already paid for.
- **One run per episode at a time.** A file lock blocks overlapping runs.
- **Secrets stay out of config.** API keys come only from `.env` / env vars.

Set `BRIEFING_DATA_DIR` to move run data off the repo (e.g. `/var/lib/ai-briefing`).

## How ingest decides what's in

- **Window:** items published in the 7 days up to the episode date.
- **Hugging Face:** Daily Papers only, at least 10 upvotes, top 5 per day.
- **Broad feeds** (Azure blog, Red Hat blogs) are scoped to AI content with
  `include_keywords`. That scopes the *source*; your stack plays no part.
- **Broad-feed keywords match the title and summary only**, so a passing
  mention of "AI" deep in an article doesn't pull it in.
- **Scraped sources** (Anthropic, Kong blog) take dates from the article page.
  A link that redirects off the blog (e.g. into docs) is not a post and is
  remembered as such. Site suffixes like "| Claude by Anthropic" are stripped
  via `title_strip`.
  An undated page is only trusted after the first run, so the first run never
  floods in a site's whole archive.
- **Sites that refuse automated access** (403) are respected, not worked
  around: the item keeps its feed text, and the rest of that site's pages are
  skipped for the run. OpenAI is set to `fetch_articles: false` for this reason.
- **Duplicates** (syndicated copies, tracking-param URLs) merge into the
  original, keeping the other URLs in `also_reported_by`.
- **Non-English** items are translated by the `translation` model route
  (Claude Haiku by default, OpenAI as fallback) and cached, so each article is
  translated once.
- **Health:** a source silent for 2 runs in a row is flagged in the show notes.

## Changing sources

Edit `config/sources.yaml`, then `briefing sources --probe`. If a scraped
site changes its layout, adjust `link_pattern` (a regex on the URL path).

## How an episode is made (Phase 2)

1. **Cluster** (Sonnet): items reporting the same event become one story.
   Anything the model drops comes back as its own story; nothing is lost.
2. **Score** (Sonnet): impact, novelty, breadth, signal, 1–10 each, weighted
   by `curation.weights`. The scorer never sees your profile.
3. **Select** (code, deterministic): highest scores first, at most 2 stories
   per vendor, 6–8 stories, one research story guaranteed if a strong one
   exists. Everything else goes to "Also this week".
4. **Thread** (Sonnet): one genuine theme across the picks, or none.
5. **Analyse** (Sonnet, one call per story): what happened, why it matters,
   the case for and against, and the so-what for *your* environment, ending
   in **evaluate / pilot / ignore**. This is the only place your profile is used.
6. **Dialogue** (Sonnet): intro, one segment per story, outro, written for
   two voices. Hosts are named in `episode.hosts`.
7. **Fact-check** (Haiku): every story segment is checked against its
   sources; unsupported facts are rewritten and listed in the show notes.

**Reliability:** every LLM call that returns data uses schema-enforced
structured output, so dialogue containing quotes can't corrupt it. On Claude
the client tries native structured output (`output_config`) first, then a
forced tool call, then prompted JSON, and remembers per model which one
works; on OpenAI it uses `json_schema`. If the fallback provider ever writes
part of an episode, the show notes say so. Run `briefing llm-check` after
changing any model. If a stage fails
partway, completed calls are remembered in `<stage>.memo.json` and the rerun
only pays for what's left; the memo is deleted once the stage succeeds. A
single story segment that fails is left out of the audio and flagged in the
show notes rather than sinking the episode.

The profile rule is enforced twice: `RunContext` blocks any other stage from
reading it, and a test confirms the story picks are identical with a
completely different profile.

**Cost:** about $0.30–0.50 per episode in LLM calls at October 2026 prices
(set in `llm.prices`), shown at the bottom of the show notes.

**Tuning:** prompts are Markdown files under `curate/prompts/` and
`synthesise/prompts/`. Edit, then `briefing run --from curate` (new picks) or
`--from synthesise` (same picks, new script).

## Audio (Phase 3)

- **Voices:** OpenAI `gpt-4o-mini-tts`. Alex (lead) is `cedar`, Jordan
  (counterpoint) is `marin`; each line is sent with its role's delivery
  instructions from `audio.style`. Other voices: alloy, ash, ballad, coral,
  echo, fable, nova, onyx, sage, shimmer, verse.
- **Assembly:** lines are stitched with short pauses (longer between
  segments), loudness-normalised to -16 LUFS and encoded to 128k MP3 with ID3
  tags and a chapter per story.
- **Cache:** every rendered line is cached by its text, voice and style, so
  `--from synthesise` followed by audio only pays for lines that changed.
- **Budget:** before rendering, projected spend (LLM so far + audio estimate)
  is checked against `budget.max_usd_per_episode`; over budget means no audio
  is rendered and the run stops with a clear message. Expect about
  $0.45–0.60 of audio per 30-minute episode.
- **Model sunset:** OpenAI shuts down `gpt-4o-mini-tts` on 2027-01-06. From 30
  days before, the log and show notes warn you. The provider layer
  (`briefing/audio/providers.py`) is where a replacement plugs in.
- **Stings:** set `audio.intro_sting` / `audio.outro_sting` to any audio file
  to bookend the episode.

## Delivery (Phase 4)

**One-time setup**

1. Give this machine a DHCP reservation on your router, then set
   `BRIEFING_FEED_BASE_URL=http://<its-LAN-IP>:8080` in `.env` (or
   `feed.base_url` in `config/config.yaml`).
2. If you run a firewall: `sudo ufw allow from 192.168.0.0/16 to any port 8080 proto tcp`
   (adjust to your LAN range).
3. `briefing install-systemd`, then run the three commands it prints.
4. In AntennaPod: **+ → Add podcast by URL** → `http://<its-LAN-IP>:8080/feed.xml`.
   To download only at home: Settings → Downloads → Automatic download →
   enable, then restrict it to your home Wi-Fi network.

**What runs**

- `ai-briefing-weekly.timer` fires on `schedule.on_calendar` (default Monday
  05:00 local time). `Persistent=true` means a run missed while the machine was
  off happens at next boot.
- `ai-briefing-feed.service` serves `data/public/` and restarts if it crashes.
- Each run adds the episode to the feed (newest first, last 12 kept) with show
  notes and source links, and AntennaPod picks it up on its next refresh.

**Checking on it**

```bash
systemctl --user list-timers ai-briefing-weekly.timer   # when it runs next
journalctl --user -u ai-briefing-weekly.service -n 100  # last run's log
systemctl --user start ai-briefing-weekly.service       # run now, the same way the timer does
```

If a weekly run fails, the feed simply doesn't get a new episode; fix the
cause and `briefing run` again (completed stages are reused).
