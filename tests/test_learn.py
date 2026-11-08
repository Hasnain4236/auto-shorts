import random

from pipeline import learn
from pipeline.config import load_config
from pipeline.state import atomic_write_json


def _cfg(tmp_path):
    cfg = load_config()
    cfg.ledger_path = tmp_path / "uploaded_ledger.json"
    return cfg


def test_choose_arm_explores_untried_then_exploits(tmp_path):
    cfg = _cfg(tmp_path)
    rng = random.Random(0)
    assert learn.choose_arm(cfg, "hook_type", learn.HOOK_TYPES, rng) in learn.HOOK_TYPES  # no data: explore
    atomic_write_json(tmp_path / "playbook.json", {"arms": {"hook_type": {
        "counterintuitive": {"n": 5, "mean": 0.8}, "stakes": {"n": 5, "mean": 0.3}, "question": {"n": 1, "mean": 0.9}}}})
    assert learn.choose_arm(cfg, "hook_type", learn.HOOK_TYPES, rng) == "question"  # under-sampled arm first
    pb = learn.load_playbook(cfg)
    pb["arms"]["hook_type"]["question"]["n"] = 5
    atomic_write_json(tmp_path / "playbook.json", pb)
    cfg.raw.setdefault("learning", {})["explore_rate"] = 0.0
    assert learn.choose_arm(cfg, "hook_type", learn.HOOK_TYPES, rng) == "question"  # best mean


def test_retention_summary_and_scoring():
    curve = [[i / 20, 1.0 - (0.4 if i >= 2 else 0) - i * 0.01] for i in range(21)]
    s = learn.summarise_retention(curve)
    assert s["biggest_drop_at"] == 0.1 and s["hook_hold"] > s["end_hold"]
    rows = [{"averageViewPercentage": 90, "views": 1000, "hook_hold": 0.8, "features": {"hook_type": "stakes"}},
            {"averageViewPercentage": 40, "views": 50, "hook_hold": 0.4, "features": {"hook_type": "question"}}]
    learn.score_videos(rows)
    assert rows[0]["score"] > rows[1]["score"]
    arms = learn.arm_stats(rows, "hook_type")
    assert arms["stakes"]["mean"] > arms["question"]["mean"]


def test_manual_reupload_is_linked_by_title():
    entries = [{"title": "Why the sky isn't violet", "youtube_id": "PRIV1"}]
    uploads = [{"id": "PUB9", "title": "Why the sky isn't violet #shorts", "published": "2026-11-01T00:00:00Z"}]
    learn.link_videos(entries, uploads, {"PRIV1": "private", "PUB9": "public"})
    assert entries[0]["live_id"] == "PUB9"


def test_playbook_prompt_empty_without_data(tmp_path):
    assert learn.playbook_prompt(_cfg(tmp_path), "script") == ""


def test_learn_runs_once_a_day(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    cfg = _cfg(tmp_path)
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    assert learn.learned_recently(cfg, now) is None  # never pulled
    atomic_write_json(tmp_path / "performance.json", {"updated": (now - timedelta(hours=5)).isoformat()})
    assert learn.learned_recently(cfg, now)  # pulled 5 h ago: later runs today skip it
    assert learn.learned_recently(cfg, now + timedelta(hours=16)) is None  # next day's first run pulls again

    # a skipped pull never touches the YouTube APIs
    atomic_write_json(tmp_path / "performance.json",
                      {"updated": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")})
    monkeypatch.setattr("pipeline.youtube_api.data_api", lambda: (_ for _ in ()).throw(AssertionError("called")))
    assert learn.run_learning(cfg) == {}
