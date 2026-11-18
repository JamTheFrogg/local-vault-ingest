"""Copy the newest phone recording into 00_Inbox when the phone is plugged in.

The phone is read-only. "Already ingested" is a SHA-256 ledger in this tool's
state folder, not a guess from the file name. The meeting companion owns the
picker; this module decides and names the file.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .sessions import hms_seconds
from .vault import get_frontmatter, sanitize_name, sha256_file

log = logging.getLogger("vault_ingest")

PHONE_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".amr", ".3ga", ".ogg", ".opus"}
PHONE_SKIP_LABEL = "Not a meeting / skip"
ALREADY_TOAST = "Latest recording already in vault"
CHARGING_TOAST = "Unlock the phone and choose File transfer"
RETRY_SECONDS = 15

# YYYY-MM-DD, then YYYYMMDD, then Samsung's YYMMDD_HHMMSS
_ISO_DATE = re.compile(r"(?P<y>19\d{2}|20\d{2})-(?P<m>\d{2})-(?P<d>\d{2})")
_YMD = re.compile(r"(?<!\d)(?P<y>19\d{2}|20\d{2})(?P<m>\d{2})(?P<d>\d{2})(?!\d)")
_VOICE_STAMP = re.compile(r"(?<!\d)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})_(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})(?!\d)")


class PickerNotReady(RuntimeError):
    pass


@dataclass(frozen=True)
class PhoneRecording:
    name: str
    phone_path: str
    size: int
    mtime: dt.datetime
    duration: str = ""


@dataclass(frozen=True)
class Decision:
    kind: str  # idle | charging | empty | waiting | known | copy
    recording: PhoneRecording | None = None
    toast: str = ""
    connected: bool = False
    disconnected: bool = False


def aware(when: dt.datetime) -> dt.datetime:
    """Naive values are this PC's local time. Everything else converts to local."""
    if when.tzinfo is None:
        return when.astimezone()
    return when.astimezone()


def mtime_key(when: dt.datetime) -> str:
    return aware(when).replace(microsecond=0).isoformat(timespec="seconds")


def format_length(text: str) -> str:
    """`00:01:58` -> `1:58`, `01:00:00` -> `1:00:00`."""
    total = hms_seconds(text)
    if total is None:
        return ""
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def file_duration(path: Path) -> str:
    """Length from the local copy. Empty when PyAV cannot read the file."""
    try:
        import av
    except ImportError:
        return ""
    try:
        with av.open(str(path), metadata_errors="ignore") as container:
            if not container.duration:
                return ""
            seconds = int(round(container.duration / 1_000_000))
    except Exception:  # noqa: BLE001  a missing length still lets the picker open
        return ""
    return format_length(f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}")


