"""End-to-end dry run through the CLI, including crash + resume."""
import importlib
from pathlib import Path

import pytest

import main
from pipeline.stages import STAGES, placeholder
from pipeline.state import COMPLETE, FAILED, RunState


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
    # second invocation is a no-op: today's quota of videos is done
    assert main.main(["run", "--dry-run"]) == 0
    assert len(RunState.list_runs(runs_dir)) == 1


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
