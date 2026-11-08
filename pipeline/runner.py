"""Drives stages in order, skipping completed ones, recording state after each."""
from __future__ import annotations

import importlib
import logging
import traceback
from pathlib import Path

from pipeline.config import Config
from pipeline.stages import STAGES, AbortRun, StageContext
from pipeline.state import RunState, read_json

log = logging.getLogger(__name__)

_run_log_handler: logging.Handler | None = None


def _attach_run_log(run_dir: Path) -> None:
    global _run_log_handler
    _detach_run_log()
    h = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(h)
    _run_log_handler = h


def _detach_run_log() -> None:
    """Close the run.log handle (Windows can't rename a dir with an open file in it)."""
    global _run_log_handler
    if _run_log_handler is not None:
        logging.getLogger().removeHandler(_run_log_handler)
        _run_log_handler.close()
        _run_log_handler = None


def _name_run_after_topic(state: RunState) -> None:
    """Name the run after the final topic (research may move past trends' first pick)."""
    topics_path = state.run_dir / "topics.json"
    if state.data.get("topic") or not topics_path.exists():
        return
    selected = read_json(topics_path).get("selected") or {}
    # the angle ("why does X happen?") names the video; the raw trend title may be a song or trailer
    title = selected.get("angle") or selected.get("title")
    if title:
        _detach_run_log()
        state.set_topic(title)
        _attach_run_log(state.run_dir)


def _send_alert(config: Config, state: RunState, message: str) -> None:
    try:
        from pipeline import notify

        notify.send_alert(config, state, message)
    except NotImplementedError:
        log.warning("Alert not sent (notify not built yet): %s", message)
    except Exception:  # alerting must never mask the original failure
        log.exception("Failed to send alert")


def run_stages(
    config: Config,
    state: RunState,
    *,
    only: str | None = None,
    force: bool = False,
    simulate_failure: str | None = None,
) -> RunState:
    """Run all remaining stages (or just `only`). Returns the (possibly renamed) state."""
    _attach_run_log(state.run_dir)
    stages = [only] if only else STAGES
    try:
        for name in stages:
            if force and only:
                state.reset_stage(name)
            if state.is_done(name):
                log.info("[%s] already done — skipping", name)
                continue

            log.info("[%s] starting", name)
            state.mark_running(name)
            ctx = StageContext(run_dir=state.run_dir, config=config, state=state, dry_run=state.dry_run)
            try:
                if simulate_failure == name:
                    raise RuntimeError(f"Simulated failure in stage '{name}'")
                module = importlib.import_module(f"pipeline.{name}")
                outputs = module.run(ctx)
            except AbortRun as e:
                log.error("[%s] aborted run: %s", name, e)
                state.mark_aborted(name, str(e))
                _send_alert(config, state, f"Run {state.run_id} aborted at {name}: {e}")
                raise
            except Exception as e:
                log.error("[%s] failed: %s", name, e)
                log.debug(traceback.format_exc())
                state.mark_failed(name, f"{type(e).__name__}: {e}")
                if not state.dry_run:
                    _send_alert(config, state, f"Run {state.run_id} failed at {name}: {type(e).__name__}: {e}. "
                                               "It will resume from this stage on the next run.")
                raise
            state.mark_done(name, outputs)
            log.info("[%s] done -> %s", name, ", ".join(state.stage_info(name)["outputs"]))

            if name == "research":
                _name_run_after_topic(state)

        if not only and all(state.is_done(s) for s in STAGES):
            state.mark_complete()
            log.info("Run %s complete. Quota used: %s", state.run_id, state.data.get("quota") or "none")
    finally:
        _detach_run_log()
    return state
