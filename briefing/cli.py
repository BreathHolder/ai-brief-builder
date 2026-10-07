"""Command line entry point: `briefing check | run | status`."""

from __future__ import annotations

import argparse
import sys
from datetime import date

from briefing.config import ConfigError, load_config
from briefing.pipeline import STAGE_NAMES, PipelineError, run_pipeline, run_status


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc


def cmd_check(args: argparse.Namespace) -> int:
    cfg = load_config()
    print(f"config ok: {cfg.root}")
    print(f"data dir: {cfg.data_dir}")
    enabled = [s for s in cfg.sources if s.enabled]
    print(f"sources: {len(enabled)} enabled, {sum(len(s.feeds) for s in enabled)} feeds")
    for s in enabled:
        kinds = ", ".join(sorted({f.kind for f in s.feeds}))
        print(f"  - {s.id:<22} {len(s.feeds)} feed(s)  {kinds}")
    s = cfg.secrets
    print(f"ANTHROPIC_API_KEY:  {'set' if s.anthropic_api_key else 'MISSING (required: curation, synthesis, translation)'}")
    print(f"OPENAI_API_KEY:     {'set' if s.openai_api_key else 'MISSING (required: audio, and the LLM fallback)'}")
    feed = cfg.settings.feed
    print(f"feed:               {feed.base_url}/feed.xml" + ("" if feed.enabled else "  (disabled)"))
    if "192.168.1.50" in feed.base_url:
        print("                    ^ placeholder: set feed.base_url to this machine's LAN IP")
    print(f"profile: {cfg.profile_path} ({len(cfg.read_profile())} chars)")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    from datetime import datetime, timedelta, timezone

    from briefing.ingest import _list_entries, make_fetcher

    cfg = load_config()
    if not args.probe:
        for s in cfg.sources:
            print(f"{s.id} ({s.vendor}, {s.tier}){'' if s.enabled else '  [disabled]'}")
            for f in s.feeds:
                print(f"    {f.kind:<16} {f.url}")
        return 0

    until = datetime.now(timezone.utc)
    since = until - timedelta(days=cfg.settings.ingest.window_days)
    failures = 0
    with make_fetcher(cfg) as fetcher:
        for s in cfg.sources:
            if not s.enabled:
                continue
            for f in s.feeds:
                try:
                    entries = _list_entries(fetcher, f, since, until)
                    dated = [e.published for e in entries if e.published]
                    newest = max(dated).date().isoformat() if dated else "n/a (dates come from article pages)"
                    print(f"OK    {s.id:<22} {f.kind:<16} {len(entries):>3} entries, newest {newest}  {f.url}")
                except Exception as exc:
                    failures += 1
                    print(f"FAIL  {s.id:<22} {f.kind:<16} {f.url}\n      {type(exc).__name__}: {exc}")
    print(f"\n{failures} feed(s) failed" if failures else "\nall feeds responded")
    return 1 if failures else 0


def cmd_llm_check(args: argparse.Namespace) -> int:
    from briefing.llm import LLMClient, complete_json, usage_cost

    cfg = load_config()
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "quote": {"type": "string"}},
              "required": ["ok", "quote"]}
    prompt = 'Return ok=true and quote set to: She said "it works", and it does.'
    problems = 0
    for task, route in cfg.settings.llm.tasks.items():
        client = LLMClient(cfg)
        try:
            out = complete_json(client, task, "llm-check", "You are a test endpoint.", prompt,
                                max_tokens=200, schema=None if task == "translation" else schema)
            used = client.usage[-1]
            mode = client.structured_mode.get(used["model"], "text" if task == "translation" else "-")
            flag = "  <-- FALLBACK" if used.get("fallback") else ""
            ok = isinstance(out, dict) and out.get("ok") is True
            problems += (not ok) + bool(flag)
            print(f"{'OK  ' if ok and not flag else 'WARN'} {task:<12} {used['provider']}/{used['model']:<28} mode={mode}{flag}")
        except Exception as exc:
            problems += 1
            print(f"FAIL {task:<12} {route.provider}/{route.model}: {exc}")
    print("\nall routes healthy" if not problems else f"\n{problems} problem(s); see above")
    return 1 if problems else 0


