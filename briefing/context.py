"""Per-run context handed to every stage."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from briefing.config import AppConfig


class ProfileAccessError(RuntimeError):
    """Raised when a stage other than synthesis tries to read the profile.

    The environment profile shapes opinions only. It must never influence
    which stories are selected, so access is blocked everywhere else.
    """


@dataclass
class RunContext:
    config: AppConfig
    episode_date: date
    run_dir: Path
    logger: logging.Logger
    stage: str = ""
    _profile_allowed: bool = field(default=False, repr=False)

    @property
    def run_id(self) -> str:
        return self.episode_date.isoformat()

    def log(self, msg: str, level: int = logging.INFO, **data) -> None:
        self.logger.log(level, f"[{self.stage}] {msg}", extra={"stage": self.stage, "data": data or None})

    def read_profile(self) -> tuple[str, str]:
        """Return (profile_text, sha256). Only the synthesise stage may call this."""
        if not self._profile_allowed:
            raise ProfileAccessError(
                f"stage '{self.stage}' attempted to read the environment profile; "
                "only 'synthesise' may use it"
            )
        text = self.config.read_profile()
        return text, hashlib.sha256(text.encode("utf-8")).hexdigest()

    def for_stage(self, name: str, profile_allowed: bool) -> "RunContext":
        return RunContext(
            config=self.config,
            episode_date=self.episode_date,
            run_dir=self.run_dir,
            logger=self.logger,
            stage=name,
            _profile_allowed=profile_allowed,
        )
