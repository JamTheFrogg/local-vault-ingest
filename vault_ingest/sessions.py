"""Meeting sessions in _System/meetings/*.json, shared by the companion (writes) and the ingest pipeline (reads)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .vault import Vault, sanitize_name

log = logging.getLogger("vault_ingest")


# ------------------------------------------------------------------ time helpers
def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone().replace(microsecond=0)


def to_local(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts).astimezone().replace(microsecond=0)


def iso(d: dt.datetime | None) -> str | None:
    return d.isoformat(timespec="seconds") if d else None


def parse_iso(s) -> dt.datetime | None:
    if not s:
        return None
    d = dt.datetime.fromisoformat(str(s))
    return d if d.tzinfo else d.astimezone()      # naive -> this PC's local time


def hms(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def hms_seconds(text: str | None) -> int | None:
    if not text:
        return None
    try:
        parts = [int(p) for p in str(text).split(":")]
    except ValueError:
        return None
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return h * 3600 + m * 60 + s


# ------------------------------------------------------------------ session
@dataclass
class Session:
    id: str
    app: str
    window_title: str
    start: dt.datetime
    end: dt.datetime | None = None
    recorded_prompt_clicked: bool = False
    screenshots: list[dict] = field(default_factory=list)
    process: str = ""
    last_seen: dt.datetime | None = None
    path: Path | None = None
    client: str | None = None             # chosen in the client picker; unset until Enter
    call_title: str | None = None
    skip: bool = False                    # "Don't ingest this meeting"
    meeting_key: str | None = None        # recurring-meeting key, for meeting-map.json
    processed_client: str | None = None   # client the call was actually filed under

    @property
    def effective_end(self) -> dt.datetime:
        return self.end or self.last_seen or self.start

    @property
    def duration(self) -> int | None:
        return int((self.end - self.start).total_seconds()) if self.end else None

    def to_json(self) -> dict:
        return {
            "id": self.id, "app": self.app, "window_title": self.window_title, "process": self.process,
            "start": iso(self.start), "end": iso(self.end), "duration": self.duration,
            "recorded_prompt_clicked": self.recorded_prompt_clicked, "screenshots": self.screenshots,
            "last_seen": iso(self.last_seen), "client": self.client, "call_title": self.call_title,
            "skip": self.skip, "meeting_key": self.meeting_key, "processed_client": self.processed_client,
        }

    @classmethod
    def from_json(cls, data: dict, path: Path | None = None) -> "Session":
        return cls(id=str(data.get("id") or uuid.uuid4().hex[:12]), app=str(data.get("app") or "Meeting"),
                   window_title=str(data.get("window_title") or ""), start=parse_iso(data["start"]),
                   end=parse_iso(data.get("end")), recorded_prompt_clicked=bool(data.get("recorded_prompt_clicked")),
                   screenshots=list(data.get("screenshots") or []), process=str(data.get("process") or ""),
                   last_seen=parse_iso(data.get("last_seen")), path=path,
                   client=data.get("client") or None, call_title=data.get("call_title") or None,
                   skip=bool(data.get("skip")), meeting_key=data.get("meeting_key") or None,
                   processed_client=data.get("processed_client") or None)


class SessionStore:
    """Reads and writes session files. Writes go through `Vault` (so only inside _System) and are serialized."""

    def __init__(self, directory: Path, vault: Vault | None = None):
        self.dir = Path(directory)
        self.vault = vault
        self.lock = threading.RLock()

    def path_for(self, s: Session) -> Path:
        return self.dir / f"{s.start:%Y-%m-%d %H-%M-%S} {sanitize_name(s.app)}.json"

    def save(self, s: Session) -> Path:
        if self.vault is None:
            raise RuntimeError("SessionStore is read-only")
        with self.lock:
            s.path = s.path or self.path_for(s)
            self.vault.write(s.path, json.dumps(s.to_json(), ensure_ascii=False, indent=2) + "\n")
            return s.path

    def start(self, app: str, title: str, process: str, when: dt.datetime,
              meeting_key: str | None = None) -> Session:
        s = Session(id=f"{when:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}", app=app, window_title=title,
                    start=when, process=process, last_seen=when, meeting_key=meeting_key)
        self.save(s)
        return s

    def find(self, sid: str) -> Session | None:
        for s in self.load():
            if s.id == sid:
                return s
        return None

    def update(self, s: Session, **changes) -> None:
        with self.lock:
            for k, v in changes.items():
                setattr(s, k, v)
            self.save(s)

    def add_screenshot(self, s: Session, entry: dict) -> None:
        with self.lock:
            s.screenshots.append(entry)
            self.save(s)

    def load(self, dates: Iterable[str] | None = None) -> list[Session]:
        """Sessions whose file name starts with one of `dates` (YYYY-MM-DD), or all of them."""
        if not self.dir.is_dir():
            return []
        prefixes = tuple(dates) if dates is not None else None
        out = []
        for p in sorted(self.dir.glob("*.json")):
            if prefixes is not None and not p.name.startswith(prefixes):
                continue
            try:
                out.append(Session.from_json(json.loads(p.read_text(encoding="utf-8")), p))
            except (ValueError, KeyError, TypeError) as e:
                log.warning("Skipping unreadable meeting session %s: %s", p.name, e)
        return out

    def close_stale(self, sessions: list[Session]) -> int:
        """Sessions left open by a crash/shutdown end when they were last seen."""
        n = 0
        for s in sessions:
            if s.end is None:
                self.update(s, end=s.last_seen or s.start)
                n += 1
        return n


# ------------------------------------------------------------------ matching
def overlaps_skipped(sessions: Iterable[Session], when: dt.datetime, grace_seconds: float = 15 * 60) -> bool:
    """True when `when` falls in a meeting the picker marked don't-ingest."""
    grace = dt.timedelta(seconds=grace_seconds)
    for s in sessions:
        if not s.skip:
            continue
        if s.start - grace <= when <= s.effective_end + grace:
            return True
    return False