SAMPLE_LINES = {
    "lead": "It's Monday, October twelfth. This week, three of the biggest AI platforms shipped "
            "agent features within days of each other, and that tells us something about where this is heading.",
    "counterpoint": "Maybe. But shipping a feature isn't the same as customers trusting it in production. "
                    "I'd want to see who's actually running this at scale before we call it a trend.",
}


def cmd_voice_sample(args: argparse.Namespace) -> int:
    from briefing.audio import pcm
    from briefing.audio.providers import make_provider

    cfg = load_config()
    audio = cfg.settings.audio
    provider = make_provider(cfg)
    out_dir = cfg.data_dir / "samples"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.voices:
        jobs = [(v.strip(), args.role) for v in args.voices.split(",") if v.strip()]
    else:
        jobs = [(audio.voices.lead, "lead"), (audio.voices.counterpoint, "counterpoint")]
    for voice, role in jobs:
        style = getattr(audio.style, role)
        data = provider.synthesize(SAMPLE_LINES[role], voice, style)
        path = out_dir / f"{role}-{voice}.mp3"
        pcm.encode_mp3(data, path, tags={"title": f"{role} sample: {voice}"}, chapters=[],
                       loudness_lufs=audio.loudness_lufs, bitrate=audio.bitrate, workdir=out_dir / ".work")
        print(f"{path}  ({pcm.duration_s(data):.1f}s)")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from briefing.logging_setup import get_logger
    from briefing.serve import make_server

    cfg = load_config()
    feed = cfg.settings.feed
    host, port = args.host or feed.host, args.port or feed.port
    logger = get_logger()
    server = make_server(cfg.public_dir, host, port)
    logger.info(f"serving {cfg.public_dir} on {host}:{port}; subscribe in AntennaPod to {feed.base_url}/feed.xml")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_feed(args: argparse.Namespace) -> int:
    from briefing.feed import load_index, write_feed

    cfg = load_config()
    index = load_index(cfg.public_dir)
    path = write_feed(cfg, index)
    print(f"feed rebuilt: {path} ({len(index)} episode(s))")
    print(f"subscribe URL: {cfg.settings.feed.base_url}/feed.xml")
    for e in index:
        print(f"  {e['date']}  {e['title']}  ({int(e['duration_s'] // 60)} min)")
    return 0


UNIT_WEEKLY = """[Unit]
Description=AI Briefing: build this week's episode

[Service]
Type=oneshot
WorkingDirectory={root}
ExecStart={bin} run
Environment=PYTHONUNBUFFERED=1
TimeoutStartSec=2h
Nice=10
"""

UNIT_TIMER = """[Unit]
Description=AI Briefing: weekly schedule

[Timer]
OnCalendar={on_calendar}
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
"""

UNIT_FEED = """[Unit]
Description=AI Briefing: podcast feed server

[Service]
WorkingDirectory={root}
ExecStart={bin} serve
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def cmd_install_systemd(args: argparse.Namespace) -> int:
    import getpass
    import shutil
    import subprocess
    from pathlib import Path

    cfg = load_config()
    exe = shutil.which("briefing") or str(Path(sys.executable).parent / "briefing")
    values = {"root": cfg.root, "bin": exe, "on_calendar": cfg.settings.schedule.on_calendar}
    units = {
        "ai-briefing-weekly.service": UNIT_WEEKLY.format(**values),
        "ai-briefing-weekly.timer": UNIT_TIMER.format(**values),
        "ai-briefing-feed.service": UNIT_FEED.format(**values),
    }
    if analyze := shutil.which("systemd-analyze"):
        check = subprocess.run([analyze, "calendar", values["on_calendar"]], capture_output=True, text=True)
        if check.returncode != 0:
            print(f"error: schedule.on_calendar is not valid: {check.stderr.strip()}", file=sys.stderr)
            return 2
        nxt = next((l.split(":", 1)[1].strip() for l in check.stdout.splitlines() if "Next elapse" in l), None)
        if nxt:
            print(f"schedule: {values['on_calendar']}  (next run: {nxt})")

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    if args.dry_run:
        for name, text in units.items():
            print(f"--- {unit_dir / name}\n{text}")
        return 0
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name, text in units.items():
        (unit_dir / name).write_text(text, encoding="utf-8")
        print(f"wrote {unit_dir / name}")
    user = getpass.getuser()
    print(f"""
