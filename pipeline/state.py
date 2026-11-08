"""Run directories, state.json, stage-skip logic, and the uploaded-topics ledger.

Layout:  <runs_dir>/<YYYY-MM-DD>_<slug>/state.json
A run starts as  <date>_pending-<id>  and is renamed to  <date>_<slug>  once the
trends stage has picked a topic. All output paths in state.json are relative to
the run dir, so renaming never breaks them.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STATE_FILE = "state.json"
PENDING_PREFIX = "pending-"

# Run statuses
IN_PROGRESS = "in_progress"
COMPLETE = "complete"
FAILED = "failed"      # resumable: rerun picks up at the failed stage
ABORTED = "aborted"    # terminal: e.g. factcheck gave up; never auto-resumed


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str, max_len: int = 50) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "untitled"


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON via temp file + rename so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class RunState:
    def __init__(self, run_dir: Path, data: dict[str, Any]):
        self.run_dir = run_dir
        self.data = data

    # ---- construction -------------------------------------------------
    @classmethod
    def create(cls, runs_dir: Path, *, dry_run: bool = False, today: date | None = None) -> "RunState":
        today = today or date.today()
        run_id = f"{today.isoformat()}_{PENDING_PREFIX}{uuid.uuid4().hex[:6]}"
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        state = cls(run_dir, {
            "run_id": run_id,
            "date": today.isoformat(),
            "status": IN_PROGRESS,
            "dry_run": dry_run,
            "created": utcnow(),
            "updated": utcnow(),
            "topic": None,
            "stages": {},
            "quota": {},
        })
        state.save()
        log.info("Created run %s", run_id)
        return state

    @classmethod
    def load(cls, run_dir: Path) -> "RunState":
        path = run_dir / STATE_FILE
        if not path.exists():
            raise FileNotFoundError(f"No {STATE_FILE} in {run_dir}")
        return cls(run_dir, read_json(path))

    @classmethod
    def list_runs(cls, runs_dir: Path) -> list["RunState"]:
        if not runs_dir.exists():
            return []
        runs = [cls.load(p) for p in sorted(runs_dir.iterdir()) if (p / STATE_FILE).exists()]
        return sorted(runs, key=lambda r: r.data.get("created", ""))

    @classmethod
    def find_resumable(cls, runs_dir: Path, *, dry_run: bool, today: date | None = None) -> "RunState | None":
        """Most recent unfinished run for today with the same dry-run flag."""
        today_s = (today or date.today()).isoformat()
        candidates = [
            r for r in cls.list_runs(runs_dir)
            if r.data.get("date") == today_s
            and r.status in (IN_PROGRESS, FAILED)
            and bool(r.data.get("dry_run")) == dry_run
        ]
        return candidates[-1] if candidates else None

    @classmethod
    def count_finished_today(cls, runs_dir: Path, *, dry_run: bool, today: date | None = None) -> int:
        today_s = (today or date.today()).isoformat()
        return sum(
            1 for r in cls.list_runs(runs_dir)
            if r.data.get("date") == today_s and r.status == COMPLETE
            and bool(r.data.get("dry_run")) == dry_run
        )

    # ---- properties ---------------------------------------------------
    @property
    def run_id(self) -> str:
        return self.data["run_id"]

    @property
    def status(self) -> str:
        return self.data["status"]

    @property
    def dry_run(self) -> bool:
        return bool(self.data.get("dry_run"))

    @property
    def is_pending_name(self) -> bool:
        return PENDING_PREFIX in self.run_id

    # ---- persistence --------------------------------------------------
    def save(self) -> None:
        self.data["updated"] = utcnow()
        atomic_write_json(self.run_dir / STATE_FILE, self.data)

    # ---- stage bookkeeping --------------------------------------------
    def stage_info(self, stage: str) -> dict[str, Any]:
        return self.data["stages"].get(stage, {})

    def is_done(self, stage: str) -> bool:
        """Done only if marked done AND every recorded output still exists on disk."""
        info = self.stage_info(stage)
        if info.get("status") != "done":
            return False
        missing = [o for o in info.get("outputs", []) if not (self.run_dir / o).exists()]
        if missing:
            log.warning("Stage %s marked done but outputs missing (%s); will rerun", stage, missing)
            return False
        return True

    def mark_running(self, stage: str) -> None:
        info = self.data["stages"].setdefault(stage, {})
        info.update(status="running", started=utcnow(), attempts=info.get("attempts", 0) + 1)
        info.pop("error", None)
        self.data["status"] = IN_PROGRESS
        self.save()

    def mark_done(self, stage: str, outputs: list[Path]) -> None:
        rel = [str(Path(o).resolve().relative_to(self.run_dir.resolve()).as_posix()) for o in outputs]
        info = self.data["stages"].setdefault(stage, {})
        info.update(status="done", outputs=rel, ts=utcnow())
        self.save()

    def mark_failed(self, stage: str, error: str) -> None:
        self.data["stages"].setdefault(stage, {}).update(status="failed", error=error, ts=utcnow())
        self.data["status"] = FAILED
        self.save()

    def mark_aborted(self, stage: str, reason: str) -> None:
        self.data["stages"].setdefault(stage, {}).update(status="aborted", error=reason, ts=utcnow())
        self.data["status"] = ABORTED
        self.data["abort_reason"] = reason
        self.save()

    def mark_complete(self) -> None:
        self.data["status"] = COMPLETE
        self.save()

    def reset_stage(self, stage: str) -> None:
        self.data["stages"].pop(stage, None)
        self.save()

    # ---- quota accounting ---------------------------------------------
    def add_quota(self, key: str, amount: int = 1) -> None:
        q = self.data.setdefault("quota", {})
        q[key] = q.get(key, 0) + amount
        self.save()

    # ---- naming -------------------------------------------------------
    def set_topic(self, topic: str) -> None:
        """Record the chosen topic and rename the run dir from pending-* to <date>_<slug>."""
        self.data["topic"] = topic
        if self.is_pending_name:
            base = f"{self.data['date']}_{slugify(topic)}"
            new_dir = self.run_dir.parent / base
            n = 2
            while new_dir.exists():
                new_dir = self.run_dir.parent / f"{base}-{n}"
                n += 1
            os.replace(self.run_dir, new_dir)
            log.info("Renamed run %s -> %s", self.run_id, new_dir.name)
            self.run_dir = new_dir
            self.data["run_id"] = new_dir.name
        self.save()


# ---------------------------------------------------------------------------
# Uploaded-topics ledger (committed to git so CI remembers across runs)
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def similarity(a: str, b: str) -> float:
    """Max of plain and token-sorted ratio, so word-order changes still match."""
    na, nb = _normalise(a), _normalise(b)
    plain = difflib.SequenceMatcher(None, na, nb).ratio()
    sa, sb = " ".join(sorted(na.split())), " ".join(sorted(nb.split()))
    return max(plain, difflib.SequenceMatcher(None, sa, sb).ratio())


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.entries: list[dict[str, Any]] = read_json(path) if path.exists() else []

    def find_duplicate(self, topic: str, threshold: float = 0.85) -> dict[str, Any] | None:
        best: tuple[float, dict[str, Any] | None] = (0.0, None)
        for entry in self.entries:
            score = similarity(topic, entry.get("topic", ""))
            if score > best[0]:
                best = (score, entry)
        return best[1] if best[0] > threshold else None

    def save(self) -> None:
        atomic_write_json(self.path, self.entries)

    def append(self, entry: dict[str, Any]) -> None:
        entry = {"ts": utcnow(), **entry}
        self.entries.append(entry)
        atomic_write_json(self.path, self.entries)