def session_for_time(sessions: Iterable[Session], t: dt.datetime, grace_seconds: float) -> Session | None:
    """The meeting a screenshot taken at `t` belongs to: active, or ended less than `grace_seconds` before."""
    best = None
    for s in sessions:
        if s.start > t:
            continue
        if s.end is not None and t > s.end + dt.timedelta(seconds=grace_seconds):
            continue
        if best is None or s.start > best.start:
            best = s
    return best


def transcript_windows(created: dt.datetime, modified: dt.datetime,
                       duration_seconds: float) -> list[tuple[dt.datetime, dt.datetime]]:
    """Where the call probably happened: starting at the file's creation time, or ending at its modified time."""
    d = dt.timedelta(seconds=max(0.0, duration_seconds))
    return [(created, created + d), (modified - d, modified)]


def _overlap(a0: dt.datetime, a1: dt.datetime, b0: dt.datetime, b1: dt.datetime) -> float | None:
    """Seconds of overlap, 0.0 when they only touch, None when they are apart."""
    lo, hi = max(a0, b0), min(a1, b1)
    return (hi - lo).total_seconds() if lo <= hi else None


def match_session(sessions: Iterable[Session], date: str, windows: list[tuple[dt.datetime, dt.datetime]],
                  tolerance_seconds: float) -> Session | None:
    """Session on `date` overlapping a transcript window (± tolerance); several -> the biggest overlap."""
    tol = dt.timedelta(seconds=tolerance_seconds)
    best, best_score = None, -1.0
    for s in sessions:
        end = s.effective_end
        if date not in (s.start.date().isoformat(), end.date().isoformat()):
            continue
        scores = [o for w0, w1 in windows if (o := _overlap(s.start, end, w0 - tol, w1 + tol)) is not None]
        if scores and max(scores) > best_score:
            best, best_score = s, max(scores)
    return best