Next, enable them:

  systemctl --user daemon-reload
  systemctl --user enable --now ai-briefing-feed.service ai-briefing-weekly.timer
  sudo loginctl enable-linger {user}     # keeps both running when you're not logged in

Check on them:

  systemctl --user list-timers ai-briefing-weekly.timer
  systemctl --user status ai-briefing-feed.service
  journalctl --user -u ai-briefing-weekly.service -n 100
""")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    outputs = run_pipeline(cfg, args.date, from_stage=args.from_stage, only=args.only, force=args.force)
    if "publish" in outputs:
        print(f"episode {args.date.isoformat()} complete")
        print(f"  show notes: {outputs['publish']['show_notes']}")
        print(f"  transcript: {outputs['publish'].get('transcript')}")
        if outputs["publish"].get("audio"):
            print(f"  audio:      {outputs['publish']['audio']}")
        if outputs["publish"].get("feed_url"):
            print(f"  feed:       {outputs['publish']['feed_url']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    for name, state in run_status(cfg, args.date):
        print(f"{name:<11} {state}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="briefing", description="Weekly AI briefing pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="validate config, sources and secrets").set_defaults(func=cmd_check)

    src = sub.add_parser("sources", help="list sources and feeds")
    src.add_argument("--probe", action="store_true", help="fetch each feed and report what it returns")
    src.set_defaults(func=cmd_sources)

    vs = sub.add_parser("voice-sample", help="render a short sample per voice to data/samples/")
    vs.add_argument("--voices", help="comma-separated voices to audition, e.g. cedar,marin,ash,coral")
    vs.add_argument("--role", choices=["lead", "counterpoint"], default="lead",
                    help="whose script line and delivery style to use with --voices (default: lead)")
    vs.set_defaults(func=cmd_voice_sample)

    sv = sub.add_parser("serve", help="serve the podcast feed and episodes on the LAN")
    sv.add_argument("--host", help="override feed.host")
    sv.add_argument("--port", type=int, help="override feed.port")
    sv.set_defaults(func=cmd_serve)

    sub.add_parser("feed", help="rebuild feed.xml (e.g. after changing feed.base_url) and list episodes").set_defaults(func=cmd_feed)

    si = sub.add_parser("install-systemd", help="install the weekly timer and feed server as user services")
    si.add_argument("--dry-run", action="store_true", help="print the unit files without writing them")
    si.set_defaults(func=cmd_install_systemd)

    sub.add_parser("llm-check", help="send one tiny structured call per model route").set_defaults(func=cmd_llm_check)

    r = sub.add_parser("run", help="run the pipeline for one episode")
    r.add_argument("--date", type=_parse_date, default=date.today(), help="episode date (default: today)")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--from", dest="from_stage", choices=STAGE_NAMES, help="rerun from this stage onward")
    g.add_argument("--only", choices=STAGE_NAMES, help="rerun just this stage")
    r.add_argument("--force", action="store_true", help="ignore existing checkpoints")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="show checkpoint status for an episode")
    s.add_argument("--date", type=_parse_date, default=date.today())
    s.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, PipelineError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
