"""End-to-end dry run through the CLI, including crash + resume."""
import importlib
from pathlib import Path

import pytest

import main
from pipeline.stages import STAGES, placeholder
from pipeline.state import ABORTED, COMPLETE, FAILED, RunState


OFFLINE_OUTPUTS = {
    "trends": {"topics.json": {"candidates": [], "selected": {"title": "Dry run placeholder topic"}}},
    "research": {"facts.json": {"facts": []}},
    "script": {"script.json": {}},
    "factcheck": {"factcheck.json": {"passed": True}},
    "tts": {"voice.mp3": "x", "timing.json": {}},
    "footage": {"footage.json": {"shots": []}},
    "captions": {"captions.ass": "x"},
    "compose": {"final.mp4": "x", "thumb.jpg": "x"},
    "qa": {"qa.json": {"passed": True}},
    "upload": {"upload.json": {"uploads": []}},
    "notify": {"notify.json": {"sent": False}},
}

# Real credentials live in .env; tests must never reach YouTube or Telegram.
SECRET_ENV = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "YOUTUBE_REFRESH_TOKEN", "YOUTUBE_CLIENT_ID",
              "YOUTUBE_CLIENT_SECRET", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"]


@pytest.fixture
def runs_dir(tmp_path: Path, monkeypatch) -> Path:
    """Isolated runs dir; real content stages (which need an LLM key) swapped for offline stand-ins."""
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)  # load_config imports it at call time
    for var in SECRET_ENV:
        monkeypatch.delenv(var, raising=False)
    for name, files in OFFLINE_OUTPUTS.items():
        module = importlib.import_module(f"pipeline.{name}")
        monkeypatch.setattr(module, "run", lambda ctx, n=name, f=files: placeholder(ctx, n, 0, f))
    return tmp_path


def test_dry_run_completes_and_renames(runs_dir: Path):
    assert main.main(["run", "--dry-run"]) == 0
    [run] = RunState.list_runs(runs_dir)
    assert run.status == COMPLETE and run.dry_run
    assert run.run_id.endswith("_dry-run-placeholder-topic")
    assert all(run.is_done(s) for s in STAGES)
    assert (run.run_dir / "run.log").exists()


def test_one_video_per_run_up_to_the_daily_cap(runs_dir: Path):
    from pipeline.config import load_config

    target = int(load_config().get("channel.videos_per_day", 1))
    for n in range(1, target + 1):
        assert main.main(["run", "--dry-run"]) == 0
        assert len(RunState.list_runs(runs_dir)) == n  # each invocation makes exactly one
    assert main.main(["run", "--dry-run"]) == 0  # cap reached: no-op
    runs = RunState.list_runs(runs_dir)
    assert len(runs) == target and all(r.status == COMPLETE for r in runs)


def test_aborted_run_gets_one_fresh_retry(runs_dir: Path, monkeypatch):
    from pipeline import research
    from pipeline.stages import AbortRun

    real = research.run
    calls = []

    def flaky(ctx):
        calls.append(ctx.state.run_id)
        if len(calls) == 1:
            raise AbortRun("only 2 verifiable facts")
        return real(ctx)

    monkeypatch.setattr(research, "run", flaky)
    assert main.main(["run", "--dry-run"]) == 0
    assert sorted(r.status for r in RunState.list_runs(runs_dir)) == sorted([ABORTED, COMPLETE])

    # two aborts in a row: give up for this slot (exit 3), the next slot tries again
    monkeypatch.setattr(research, "run", lambda ctx: (_ for _ in ()).throw(AbortRun("no topic")))
    assert main.main(["run", "--dry-run"]) == 3
    assert [r.status for r in RunState.list_runs(runs_dir)].count(ABORTED) == 3


def test_crash_then_resume_skips_finished_stages(runs_dir: Path):
    assert main.main(["run", "--dry-run", "--simulate-failure", "captions"]) == 1
    [run] = RunState.list_runs(runs_dir)
    assert run.status == FAILED
    assert run.stage_info("captions")["status"] == "failed"
    done_before = {s: run.stage_info(s)["ts"] for s in STAGES[: STAGES.index("captions")]}

    assert main.main(["run", "--dry-run"]) == 0
    [run] = RunState.list_runs(runs_dir)  # same run resumed, not a new one
    assert run.status == COMPLETE
    for s, ts in done_before.items():
        assert run.stage_info(s)["ts"] == ts, f"{s} was re-run"
    assert run.stage_info("captions")["attempts"] == 2


def test_real_run_refuses_unbuilt_stages(runs_dir: Path, monkeypatch):
    # in a real run the offline stand-ins refuse too, and nothing is faked
    assert main.main(["run"]) == 4
    [run] = RunState.list_runs(runs_dir)
    assert not run.dry_run and run.stage_info("trends")["status"] == "failed"


def test_single_stage_force(runs_dir: Path):
    main.main(["run", "--dry-run"])
    [run] = RunState.list_runs(runs_dir)
    ts = run.stage_info("qa")["ts"]
    assert main.main(["stage", "qa", "--run-id", run.run_id, "--force"]) == 0
    assert RunState.load(run.run_dir).stage_info("qa")["ts"] >= ts


def test_prune_slims_finished_and_drops_old_runs(runs_dir: Path):
    from datetime import date, timedelta

    main.main(["run", "--dry-run"])
    [done] = RunState.list_runs(runs_dir)
    (done.run_dir / "clips").mkdir()
    old = RunState.create(runs_dir, dry_run=True, today=date.today() - timedelta(days=10))
    unfinished = RunState.create(runs_dir, dry_run=True)
    (unfinished.run_dir / "clips").mkdir()
    assert main.main(["prune", "--keep-days", "3"]) == 0
    assert not old.run_dir.exists()
    assert not (done.run_dir / "clips").exists() and (done.run_dir / "state.json").exists()
    assert (unfinished.run_dir / "clips").exists()  # unfinished runs keep everything for resume
