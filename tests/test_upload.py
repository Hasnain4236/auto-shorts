from datetime import datetime, timezone

from pipeline.config import load_config
from pipeline.upload import compute_publish_at, publish_plan


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_afternoon_run_goes_live_same_evening():
    # 09:20 UTC = 14:50 IST -> 19:30 IST today = 14:00 UTC
    assert compute_publish_at(utc("2026-11-09T09:20"), "19:30", "Asia/Kolkata", 2) == utc("2026-11-09T14:00")


def test_late_run_without_review_time_goes_live_next_day():
    # 13:00 UTC = 18:30 IST: only 1 h left before 19:30 IST -> tomorrow
    assert compute_publish_at(utc("2026-11-09T13:00"), "19:30", "Asia/Kolkata", 2) == utc("2026-11-10T14:00")


def test_publish_plan_modes():
    cfg = load_config()
    cfg.raw["upload"] = {"publish_mode": "scheduled", "publish_time": "19:30", "timezone": "Asia/Kolkata",
                         "min_review_hours": 2}
    plan = publish_plan(cfg, now_utc=utc("2026-11-09T09:20"))
    assert plan == {"mode": "scheduled", "privacy": "private", "publish_at": "2026-11-09T14:00:00Z",
                    "publish_at_local": "19:30 IST, Mon 09 Nov"}
    cfg.raw["upload"] = {"publish_mode": "public"}
    assert publish_plan(cfg)["privacy"] == "public" and publish_plan(cfg)["publish_at"] is None
    cfg.raw["upload"] = {"youtube_privacy": "private"}  # old config keys still work
    assert publish_plan(cfg)["mode"] == "private"
