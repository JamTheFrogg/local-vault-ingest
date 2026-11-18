"""Copy new screenshots into _System/screenshots and attach them to the meeting they were taken in."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from .sessions import Session, SessionStore, hms, iso, session_for_time, to_local
from .vault import Vault

log = logging.getLogger("vault_ingest")


def created_ts(st: os.stat_result) -> float:
    """File creation time (st_birthtime on Python 3.12+, st_ctime is creation time on older Windows Pythons)."""
    return getattr(st, "st_birthtime", None) or st.st_ctime


def _readable(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


class ScreenshotWatcher:
    """Polled from the companion loop. A file is taken once its size is unchanged between two polls."""

    def __init__(self, folder: Path, dest_root: Path, vault: Vault, store: SessionStore,
                 sessions: Callable[[], Iterable[Session]], extensions: Iterable[str] = (".png", ".jpg", ".jpeg"),
                 attach_grace_seconds: float = 120, catch_up_minutes: float = 10,
                 clock: Callable[[], float] = time.time):
        self.folder = Path(folder)
        self.dest_root = Path(dest_root)
        self.vault = vault
        self.store = store
        self.sessions = sessions
        self.extensions = {e.lower() for e in extensions}
        self.grace = attach_grace_seconds
        self.catch_up = catch_up_minutes * 60
        self.clock = clock
        self.index_path = self.dest_root / "index.json"
        self.known: set[str] = set()
        self.pending: dict[str, int] = {}
        self._missing_warned = False
        self.lock = threading.Lock()          # the popup's Record button files captures from another thread

    # ---------------------------------------------------------------- index
    def _read_index(self) -> dict:
        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("screenshots"), list):
                    return data
            except ValueError:
                log.warning("Screenshot index %s is unreadable; starting a new list", self.index_path)
        return {"screenshots": []}

    def _append_index(self, entry: dict) -> None:
        data = self._read_index()
        data["screenshots"].append(entry)
        self.vault.write(self.index_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    # ---------------------------------------------------------------- scanning
    def _candidates(self) -> list[os.DirEntry]:
        if not self.folder.is_dir():
            if not self._missing_warned:
                log.warning("Screenshot folder not found: %s (set [screenshots] folder in companion.toml)", self.folder)
                self._missing_warned = True
            return []
        self._missing_warned = False
        return [e for e in os.scandir(self.folder)
                if e.is_file() and Path(e.name).suffix.lower() in self.extensions and not e.name.startswith(".")]

    def prime(self) -> None:
        """Everything already in the folder is old, except snips from the last few minutes not copied yet."""
        copied = {str(e.get("source", "")).lower() for e in self._read_index()["screenshots"]}
        # Windows file times can lead time.time() by a fraction of a millisecond, so a file
        # created at this instant would otherwise look newer than the cutoff.
        cutoff = self.clock() - self.catch_up + 1
        for e in self._candidates():
            key = os.path.normcase(e.path)
            if created_ts(e.stat()) < cutoff or e.path.lower() in copied:
                self.known.add(key)

    def poll(self) -> list[dict]:
        done = []
        for e in self._candidates():
            key = os.path.normcase(e.path)
            if key in self.known:
                continue
            size = e.stat().st_size
            if size == 0 or self.pending.get(key) != size:
                self.pending[key] = size
                continue
            path = Path(e.path)
            if not _readable(path):
                continue
            self.pending.pop(key, None)
            self.known.add(key)
            try:
                done.append(self.ingest(path))
            except Exception:  # noqa: BLE001  one bad file must not stop the companion
                log.exception("Could not copy screenshot %s", path.name)
        return done

    # ---------------------------------------------------------------- copy + assign
    def add_capture(self, path: Path, kind: str = "participants") -> dict:
        """A screenshot the companion took itself (Record button, tray menu)."""
        return self.ingest(Path(path), kind=kind)

    def ingest(self, path: Path, kind: str = "") -> dict:
        with self.lock:
            return self._ingest(path, kind)

    def _ingest(self, path: Path, kind: str = "") -> dict:
        created = to_local(created_ts(path.stat()))
        stem = f"{created:%Y-%m-%d %H-%M-%S}" + (f" {kind}" if kind else "")
        dest = self.dest_root / f"{created:%Y-%m-%d}" / f"{stem}{path.suffix.lower()}"
        final = self.vault.copy(path, dest)
        entry = {"path": self.vault.rel(final), "source": str(path), "timestamp": iso(created)}
        if kind:
            entry["kind"] = kind
        session = session_for_time(self.sessions(), created, self.grace)
        if session:
            entry["offset"] = hms((created - session.start).total_seconds())
            self.store.add_screenshot(session, dict(entry))
            log.info("Screenshot %s -> %s (meeting %s, %s in)", path.name, entry["path"], session.app, entry["offset"])
        else:
            log.info("Screenshot %s -> %s (no meeting)", path.name, entry["path"])
        self._append_index({**entry, "session": session.id if session else None})
        return {**entry, "session": session.id if session else None}
