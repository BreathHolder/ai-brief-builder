"""Orchestrator: runs stages in order with on-disk checkpoints.

Each stage writes its output to data/runs/<episode-date>/NN-<stage>.json.
A rerun skips stages that already have a checkpoint, so a failure in audio
does not re-spend money on curation and synthesis.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from briefing import audio, curate, ingest, normalise, publish, synthesise
from briefing.config import AppConfig
from briefing.context import RunContext
from briefing.logging_setup import attach_run_log, get_logger

StageFn = Callable[[RunContext, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class Stage:
    name: str
    fn: StageFn
    profile_allowed: bool = False


STAGES: list[Stage] = [
    Stage("ingest", ingest.run),
    Stage("normalise", normalise.run),
    Stage("curate", curate.run),
    Stage("synthesise", synthesise.run, profile_allowed=True),
    Stage("audio", audio.run),
    Stage("publish", publish.run),
]
STAGE_NAMES = [s.name for s in STAGES]


class PipelineError(Exception):
    pass


def checkpoint_path(run_dir: Path, index: int, name: str) -> Path:
    return run_dir / f"{index:02d}-{name}.json"


def _atomic_write_json(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_checkpoint(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)["output"]


@contextmanager
def _run_lock(run_dir: Path) -> Iterator[None]:
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_file = (run_dir / ".lock").open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise PipelineError(f"another run for {run_dir.name} is already in progress") from exc
    try:
        yield
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def _select_stages(from_stage: str | None, only: str | None) -> list[int]:
    for label, value in (("--from", from_stage), ("--only", only)):
        if value and value not in STAGE_NAMES:
            raise PipelineError(f"unknown stage for {label}: {value} (choose from {', '.join(STAGE_NAMES)})")
    if only:
        return [STAGE_NAMES.index(only)]
    start = STAGE_NAMES.index(from_stage) if from_stage else 0
    return list(range(start, len(STAGES)))


def run_pipeline(
    config: AppConfig,
    episode_date: date,
    *,
    from_stage: str | None = None,
    only: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run the pipeline and return the final stage outputs keyed by stage name."""
    logger = get_logger()
    run_dir = config.runs_dir / episode_date.isoformat()
    selected = _select_stages(from_stage, only)

    with _run_lock(run_dir):
        handler = attach_run_log(logger, run_dir / "run.log")
        base_ctx = RunContext(config=config, episode_date=episode_date, run_dir=run_dir, logger=logger)
        manifest_path = run_dir / "manifest.json"
        manifest: dict[str, Any] = (
            json.loads(manifest_path.read_text()) if manifest_path.exists() else {"run_id": episode_date.isoformat(), "stages": {}}
        )
        outputs: dict[str, Any] = {}
        try:
            for idx, stage in enumerate(STAGES):
                cp = checkpoint_path(run_dir, idx, stage.name)
                rerun = idx in selected and (force or not cp.exists() or (from_stage is not None) or (only is not None))

                if not rerun:
                    if not cp.exists():
                        raise PipelineError(
                            f"stage '{stage.name}' has no checkpoint; run it first (e.g. --from {stage.name})"
                        )
                    outputs[stage.name] = _read_checkpoint(cp)
                    if idx in selected:
                        logger.info(f"[{stage.name}] checkpoint found, skipping", extra={"stage": stage.name})
                    continue

                ctx = base_ctx.for_stage(stage.name, stage.profile_allowed)
                ctx.log("starting")
                started = time.monotonic()
                try:
                    result = stage.fn(ctx, dict(outputs))
                except Exception:
                    manifest["stages"][stage.name] = {"status": "failed", "at": _now()}
                    _atomic_write_json(manifest_path, manifest)
                    ctx.log("failed", level=logging.ERROR)
                    raise
                if not isinstance(result, dict):
                    raise PipelineError(f"stage '{stage.name}' must return a dict, got {type(result).__name__}")

                elapsed = round(time.monotonic() - started, 2)
                _atomic_write_json(cp, {"stage": stage.name, "completed_at": _now(), "output": result})
                # The memo only exists to resume a failed attempt; a completed stage drops it.
                (run_dir / f"{stage.name}.memo.json").unlink(missing_ok=True)
                manifest["stages"][stage.name] = {"status": "done", "at": _now(), "seconds": elapsed}
                _atomic_write_json(manifest_path, manifest)
                outputs[stage.name] = result
                ctx.log(f"done in {elapsed}s")

                if only:
                    break
        finally:
            logger.removeHandler(handler)
            handler.close()
    return outputs


def run_status(config: AppConfig, episode_date: date) -> list[tuple[str, str]]:
    run_dir = config.runs_dir / episode_date.isoformat()
    rows = []
    for idx, stage in enumerate(STAGES):
        cp = checkpoint_path(run_dir, idx, stage.name)
        rows.append((stage.name, "done" if cp.exists() else "pending"))
    return rows


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
