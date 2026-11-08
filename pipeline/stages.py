"""Stage registry, the context passed to each stage, and placeholder support."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.config import Config
from pipeline.state import RunState

if TYPE_CHECKING:
    from pipeline.llm import LLM

log = logging.getLogger(__name__)

# Order matters. Each name maps to module pipeline.<name> exposing run(ctx) -> list[Path].
STAGES: list[str] = [
    "trends",
    "research",
    "script",
    "factcheck",
    "tts",
    "footage",
    "captions",
    "compose",
    "qa",
    "upload",
    "notify",
]


class AbortRun(Exception):
    """Raised by a stage to end the run permanently (e.g. factcheck failed twice)."""


@dataclass
class StageContext:
    run_dir: Path
    config: Config
    state: RunState
    dry_run: bool = False
    _llm: "LLM | None" = field(default=None, repr=False)

    _role_llms: dict[str, "LLM"] = field(default_factory=dict, repr=False)

    @property
    def llm(self) -> "LLM":
        """Default-role LLM, built lazily so stages without LLM use never need an API key."""
        if self._llm is None:
            self._llm = self.llm_for("default")
        return self._llm

    def llm_for(self, role: str) -> "LLM":
        """Fallback chain for a job: 'writer', 'checker', 'vision' (see llm.roles in config)."""
        if self._llm is not None and role != "default" and not self._role_llms:
            return self._llm  # tests inject a single fake LLM for every role
        if role not in self._role_llms:
            from pipeline.llm import build_llm

            self._role_llms[role] = build_llm(self.config, on_call=self.state.add_quota, role=role)
        return self._role_llms[role]

    def path(self, name: str) -> Path:
        return self.run_dir / name


def placeholder(ctx: StageContext, stage: str, phase: int, files: dict[str, Any]) -> list[Path]:
    """For stages not built yet: write fake outputs in dry-run, refuse otherwise.

    Placeholders are only ever written into dry-run runs, and dry-run runs are never
    resumed by a real run, so fake output can't leak into a real upload.
    """
    if not ctx.dry_run:
        raise NotImplementedError(f"Stage '{stage}' is built in Phase {phase}. Use --dry-run for now.")
    log.info("[%s] not implemented yet (Phase %d) — writing placeholder output", stage, phase)
    outputs: list[Path] = []
    for name, content in files.items():
        p = ctx.path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (dict, list)):
            p.write_text(json.dumps({"_placeholder": True, **content} if isinstance(content, dict) else content,
                                    indent=2), encoding="utf-8")
        else:
            p.write_text(str(content), encoding="utf-8")
        outputs.append(p)
    return outputs
