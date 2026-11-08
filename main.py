"""CLI for the auto-shorts pipeline.

    python main.py run [--dry-run]            # make one video (resumes today's unfinished run first)
    python main.py resume <run_id>            # continue a specific run
    python main.py stage <name> [--run-id ID] [--force]
    python main.py learn [--force]            # pull analytics, update data/playbook.json
    python main.py list                       # show runs and stage status
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from datetime import date, timedelta

from pipeline.config import ConfigError, load_config
from pipeline.runner import run_stages
from pipeline.stages import STAGES, AbortRun
from pipeline.state import ABORTED, COMPLETE, RunState

log = logging.getLogger("auto_shorts")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("urllib3", "httpx", "httpcore", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_run(args: argparse.Namespace) -> int:
    """Make ONE video (resuming today's unfinished run first), up to channel.videos_per_day.

    CI calls this once per scheduled slot, so the day's videos are spread out. A run that
    aborts (no topic survived research or fact-checking, or QA rejected the render) gets one
    fresh retry; trends skips the topics the aborted run already tried."""
    config = load_config(args.config)
    target = int(config.get("channel.videos_per_day", 1))
    done = RunState.count_finished_today(config.runs_dir, dry_run=args.dry_run)
    if done >= target:
        log.info("Already finished %d/%d videos today — nothing to do", done, target)
        return 0
    log.info("Making video %d of %d for today", done + 1, target)
    for attempt in (1, 2):
        state = RunState.find_resumable(config.runs_dir, dry_run=args.dry_run)
        if state:
            log.info("Resuming run %s", state.run_id)
        else:
            state = RunState.create(config.runs_dir, dry_run=args.dry_run)
        try:
            run_stages(config, state, simulate_failure=args.simulate_failure)
            return 0
        except AbortRun:
            if attempt == 2:
                raise
            log.warning("Run %s aborted; trying once more with a different topic", state.run_id)
    return 0


def _find_run(config, run_id: str) -> RunState:
    run_dir = config.runs_dir / run_id
    if not (run_dir / "state.json").exists():
        raise SystemExit(f"Run not found: {run_dir}")
    return RunState.load(run_dir)


def cmd_resume(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state = _find_run(config, args.run_id)
    if state.status == COMPLETE:
        log.info("Run %s is already complete", state.run_id)
        return 0
    if state.status == ABORTED and not args.force:
        raise SystemExit(f"Run {state.run_id} was aborted ({state.data.get('abort_reason')}). Use --force to retry.")
    run_stages(config, state, simulate_failure=args.simulate_failure)
    return 0


def cmd_stage(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.run_id:
        state = _find_run(config, args.run_id)
    else:
        state = RunState.find_resumable(config.runs_dir, dry_run=args.dry_run)
        if not state:
            state = RunState.create(config.runs_dir, dry_run=args.dry_run)
    run_stages(config, state, only=args.name, force=args.force)
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Keep the runs folder small (CI caches it between days).

    Runs older than --keep-days are deleted. Finished runs keep their final video and
    JSON but drop bulky intermediates. Unfinished runs are left intact so they can resume."""
    config = load_config(args.config)
    cutoff = date.today() - timedelta(days=args.keep_days)
    removed = slimmed = 0
    for r in RunState.list_runs(config.runs_dir):
        if date.fromisoformat(r.data["date"]) < cutoff:
            shutil.rmtree(r.run_dir, ignore_errors=True)
            removed += 1
        elif r.status in (COMPLETE, ABORTED):
            for heavy in ("clips", "compose_parts", "tts_parts", "fonts"):
                if (r.run_dir / heavy).exists():
                    shutil.rmtree(r.run_dir / heavy, ignore_errors=True)
                    slimmed += 1
    stale = 0
    cache = config.runs_dir / "_cache" / "footage"
    if cache.exists():
        limit = time.time() - args.keep_days * 86400
        for f in cache.iterdir():
            if f.is_file() and f.stat().st_mtime < limit:
                f.unlink(missing_ok=True)
                stale += 1
    log.info("prune: removed %d old runs, slimmed %d folders, dropped %d cached clips", removed, slimmed, stale)
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    from pipeline.learn import run_learning
    from pipeline.llm import build_llm

    config = load_config(args.config)
    run_learning(config, llm_factory=lambda: build_llm(config), force=args.force)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    runs = RunState.list_runs(config.runs_dir)
    if not runs:
        print(f"No runs in {config.runs_dir}")
        return 0
    for r in runs:
        marks = "".join(
            {"done": "✓", "failed": "✗", "running": "…", "aborted": "!"}.get(r.stage_info(s).get("status", ""), "·")
            for s in STAGES
        )
        flag = " [dry]" if r.dry_run else ""
        print(f"{r.run_id:<50} {r.status:<12} {marks}{flag}")
    print(f"\nstages: {' '.join(STAGES)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="auto-shorts", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None, help="path to config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--dry-run", action="store_true", help="skip uploads/notifications")
        sp.add_argument("--simulate-failure", metavar="STAGE", choices=STAGES, help=argparse.SUPPRESS)

    r = sub.add_parser("run", help="run today's pipeline (resumes if unfinished)")
    common(r)
    r.set_defaults(func=cmd_run)

    rs = sub.add_parser("resume", help="resume a specific run")
    rs.add_argument("run_id")
    rs.add_argument("--force", action="store_true", help="retry even if the run was aborted")
    rs.add_argument("--simulate-failure", metavar="STAGE", choices=STAGES, help=argparse.SUPPRESS)
    rs.set_defaults(func=cmd_resume)

    st = sub.add_parser("stage", help="run a single stage")
    st.add_argument("name", choices=STAGES)
    st.add_argument("--run-id", default=None)
    st.add_argument("--force", action="store_true", help="rerun even if already done")
    st.add_argument("--dry-run", action="store_true")
    st.set_defaults(func=cmd_stage)

    pr = sub.add_parser("prune", help="delete old runs and bulky intermediates")
    pr.add_argument("--keep-days", type=int, default=3)
    pr.set_defaults(func=cmd_prune)

    ln = sub.add_parser("learn", help="pull YouTube analytics and update the playbook")
    ln.add_argument("--force", action="store_true", help="pull again even if it ran in the last learning.every_hours")
    ln.set_defaults(func=cmd_learn)

    ls = sub.add_parser("list", help="list runs")
    ls.set_defaults(func=cmd_list)
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except ConfigError as e:
        log.error("Config error: %s", e)
        return 2
    except AbortRun:
        return 3
    except NotImplementedError as e:
        log.error("%s", e)
        return 4
    except Exception as e:
        log.error("Run failed: %s — rerun the same command to resume from this stage", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
