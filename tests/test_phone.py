"""Phone ledger, seeding, in-progress detection, and inbox names. No MTP."""
import datetime as dt
from pathlib import Path

from vault_ingest.phone import (ALREADY_TOAST, CHARGING_TOAST, ConnectTracker, ImportLedger, PhoneMonitor,
                                PhoneRecording, inbox_filename, newest_recording, publish_inbox, recording_day,
                                recording_in_progress, seed_ledger)
from vault_ingest.picker import DecideLater, Select, Skip
from vault_ingest.vault import sha256_file

TZ = dt.timezone(dt.timedelta(hours=2))


def at(day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def rec(name: str, when: dt.datetime, size: int = 10, folder: str = "Internal storage/Recordings/Voice Recorder") -> PhoneRecording:
    return PhoneRecording(name, f"{folder}/{name}", size, when, duration="00:01:00")


class FakePhone:
    def __init__(self):
        self.present = False
        self.readable = False
        self.files: list[PhoneRecording] = []
        self.payloads: dict[str, bytes] = {}
        self.copies = 0

    def usb_present(self) -> bool:
        return self.present

    def storage_readable(self) -> bool:
        return self.readable

    def list_recordings(self):
        return list(self.files)

    def copy_recording(self, recording: PhoneRecording, dest: Path) -> None:
        self.copies += 1
        dest.write_bytes(self.payloads[recording.phone_path])


def monitor(tmp_path, phone: FakePhone, ask, client="Acme"):
    toasts: list[str] = []
    root = tmp_path / "vault"
    (root / "_System" / "audio").mkdir(parents=True)
    (root / "00_Inbox" / "_needs-review").mkdir(parents=True)
    (root / "Clients").mkdir()
    mon = PhoneMonitor(
        phone, ImportLedger(tmp_path / "state" / "phone_imports.json"),
        root / "00_Inbox", root / "00_Inbox" / "_needs-review", root / "_System" / "audio",
        root / "Clients", root / "_System" / "ingest-log.json", tmp_path / "state" / "phone-tmp",
        toast=toasts.append, ask=ask, create_client=lambda action: client)
    return mon, toasts, root


# ------------------------------------------------------------------ naming
def test_recording_day_prefers_the_name_and_keeps_the_extension():
    made = at(20, 13, 15)
    assert recording_day("Voice 002.m4a", made) == dt.date(2026, 10, 20)
    assert recording_day("Voice 251020_131510.m4a", at(1, 9)) == dt.date(2025, 10, 20)
    assert recording_day("memo-2026-10-05.wav", at(1, 9)) == dt.date(2026, 10, 5)
    assert inbox_filename("Acme", dt.date(2026, 10, 20), "", "Voice 002", ".m4a") == \
        "Acme - 2026-10-20 - Voice 002.m4a"
    assert inbox_filename("Acme", dt.date(2026, 10, 20), "Kickoff", "Voice 002", ".AAC") == \
        "Acme - 2026-10-20 - Kickoff.aac"


def test_newest_skips_call_recordings_and_other_extensions():
    older = rec("Voice 001.m4a", at(5, 10))
    newer = rec("Voice 002.m4a", at(20, 13))
    call = rec("Call 003.m4a", at(21, 9), folder="Internal storage/Recordings/Call")
    photo = rec("snap.jpg", at(21, 12))
    assert newest_recording([older, call, photo, newer]) == newer
    assert newest_recording([call, photo]) is None


def test_in_progress_is_a_fresh_mtime_or_a_changing_size():
    now = at(20, 13, 15)
    assert recording_in_progress(now - dt.timedelta(seconds=10), now, None, 10, 30)
    assert recording_in_progress(now - dt.timedelta(minutes=5), now, 10, 12, 30)
    assert not recording_in_progress(now - dt.timedelta(minutes=5), now, 12, 12, 30)
    assert not recording_in_progress(now - dt.timedelta(minutes=5), now, None, 12, 30)


def test_publish_renames_the_part_file_and_does_not_overwrite(tmp_path):
    inbox = tmp_path / "inbox"
    src = tmp_path / "src.m4a"
    src.write_bytes(b"audio")
    dest = publish_inbox(src, inbox, "Acme - 2026-10-20 - Kickoff.m4a")
    assert dest.name == "Acme - 2026-10-20 - Kickoff.m4a"
    assert dest.read_bytes() == b"audio"
    assert not list(inbox.glob("*.part"))
    again = publish_inbox(src, inbox, dest.name)
    assert again.name == "Acme - 2026-10-20 - Kickoff (2).m4a"


# ------------------------------------------------------------------ ledger
def test_seed_hashes_audio_and_call_notes_once(tmp_path):
    audio = tmp_path / "audio"
    audio.mkdir()
    (audio / "Kickoff.m4a").write_bytes(b"same-audio")
    clients = tmp_path / "Clients" / "Acme" / "Calls"
    clients.mkdir(parents=True)
    (clients / "2026-10-05 Kickoff.md").write_text(
        "---\naudio_sha256: abcdef\naudio_source: _System/audio/Other.m4a\n---\n# Call\n", encoding="utf-8")
    ledger = ImportLedger(tmp_path / "phone_imports.json")
    assert seed_ledger(ledger, audio, tmp_path / "Clients") == 2
    assert seed_ledger(ledger, audio, tmp_path / "Clients") == 0
    seeded = ledger.by_hash(sha256_file(audio / "Kickoff.m4a"))
    assert seeded["outcome"] == "ingested" and seeded["vault_name"] == "Kickoff.m4a"
    assert seeded["phone_path"] == ""
    assert ledger.by_hash("abcdef")["vault_name"] == "Other.m4a"
    again = ImportLedger(ledger.path)
    assert again.seeded and again.by_hash("abcdef")["outcome"] == "ingested"


def test_fast_path_matches_path_size_and_mtime_and_skip_is_kept(tmp_path):
    ledger = ImportLedger(tmp_path / "phone_imports.json")
    from vault_ingest.phone import mtime_key
    recording = rec("Voice 002.m4a", at(20, 13, 15), size=4)
    assert ledger.matches_fast(recording) is None
    ledger.remember("abc", phone_path=recording.phone_path, size=recording.size, mtime=mtime_key(recording.mtime),
                    outcome="skipped", vault_name="")
    found = ledger.matches_fast(recording)
    assert found["outcome"] == "skipped"
    ledger.remember("abc", phone_path=recording.phone_path, size=recording.size, mtime=mtime_key(recording.mtime),
                    outcome="queued", vault_name="nope.m4a")
    assert ledger.by_hash("abc")["outcome"] == "skipped"


def sha256_file_bytes(payload: bytes) -> str:
    import hashlib
    return hashlib.sha256(payload).hexdigest()


def test_dismiss_writes_nothing_and_skip_is_not_asked_again(tmp_path):
    phone = FakePhone()
    phone.present = True
    phone.readable = True
    recording = rec("Voice 002.m4a", at(20, 13, 15), size=4)
    phone.files = [recording]
    phone.payloads[recording.phone_path] = b"wave"
    chosen = {"action": DecideLater()}

    def ask(_rec, heading, respond):
        from vault_ingest.phone import aware
        shown = aware(recording.mtime).strftime("%Y-%m-%d %H:%M")      # local time, so any time zone passes
        assert "Voice 002.m4a" in heading and shown in heading
        respond(chosen["action"])

    mon, toasts, root = monitor(tmp_path, phone, ask)
    mon.tick_once(at(21, 2))
    assert phone.copies == 1
    assert mon.ledger.files == {}
    assert toasts == []
    assert not any((root / "00_Inbox").glob("*.m4a"))

    phone.present = False
    mon.tick_once(at(21, 3))
    phone.present = True
    chosen["action"] = Skip("Voice 002")
    mon.tick_once(at(21, 4))
    assert phone.copies == 2
    assert mon.ledger.by_hash(sha256_file_bytes(b"wave"))["outcome"] == "skipped"
    assert toasts == ["Skipped: Voice 002.m4a"]

    phone.present = False
    mon.tick_once(at(21, 5))
    phone.present = True
    mon.tick_once(at(21, 6))
    assert phone.copies == 2
    assert toasts == ["Skipped: Voice 002.m4a"]


def test_seeded_audio_is_not_queued_again(tmp_path):
    phone = FakePhone()
    phone.present = True
    phone.readable = True
    recording = rec("Voice 001.m4a", at(5, 10, 43), size=10)
    phone.files = [recording]
    phone.payloads[recording.phone_path] = b"same-audio"
    asked = []

    def ask(*_args):
        asked.append(_args)

    mon, toasts, root = monitor(tmp_path, phone, ask)
    (root / "_System" / "audio" / "Solution Hub - 2026-10-05 - Kickoff.m4a").write_bytes(b"same-audio")
    mon.tick_once(at(21, 2))
    assert asked == []
    assert phone.copies == 1
    assert toasts == [ALREADY_TOAST]
    entry = mon.ledger.by_hash(sha256_file_bytes(b"same-audio"))
    assert entry["outcome"] == "ingested"
    assert entry["phone_path"].endswith("Voice 001.m4a")

    phone.present = False
    mon.tick_once(at(21, 3))
    phone.present = True
    mon.tick_once(at(21, 4))
    assert phone.copies == 1
    assert toasts == [ALREADY_TOAST, ALREADY_TOAST]


def test_a_chosen_client_is_written_atomically_and_a_replug_does_not_copy(tmp_path):
    phone = FakePhone()
    phone.present = True
    phone.readable = True
    recording = rec("Voice 251020_131510.m4a", at(1, 9), size=5)
    phone.files = [recording]
    phone.payloads[recording.phone_path] = b"hello"

    def ask(_rec, _heading, respond):
        respond(Select("Acme", "Kickoff"))

    mon, toasts, root = monitor(tmp_path, phone, ask)
    mon.tick_once(at(21, 2))
    queued = list((root / "00_Inbox").glob("*.m4a"))
    assert [p.name for p in queued] == ["Acme - 2025-10-20 - Kickoff.m4a"]
    assert queued[0].read_bytes() == b"hello"
    assert not list((root / "00_Inbox").glob("*.part"))
    assert mon.ledger.by_hash(sha256_file_bytes(b"hello"))["outcome"] == "queued"
    assert toasts == ["Copied to inbox: Acme - 2025-10-20 - Kickoff.m4a"]

    phone.present = False
    mon.tick_once(at(21, 3))
    phone.present = True
    mon.tick_once(at(21, 4))
    assert phone.copies == 1


def test_in_progress_waits_until_the_size_settles(tmp_path):
    phone = FakePhone()
    phone.present = True
    phone.readable = True
    now = at(21, 2)
    early = rec("Voice 002.m4a", now - dt.timedelta(seconds=5), size=10)
    phone.files = [early]
    phone.payloads[early.phone_path] = b"0123456789"

    def ask(_rec, _heading, respond):
        respond(DecideLater())

    mon, _toasts, _root = monitor(tmp_path, phone, ask)
    mon.tick_once(now)
    assert phone.copies == 0
    grown = rec("Voice 002.m4a", now - dt.timedelta(minutes=5), size=20)
    phone.files = [grown]
    phone.payloads[grown.phone_path] = b"0123456789abcdef0123"
    mon.tick_once(now + dt.timedelta(seconds=10))
    assert phone.copies == 0
    mon.tick_once(now + dt.timedelta(seconds=20))
    assert phone.copies == 1


def test_charging_toasts_once_then_imports_when_storage_appears(tmp_path):
    phone = FakePhone()
    phone.present = True
    phone.readable = False
    recording = rec("Voice 002.m4a", at(20, 13), size=4)
    phone.files = [recording]
    phone.payloads[recording.phone_path] = b"wave"
    seen = []

    def ask(_rec, _heading, respond):
        seen.append(True)
        respond(Select("Acme", "Kickoff"))

    mon, toasts, _root = monitor(tmp_path, phone, ask)
    mon.tick_once(at(21, 2))
    mon.tick_once(at(21, 2, 1))
    assert toasts == [CHARGING_TOAST]
    assert phone.copies == 0
    phone.readable = True
    mon.tick_once(at(21, 2, 2))
    assert seen == [True]
    assert toasts[-1].startswith("Copied to inbox:")


def test_a_file_already_in_the_inbox_is_not_copied_twice(tmp_path):
    phone = FakePhone()
    phone.present = True
    phone.readable = True
    recording = rec("Voice 002.m4a", at(20, 13), size=4)
    phone.files = [recording]
    phone.payloads[recording.phone_path] = b"wave"

    def ask(*_args):
        raise AssertionError("picker must not open")

    mon, toasts, root = monitor(tmp_path, phone, ask)
    (root / "00_Inbox" / "waiting.m4a").write_bytes(b"wave")
    mon.tick_once(at(21, 2))
    assert phone.copies == 1
    assert toasts == [ALREADY_TOAST]
    assert mon.ledger.by_hash(sha256_file_bytes(b"wave"))["outcome"] == "queued"


def test_tracker_does_not_latch_a_failed_copy(tmp_path):
    tracker = ConnectTracker(30)
    ledger = ImportLedger(tmp_path / "phone_imports.json")
    recording = rec("Voice 002.m4a", at(20, 13), size=4)
    now = at(21, 2)
    decision = tracker.decide(usb=True, readable=True, newest=recording, now=now, ledger=ledger)
    assert decision.kind == "copy" and decision.connected
    tracker.note_failure(now)
    again = tracker.decide(usb=True, readable=True, newest=recording, now=now + dt.timedelta(seconds=5), ledger=ledger)
    assert again.kind == "idle"
    later = tracker.decide(usb=True, readable=True, newest=recording, now=now + dt.timedelta(seconds=20), ledger=ledger)
    assert later.kind == "copy"
