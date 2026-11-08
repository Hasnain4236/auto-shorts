from datetime import date
from pathlib import Path

import pytest

from pipeline.state import ABORTED, COMPLETE, FAILED, Ledger, RunState, similarity, slugify


def test_slugify():
    assert slugify("Why is the Sky Blue?!") == "why-is-the-sky-blue"
    assert slugify("!!!") == "untitled"


def test_create_and_reload(tmp_path: Path):
    s = RunState.create(tmp_path, today=date(2026, 1, 2))
    assert s.run_id.startswith("2026-01-02_pending-")
    again = RunState.load(s.run_dir)
    assert again.data == s.data


def test_is_done_requires_outputs_on_disk(tmp_path: Path):
    s = RunState.create(tmp_path)
    out = s.run_dir / "topics.json"
    out.write_text("{}")
    s.mark_running("trends")
    s.mark_done("trends", [out])
    assert s.is_done("trends")
    out.unlink()
    assert not s.is_done("trends")  # deleted output => stage reruns


def test_failed_run_is_resumable_aborted_is_not(tmp_path: Path):
    s = RunState.create(tmp_path)
    s.mark_failed("tts", "boom")
    assert s.status == FAILED
    assert RunState.find_resumable(tmp_path, dry_run=False).run_id == s.run_id
    s.mark_aborted("factcheck", "unsupported claims")
    assert s.status == ABORTED
    assert RunState.find_resumable(tmp_path, dry_run=False) is None


def test_dry_and_real_runs_never_mix(tmp_path: Path):
    RunState.create(tmp_path, dry_run=True)
    assert RunState.find_resumable(tmp_path, dry_run=False) is None
    assert RunState.find_resumable(tmp_path, dry_run=True) is not None


def test_set_topic_renames_dir_and_keeps_outputs(tmp_path: Path):
    s = RunState.create(tmp_path, today=date(2026, 3, 4))
    out = s.run_dir / "topics.json"
    out.write_text("{}")
    s.mark_done("trends", [out])
    s.set_topic("Black holes evaporate")
    assert s.run_id == "2026-03-04_black-holes-evaporate"
    assert s.run_dir.exists() and s.is_done("trends")
    # a second run with the same topic gets a suffix
    s2 = RunState.create(tmp_path, today=date(2026, 3, 4))
    s2.set_topic("Black holes evaporate")
    assert s2.run_id.endswith("-2")


def test_count_finished_today(tmp_path: Path):
    s = RunState.create(tmp_path)
    assert RunState.count_finished_today(tmp_path, dry_run=False) == 0
    s.mark_complete()
    assert s.status == COMPLETE
    assert RunState.count_finished_today(tmp_path, dry_run=False) == 1


def test_quota(tmp_path: Path):
    s = RunState.create(tmp_path)
    s.add_quota("llm.gemini")
    s.add_quota("youtube.units", 1600)
    assert RunState.load(s.run_dir).data["quota"] == {"llm.gemini": 1, "youtube.units": 1600}


@pytest.mark.parametrize("a,b,dup", [
    ("Why is the sky blue?", "why is the sky blue", True),
    ("James Webb finds water on exoplanet", "Exoplanet water found by James Webb", True),  # same story, rephrased
    ("James Webb telescope finds water", "Webb telescope James finds water", True),  # token reorder
    ("How do vaccines work", "How do black holes form", False),
])
def test_similarity_threshold(a, b, dup):
    assert (similarity(a, b) > 0.85) is dup


def test_ledger_roundtrip(tmp_path: Path):
    path = tmp_path / "ledger.json"
    led = Ledger(path)
    assert led.find_duplicate("anything") is None
    led.append({"topic": "Why is the sky blue?", "youtube_id": "abc"})
    led2 = Ledger(path)
    assert led2.find_duplicate("why is the sky blue")["youtube_id"] == "abc"
    assert led2.find_duplicate("how magnets work") is None