def _date(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def recording_day(name: str, mtime: dt.datetime) -> dt.date:
    """The day the recording was made: a date in the file name, otherwise the local mtime."""
    stem = Path(name).stem
    for pattern, century in ((_ISO_DATE, False), (_YMD, False)):
        match = pattern.search(stem)
        if match:
            found = _date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
            if found:
                return found
    match = _VOICE_STAMP.search(stem)
    if match:
        year = int(match.group("y"))
        year += 2000 if year < 70 else 1900
        found = _date(year, int(match.group("m")), int(match.group("d")))
        if found:
            return found
    return aware(mtime).date()


def inbox_filename(client: str, day: dt.date, title: str | None, stem: str, ext: str) -> str:
    """`<Client> - YYYY-MM-DD - <Title>.<ext>`. An empty title keeps the recorder's file name."""
    client_name = sanitize_name(client)
    typed = sanitize_name(title.strip()) if title and title.strip() else ""
    if not typed or typed == "Untitled":
        typed = sanitize_name(stem) or "Recording"
    suffix = ext.lower() if ext.startswith(".") else f".{ext.lower()}"
    return f"{client_name} - {day.isoformat()} - {typed}{suffix}"


def picker_heading(rec: PhoneRecording, length: str = "") -> str:
    local = aware(rec.mtime)
    text = f"{rec.name} · {local.strftime('%Y-%m-%d %H:%M')}"
    shown = format_length(length or rec.duration)
    if shown:
        text += f" · {shown}"
    return text


def excluded_path(phone_path: str) -> bool:
    """Call recordings stay on the phone. `Recordings/Call` is never a voice memo."""
    return any(part.lower() == "call" for part in phone_path.replace("\\", "/").split("/"))


def newest_recording(files: list[PhoneRecording]) -> PhoneRecording | None:
    chosen: PhoneRecording | None = None
    for rec in files:
        if Path(rec.name).suffix.lower() not in PHONE_EXTENSIONS:
            continue
        if excluded_path(rec.phone_path):
            continue
        if chosen is None or rec.mtime > chosen.mtime or (rec.mtime == chosen.mtime and rec.name > chosen.name):
            chosen = rec
    return chosen


def recording_in_progress(mtime: dt.datetime, now: dt.datetime, previous_size: int | None, size: int,
                          settle_seconds: float) -> bool:
    """True when the memo is still being written. The first sighting has no previous size."""
    if (aware(now) - aware(mtime)).total_seconds() < settle_seconds:
        return True
    return previous_size is not None and previous_size != size


def publish_inbox(src: Path, inbox: Path, filename: str) -> Path:
    """Copy into the inbox as `name.ext.part`, then rename. The watcher ignores `.part`."""
    inbox.mkdir(parents=True, exist_ok=True)
    base = Path(filename)
    dest = inbox / filename
    n = 2
    while dest.exists():
        dest = inbox / f"{base.stem} ({n}){base.suffix}"
        n += 1
    part = dest.with_name(dest.name + ".part")
    if part.exists():
        part.unlink()
    shutil.copy2(src, part)
    os.replace(part, dest)
    return dest


# ------------------------------------------------------------------ ledger
class ImportLedger:
    """`state/phone_imports.json`, keyed by SHA-256. Not stored in the vault."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {"seeded": False, "files": {}}
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("Could not read %s; starting a new phone ledger", self.path)
            return
        if isinstance(raw, dict) and isinstance(raw.get("files"), dict):
            self.data = {"seeded": bool(raw.get("seeded")), "files": raw["files"]}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.data, indent=2, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".phone-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    @property
    def files(self) -> dict:
        return self.data["files"]

    @property
    def seeded(self) -> bool:
        return bool(self.data.get("seeded"))

    def by_hash(self, digest: str) -> dict | None:
        entry = self.files.get(digest)
        return entry if isinstance(entry, dict) else None

    def matches_fast(self, rec: PhoneRecording) -> dict | None:
        """Same phone path, size, and mtime: no need to copy the file again."""
        stamp = mtime_key(rec.mtime)
        for entry in self.files.values():
            if not isinstance(entry, dict):
                continue
            if (entry.get("phone_path") == rec.phone_path and entry.get("size") == rec.size
                    and entry.get("mtime") == stamp):
                return entry
        return None

    def remember(self, digest: str, *, phone_path: str, size: int, mtime: str, outcome: str,
                 vault_name: str, imported: str | None = None) -> dict:
        current = self.by_hash(digest) or {}
        # A skipped file stays skipped. An ingested file is not pushed back to queued.
        if current.get("outcome") == "skipped":
            outcome = "skipped"
        elif current.get("outcome") == "ingested" and outcome == "queued":
            outcome = "ingested"
        stamp = imported or current.get("imported") or aware(dt.datetime.now()).replace(microsecond=0).isoformat(timespec="seconds")
        entry = {
            "phone_path": phone_path or current.get("phone_path") or "",
            "size": size if size else current.get("size") or 0,
            "mtime": mtime or current.get("mtime") or "",
            "imported": stamp,
            "vault_name": vault_name or current.get("vault_name") or "",
            "outcome": outcome,
        }
        self.files[digest] = entry
        self.save()
        return entry


def seed_ledger(ledger: ImportLedger, audio_dir: Path, clients_dir: Path | None = None) -> int:
    """Hash audio already in the vault so a file dropped in by hand counts as ingested."""
    if ledger.seeded:
        return 0
    added = 0
    if audio_dir.is_dir():
        for path in sorted(audio_dir.iterdir()):
            if not path.is_file() or path.name.startswith((".", "~$")):
                continue
            digest = sha256_file(path)
            if ledger.by_hash(digest):
                continue
            modified = dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            ledger.files[digest] = {
                "phone_path": "",
                "size": path.stat().st_size,
                "mtime": mtime_key(modified),
                "imported": aware(dt.datetime.now()).replace(microsecond=0).isoformat(timespec="seconds"),
                "vault_name": path.name,
                "outcome": "ingested",
            }
            added += 1
    if clients_dir is not None and clients_dir.is_dir():
        for note in clients_dir.glob("*/Calls/*.md"):
            added += _remember_note(ledger, note)
    ledger.data["seeded"] = True
    ledger.save()
    return added


def _remember_note(ledger: ImportLedger, note: Path) -> int:
    try:
        front = get_frontmatter(note.read_text(encoding="utf-8"))
    except OSError:
        return 0
    digest = str(front.get("audio_sha256") or "")
    if not digest or ledger.by_hash(digest):
        return 0
    source = str(front.get("audio_source") or note.name)
    ledger.files[digest] = {
        "phone_path": "",
        "size": 0,
        "mtime": "",
        "imported": aware(dt.datetime.now()).replace(microsecond=0).isoformat(timespec="seconds"),
        "vault_name": Path(source).name,
        "outcome": "ingested",
    }
    return 1


def folder_has_hash(folder: Path, digest: str, cache: dict) -> str | None:
    if not folder.is_dir():
        return None
    for path in folder.iterdir():
        if not path.is_file() or path.name.startswith((".", "~$")) or path.name.endswith(".part"):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        key = str(path)
        cached = cache.get(key)
        if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            found = cached[2]
        else:
            found = sha256_file(path)
            cache[key] = (stat.st_size, stat.st_mtime_ns, found)
        if found == digest:
            return path.name
    return None


def ingest_has_hash(path: Path, digest: str) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and digest in data


def note_has_hash(clients_dir: Path, digest: str) -> str | None:
    if not clients_dir.is_dir():
        return None
    for note in clients_dir.glob("*/Calls/*.md"):
        try:
            front = get_frontmatter(note.read_text(encoding="utf-8"))
        except OSError:
            continue
        if str(front.get("audio_sha256") or "") == digest:
            return note.name
    return None


# ------------------------------------------------------------------ one connect
class ConnectTracker:
    """Act once each time the phone goes from unplugged to readable. Unplug clears it."""

    def __init__(self, settle_seconds: float = 30.0):
        self.settle_seconds = settle_seconds
        self.usb = False
        self.latched = False
        self.charging_told = False
        self.sizes: dict[str, int] = {}
        self.retry_after: dt.datetime | None = None

    def latch(self) -> None:
        self.latched = True

    def note_failure(self, now: dt.datetime) -> None:
        self.retry_after = aware(now) + dt.timedelta(seconds=RETRY_SECONDS)

    def _clear(self) -> None:
        self.usb = False
        self.latched = False
        self.charging_told = False
        self.sizes.clear()
        self.retry_after = None

    def decide(self, *, usb: bool, readable: bool, newest: PhoneRecording | None, now: dt.datetime,
               ledger: ImportLedger) -> Decision:
        now = aware(now)
        if not usb:
            disconnected = self.usb
            self._clear()
            return Decision("idle", disconnected=disconnected)
        connected = not self.usb
        self.usb = True
        if not readable:
            if self.charging_told:
                return Decision("idle", connected=connected)
            self.charging_told = True
            return Decision("charging", toast=CHARGING_TOAST, connected=connected)
        if self.latched:
            return Decision("idle", connected=connected)
        if self.retry_after is not None and now < self.retry_after:
            return Decision("idle", connected=connected)
        self.retry_after = None
        if newest is None:
            self.latched = True
            return Decision("empty", connected=connected)
        previous = self.sizes.get(newest.phone_path)
        self.sizes[newest.phone_path] = newest.size
        if recording_in_progress(newest.mtime, now, previous, newest.size, self.settle_seconds):
            return Decision("waiting", newest, connected=connected)
        fast = ledger.matches_fast(newest)
        if fast is not None:
            self.latched = True
            toast = "" if fast.get("outcome") == "skipped" else ALREADY_TOAST
            return Decision("known", newest, toast=toast, connected=connected)
        return Decision("copy", newest, connected=connected)


# ------------------------------------------------------------------ driver
class PhoneMonitor:
    """One scan per poll. MTP work stays here; the companion only supplies the picker."""

    def __init__(self, storage, ledger: ImportLedger, inbox: Path, review: Path, audio: Path,
                 clients: Path, ingest_log: Path, temp_dir: Path, *, settle_seconds: float = 30.0,
                 toast=None, ask=None, create_client=None, picker_status=None):
        self.storage = storage
        self.ledger = ledger
        self.inbox = Path(inbox)
        self.review = Path(review)
        self.audio = Path(audio)
        self.clients = Path(clients)
        self.ingest_log = Path(ingest_log)
        self.temp_dir = Path(temp_dir)
        self.toast = toast or (lambda _text: None)
        self.ask = ask or (lambda _rec, _heading, _done: None)
        self.create_client = create_client
        self.picker_status = picker_status or (lambda: (False, None))
        self.tracker = ConnectTracker(settle_seconds)
        self.lock = threading.RLock()
        self.hash_cache: dict = {}
        self.holding: tuple[PhoneRecording, str, Path, str] | None = None
        self.waiting_for_user = False
        self._seeded = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.poll_seconds = 5.0

    def start(self, poll_seconds: float = 5.0) -> None:
        self.poll_seconds = poll_seconds
        self._thread = threading.Thread(target=self._loop, name="phone-import", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def _loop(self) -> None:
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:  # noqa: BLE001  listing still works if COM was already started
            log.debug("COM init for the phone scan", exc_info=True)
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception:  # noqa: BLE001  one bad scan must not stop the companion
                log.exception("Phone scan failed")
            self._stop.wait(self.poll_seconds)

    def tick_once(self, now: dt.datetime | None = None) -> None:
        now = aware(now or dt.datetime.now())
        with self.lock:
            self._promote()
            if not self._seeded:
                count = seed_ledger(self.ledger, self.audio, self.clients)
                self._seeded = True
                log.info("Phone ledger seeded (%d new hash(es) from the vault).", count)
            busy = self.tracker.latched or self.holding is not None
        if busy:
            self._while_busy(now)
            return
        try:
            usb = bool(self.storage.usb_present())
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not check whether the phone is plugged in")
            self.toast(f"Phone: {exc}")
            self.tracker.note_failure(now)
            return
        readable = False
        newest = None
        if usb:
            try:
                readable = bool(self.storage.storage_readable())
                if readable:
                    newest = newest_recording(list(self.storage.list_recordings()))
            except Exception as exc:  # noqa: BLE001
                log.exception("Could not read the phone")
                self.toast(f"Phone: {exc}")
                self.tracker.note_failure(now)
                return
        with self.lock:
            decision = self.tracker.decide(usb=usb, readable=readable, newest=newest, now=now, ledger=self.ledger)
        self._report(decision)
        if decision.kind == "copy" and decision.recording is not None:
            self._copy_and_consider(decision.recording, now)

    def _while_busy(self, now: dt.datetime) -> None:
        try:
            usb = bool(self.storage.usb_present())
        except Exception:  # noqa: BLE001
            log.debug("Could not recheck the phone", exc_info=True)
            return
        with self.lock:
            if not usb:
                if self.holding is not None:
                    log.info("Phone unplugged before a client was chosen")
                    self._drop_holding()
                decision = self.tracker.decide(usb=False, readable=False, newest=None, now=now, ledger=self.ledger)
                if decision.disconnected:
                    log.info("Phone disconnected")
                return
            if self.holding is not None:
                self._maybe_ask()

    def _report(self, decision: Decision) -> None:
        if decision.disconnected:
            log.info("Phone disconnected")
        if decision.connected:
            log.info("Phone connected")
        if decision.kind == "charging":
            log.info("Phone is connected but storage is not readable")
            if decision.toast:
                self.toast(decision.toast)
        elif decision.kind == "empty":
            log.info("No voice recordings on the phone")
        elif decision.kind == "waiting" and decision.recording is not None:
            log.info("Recording in progress, will recheck: %s", decision.recording.name)
        elif decision.kind == "known" and decision.recording is not None:
            log.info("Latest recording already handled: %s", decision.recording.name)
            if decision.toast:
                self.toast(decision.toast)
        elif decision.kind == "copy" and decision.recording is not None:
            log.info("New recording %s (%d bytes, %s)", decision.recording.name, decision.recording.size,
                     mtime_key(decision.recording.mtime))

    def _copy_and_consider(self, rec: PhoneRecording, now: dt.datetime) -> None:
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        dest = self.temp_dir / f"{uuid.uuid4().hex}{Path(rec.name).suffix.lower()}"
        try:
            self.storage.copy_recording(rec, dest)
            digest = sha256_file(dest)
            length = file_duration(dest) or rec.duration
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not copy %s", rec.phone_path)
            self.toast(f"Phone: {exc}")
            self.tracker.note_failure(now)
            dest.unlink(missing_ok=True)
            return
        with self.lock:
            known = self._classify(digest)
            if known is not None:
                outcome, vault_name = known
                self.ledger.remember(digest, phone_path=rec.phone_path, size=rec.size, mtime=mtime_key(rec.mtime),
                                     outcome=outcome, vault_name=vault_name)
                self.tracker.latch()
                dest.unlink(missing_ok=True)
                log.info("Already in the vault (%s): %s", outcome, rec.name)
                if outcome != "skipped":
                    self.toast(ALREADY_TOAST)
                return
            self.holding = (rec, digest, dest, length)
            self._maybe_ask()

    def _classify(self, digest: str) -> tuple[str, str] | None:
        entry = self.ledger.by_hash(digest)
        if entry is not None:
            return str(entry.get("outcome") or "ingested"), str(entry.get("vault_name") or "")
        for folder, outcome in ((self.audio, "ingested"), (self.inbox, "queued"), (self.review, "queued")):
            name = folder_has_hash(folder, digest, self.hash_cache)
            if name:
                return outcome, name
        if ingest_has_hash(self.ingest_log, digest):
            return "ingested", ""
        note = note_has_hash(self.clients, digest)
        if note:
            return "ingested", note
        return None

    def _promote(self) -> None:
        changed = False
        for digest, entry in list(self.ledger.files.items()):
            if not isinstance(entry, dict) or entry.get("outcome") != "queued":
                continue
            size = int(entry.get("size") or 0)
            if self._queued_finished(digest, size):
                entry["outcome"] = "ingested"
                changed = True
                log.info("Phone import finished: %s", entry.get("vault_name") or digest[:12])
        if changed:
            self.ledger.save()

    def _queued_finished(self, digest: str, size: int) -> bool:
        if ingest_has_hash(self.ingest_log, digest) or note_has_hash(self.clients, digest):
            return True
        if size and self.audio.is_dir():
            same_size = False
            for path in self.audio.iterdir():
                try:
                    if path.is_file() and path.stat().st_size == size:
                        same_size = True
                        break
                except OSError:
                    continue
            if not same_size:
                return False
        return folder_has_hash(self.audio, digest, self.hash_cache) is not None

    def _maybe_ask(self) -> None:
        if self.holding is None:
            return
        shown, app = self.picker_status()
        if self.waiting_for_user and shown and app in (None, "Phone"):
            return
        self.waiting_for_user = False
        if shown and app not in (None, "Phone"):
            log.info("Meeting picker is open; the phone recording will wait")
            return
        rec, _digest, _dest, length = self.holding
        self.waiting_for_user = True
        try:
            self.ask(rec, picker_heading(rec, length), self.on_action)
        except PickerNotReady:
            self.waiting_for_user = False
            log.info("Client picker is not ready; will ask again")

    def on_action(self, action) -> None:
        with self.lock:
            if self.holding is None:
                return
            from .picker import Created, DecideLater, Select, Skip

            rec, digest, _dest, _length = self.holding
            if isinstance(action, DecideLater) or action is None:
                self._drop_holding()
                self.tracker.latch()
                log.info("Picker closed without a choice for %s; will ask on the next connect", rec.name)
                return
            if isinstance(action, Skip):
                self.ledger.remember(digest, phone_path=rec.phone_path, size=rec.size, mtime=mtime_key(rec.mtime),
                                     outcome="skipped", vault_name="")
                self._drop_holding()
                self.tracker.latch()
                self.toast(f"Skipped: {rec.name}")
                log.info("Skipped %s", rec.phone_path)
                return
            client, title = self._chosen(action, Select, Created)
            if not client:
                self.waiting_for_user = False
                self.toast("Phone: no client was chosen")
                log.info("Picker returned no client for %s", rec.name)
                return
            try:
                final = publish_inbox(
                    self.holding[2], self.inbox,
                    inbox_filename(client, recording_day(rec.name, rec.mtime), title, Path(rec.name).stem,
                                   Path(rec.name).suffix))
            except Exception as exc:  # noqa: BLE001
                log.exception("Could not queue %s", rec.name)
                self.toast(f"Phone: {exc}")
                self.tracker.note_failure(dt.datetime.now().astimezone())
                self.waiting_for_user = False
                return
            self.ledger.remember(digest, phone_path=rec.phone_path, size=rec.size, mtime=mtime_key(rec.mtime),
                                 outcome="queued", vault_name=final.name)
            self._drop_holding()
            self.tracker.latch()
            self.toast(f"Copied to inbox: {final.name}")
            log.info("Queued %s as %s", rec.phone_path, final.name)

    def _chosen(self, action, select_type, created_type) -> tuple[str | None, str]:
        title = (getattr(action, "title", None) or "").strip()
        if isinstance(action, select_type):
            return action.client, title
        if isinstance(action, created_type):
            if self.create_client is None:
                return action.name, title
            return self.create_client(action), title
        return None, title

    def _drop_holding(self) -> None:
        if self.holding is not None:
            self.holding[2].unlink(missing_ok=True)
        self.holding = None
        self.waiting_for_user = False
