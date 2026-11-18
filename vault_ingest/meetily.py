"""Export finished Meetily recordings (its SQLite database, opened read-only) as .txt files into 00_Inbox."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote

from .sessions import hms, iso, now_local, parse_iso
from .vault import Vault, sanitize_name

log = logging.getLogger("vault_ingest")

TERMINAL_STATUSES = {"completed", "complete", "done", "success", "succeeded", "failed", "error"}
FULL_NAME_RE = re.compile(r"^.+?\s+-\s+\d{4}-\d{2}-\d{2}\s+-\s+.+$")


@contextmanager
def open_readonly(db: Path) -> Iterator[sqlite3.Connection]:
    """Read-only connection. If Meetily holds a lock, read a temporary copy instead. Never writes the database."""
    con = None
    tmp = None
    try:
        try:
            con = sqlite3.connect(f"file:{quote(db.as_posix(), safe='/:')}?mode=ro", uri=True, timeout=5)
            con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        except sqlite3.Error as e:
            if con is not None:
                con.close()
            log.debug("Meetily database busy (%s); reading a copy", e)
            tmp = Path(tempfile.mkdtemp(prefix="meetily-"))
            for suffix in ("", "-wal"):
                src = Path(str(db) + suffix)
                if src.exists():
                    shutil.copy2(src, tmp / (db.name + suffix))
            con = sqlite3.connect(str(tmp / db.name), timeout=5)
        con.row_factory = sqlite3.Row
        yield con
    finally:
        if con is not None:
            con.close()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _speaker(raw) -> str:
    s = " ".join(str(raw or "").split())
    return s[:1].upper() + s[1:] if s else ""


def transcript_lines(rows: list[sqlite3.Row]) -> list[str]:
    """One `[HH:MM:SS] Speaker: text` line per segment (timestamp/speaker left out when Meetily has none)."""
    lines = []
    for r in rows:
        text = " ".join(str(r["transcript"] or "").split())
        if not text:
            continue
        keys = r.keys()
        start = r["audio_start_time"] if "audio_start_time" in keys else None
        speaker = _speaker(r["speaker"]) if "speaker" in keys else ""
        prefix = f"[{hms(start)}] " if isinstance(start, (int, float)) else ""
        lines.append(f"{prefix}{speaker + ': ' if speaker else ''}{text}")
    return lines


def export_name(title: str, created: dt.datetime | None) -> str:
    """'<Client> - YYYY-MM-DD - <Title>' titles are kept; anything else becomes '<Title> - YYYY-MM-DD'."""
    t = sanitize_name(title or "") if (title or "").strip() else "Meetily recording"
    if FULL_NAME_RE.match(t):
        return f"{t}.txt"
    day = (created.astimezone() if created else now_local()).date().isoformat()
    return f"{t} - {day}.txt"


class MeetilyExporter:
    def __init__(self, db: Path, vault: Vault, inbox: Path, state_path: Path, settle_seconds: float = 120,
                 clock: Callable[[], float] = time.time):
        self.db = Path(db)
        self.vault = vault
        self.inbox = Path(inbox)
        self.state_path = Path(state_path)
        self.settle = settle_seconds
        self.clock = clock
        self.seen: dict[str, tuple[int, float]] = {}       # meeting id -> (segment count, unchanged since)
        self._missing_warned = False

    # ---------------------------------------------------------------- state
    def _state(self) -> dict | None:
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("%s is unreadable; not exporting until it is fixed or removed", self.state_path)
            return {"exported": {}, "baseline": [], "broken": True}
        data.setdefault("exported", {})
        data.setdefault("baseline", [])
        return data

    def _save_state(self, data: dict) -> None:
        self.vault.write(self.state_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    # ---------------------------------------------------------------- reading
    def meetings(self, con: sqlite3.Connection) -> list[dict]:
        has_status = "meeting_id" in _columns(con, "summary_processes")
        status = ("(SELECT s.status FROM summary_processes s WHERE s.meeting_id = m.id LIMIT 1)"
                  if has_status else "NULL")
        rows = con.execute(
            f"SELECT m.id, m.title, m.created_at, "
            f"(SELECT COUNT(*) FROM transcripts t WHERE t.meeting_id = m.id) AS segments, {status} AS status "
            f"FROM meetings m").fetchall()
        return [dict(r) for r in rows]

    def segments(self, con: sqlite3.Connection, meeting_id: str) -> list[sqlite3.Row]:
        cols = _columns(con, "transcripts")
        wanted = [c for c in ("transcript", "speaker", "audio_start_time", "timestamp") if c in cols]
        order = "audio_start_time IS NULL, audio_start_time, " if "audio_start_time" in cols else ""
        order += "timestamp" if "timestamp" in cols else "rowid"
        return con.execute(f"SELECT {', '.join(wanted)} FROM transcripts WHERE meeting_id = ? ORDER BY {order}",
                           (meeting_id,)).fetchall()

    # ---------------------------------------------------------------- polling
    def poll(self) -> list[Path]:
        if not self.db.is_file():
            if not self._missing_warned:
                log.warning("Meetily database not found: %s (set [meetily] database in companion.toml)", self.db)
                self._missing_warned = True
            return []
        self._missing_warned = False
        state = self._state()
        if state is not None and state.get("broken"):
            return []
        written: list[Path] = []
        with open_readonly(self.db) as con:
            meetings = self.meetings(con)
            if state is None:
                # first run: recordings made before the companion existed are not dumped into the inbox
                state = {"exported": {}, "baseline": [m["id"] for m in meetings]}
                self._save_state(state)
                if meetings:
                    log.info("Meetily: %d earlier recording(s) left as they are; new ones will be exported.",
                             len(meetings))
                return []
            done = set(state["exported"]) | set(state["baseline"])
            now = self.clock()
            for m in meetings:
                mid = str(m["id"])
                if mid in done:
                    continue
                count = int(m["segments"] or 0)
                prev = self.seen.get(mid)
                if prev is None or prev[0] != count:
                    self.seen[mid] = (count, now)
                    since = now
                else:
                    since = prev[1]
                finished = str(m.get("status") or "").strip().lower() in TERMINAL_STATUSES
                if count == 0 or not (finished or now - since >= self.settle):
                    continue
                path = self.export(con, m)
                if path is None:
                    continue
                state["exported"][mid] = {"title": m.get("title"), "file": self.vault.rel(path),
                                          "exported_at": iso(now_local())}
                self._save_state(state)
                self.seen.pop(mid, None)
                written.append(path)
        return written

    def export(self, con: sqlite3.Connection, m: dict) -> Path | None:
        lines = transcript_lines(self.segments(con, str(m["id"])))
        if not lines:
            return None
        try:
            created = parse_iso(m.get("created_at"))
        except ValueError:
            created = None
        name = export_name(str(m.get("title") or ""), created)
        dest = self.inbox / name
        n = 2
        while dest.exists():
            dest = self.inbox / f"{Path(name).stem} ({n}).txt"
            n += 1
        self.vault.write(dest, "\n".join(lines) + "\n")
        log.info("Meetily: exported '%s' (%d lines) to %s", m.get("title"), len(lines), self.vault.rel(dest))
        return dest
