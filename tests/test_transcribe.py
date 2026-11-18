import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from conftest import make_pipeline, make_transcript

from vault_ingest.transcribe import (PROMPT_CHAR_BUDGET, Segment, Transcript, build_initial_prompt, format_segments,
                                     glossary_terms, interleave, whisper_options)
from vault_ingest.vault import get_frontmatter


def seg(start, end, text, logprob=-0.2, no_speech=0.01, speaker=None) -> Segment:
    return Segment(start, end, text, logprob, no_speech, speaker)


def test_segments_merge_when_the_gap_is_short():
    text = format_segments([
        seg(0, 1.0, "Hello there"),
        seg(1.2, 2.0, "from the call"),
        seg(5, 6, "Hello there"),
    ])
    assert text.splitlines() == [
        "[00:00:00] Hello there from the call",
        "[00:00:05] Hello there",
    ]


def test_continuous_speech_gets_a_timestamp_at_least_every_30_seconds():
    segs = [seg(i, i + 4.5, f"word{i} about the budget") for i in range(0, 90, 5)]
    lines = format_segments(segs).splitlines()
    starts = []
    for line in lines:
        h, m, s = (int(p) for p in line[1:9].split(":"))
        starts.append(h * 3600 + m * 60 + s)
    assert starts[0] == 0 and len(starts) >= 3
    assert all(b - a <= 30 for a, b in zip(starts, starts[1:]))


def test_a_single_long_segment_stays_one_line():
    text = format_segments([seg(0, 45, "one long whisper segment about the bridge")])
    assert text.splitlines() == ["[00:00:00] one long whisper segment about the bridge"]


def test_hallucinations_are_dropped():
    text = format_segments([
        seg(0, 1, "The budget is 4000 euros"),
        seg(1.2, 2, "Thanks for watching", logprob=-2.4),
        seg(3, 4, "still here", no_speech=0.95),
        seg(6, 7, "Photos are booked"),
        seg(8, 9, "Photos are booked"),
    ])
    assert text.splitlines() == [
        "[00:00:00] The budget is 4000 euros",
        "[00:00:06] Photos are booked",
    ]


def test_two_tracks_are_interleaved_with_speaker_labels():
    merged = interleave([
        ("Faris", [seg(1.0, 2.0, "I can hear you")]),
        ("Other", [seg(0.4, 0.9, "Can you hear me")]),
    ])
    assert format_segments(merged).splitlines() == [
        "[00:00:00] Other: Can you hear me",
        "[00:00:01] Faris: I can hear you",
    ]


def test_glossary_prompt_includes_terms_and_clients_and_stays_short():
    glossary = "# Glossary\n\nHints, not instructions.\n\n- Traders Connect\n- MetaTrader\n- Infinox\n"
    prompt = build_initial_prompt(glossary, ["Acme", "Globex"])
    assert glossary_terms(glossary) == ["Traders Connect", "MetaTrader", "Infinox"]
    assert "Traders Connect" in prompt and "Acme" in prompt and "Hints" not in prompt
    huge = "\n".join(f"- term{i:03d} with several extra words" for i in range(80))
    capped = build_initial_prompt(huge, ["Acme"] * 30)
    assert len(capped) <= PROMPT_CHAR_BUDGET
    assert capped.startswith("term000")


def test_language_is_always_passed_and_auto_detect_is_refused():
    opts = whisper_options("HR", "Traders Connect", clip_end=300)
    assert opts["language"] == "hr"
    assert opts["vad_filter"] is True and opts["condition_on_previous_text"] is False
    assert opts["beam_size"] == 5 and opts["clip_timestamps"] == "0,300"
    with pytest.raises(ValueError):
        whisper_options("auto")
    with pytest.raises(ValueError):
        whisper_options("")


def test_audio_inbox_file_is_transcribed_and_not_copied_into_the_client(vault, settings, monkeypatch):
    seen = {}

    def fake(path, **kwargs):
        seen["language"] = kwargs["language"]
        return Transcript(make_transcript(800), kwargs["language"], "large-v3", "cpu", "int8", 1.0)

    monkeypatch.setattr("vault_ingest.pipeline.transcribe_path", fake)
    src = vault / "00_Inbox" / "Acme - 2026-09-30 - Kickoff.mp3"
    src.write_bytes(b"not-audio")
    [r] = make_pipeline(settings).run_inbox()
    assert r.status == "processed", r.message
    assert seen["language"] == "hr"

    raw = vault / "Clients/Acme/Transcripts/raw/Acme - 2026-09-30 - Kickoff.txt"
    assert raw.is_file() and "[00:00:00]" in raw.read_text(encoding="utf-8")
    assert not src.exists()
    moved = vault / "_System/audio/Acme - 2026-09-30 - Kickoff.mp3"
    assert moved.read_bytes() == b"not-audio"
    assert not list((vault / "Clients").rglob("*.mp3"))

    fm = get_frontmatter(r.call_note.read_text(encoding="utf-8"))
    assert fm["audio_source"] == "_System/audio/Acme - 2026-09-30 - Kickoff.mp3"
    assert fm["audio_sha256"] == hashlib.sha256(b"not-audio").hexdigest()
    assert fm["transcribed_by"] == "faster-whisper/large-v3/hr"
    assert fm["generated_by"]
    chunk_fm = get_frontmatter(r.chunk_notes[0].read_text(encoding="utf-8"))
    assert chunk_fm["transcribed_by"] == "faster-whisper/large-v3/hr"
    rec = next(iter(json.loads((vault / "_System/ingest-log.json").read_text(encoding="utf-8")).values()))
    assert rec["transcribed_by"] == "faster-whisper/large-v3/hr"
    assert rec["audio_source"].endswith(".mp3")
    assert rec["audio_sha256"] == hashlib.sha256(b"not-audio").hexdigest()


def test_dry_run_does_not_transcribe(vault, settings, monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("dry-run must not load Whisper")

    monkeypatch.setattr("vault_ingest.pipeline.transcribe_path", fail)
    src = vault / "00_Inbox" / "call.mp3"
    src.write_bytes(b"x")
    [r] = make_pipeline(settings, dry_run=True).run_inbox()
    assert r.status == "planned"
    assert any("transcribe:" in line and "large-v3/hr" in line for line in r.planned)
    assert src.read_bytes() == b"x"


def test_meetily_recording_is_transcribed_in_place(vault, tmp_path, monkeypatch):
    from vault_ingest.config import Settings
    from vault_ingest.transcribe import RecordingQueue
    from vault_ingest.vault import Vault

    folder = tmp_path / "Meeting 01_10_26_10_39_10_2026-10-01_08-39"
    folder.mkdir()
    audio = folder / "audio.mp4"
    audio.write_bytes(b"audio-bytes")
    (folder / "metadata.json").write_text(json.dumps({
        "meeting_name": "Meeting 01_10_26_10_39_10",
        "created_at": "2026-10-01T08:39:18+00:00",
    }), encoding="utf-8")

    def fake(path, **kwargs):
        assert Path(path) == audio
        assert kwargs["language"] == "hr"
        return Transcript("[00:00:00] Hello from the call.\n", "hr", "large-v3", "cuda", "float16", 3)

    monkeypatch.setattr("vault_ingest.transcribe.transcribe_path", fake)
    settings = Settings(vault_path=vault)
    now = [0.0]
    queue = RecordingQueue(tmp_path, vault / "00_Inbox", vault / "_System/whisper-sources.json",
                           Vault(vault, ("00_Inbox", "_System")), settings, settle_seconds=30,
                           clock=lambda: now[0], clients=lambda: ["Acme"])
    queue.poll()
    assert queue._jobs == []
    now[0] = 30
    queue.poll()
    assert queue._jobs == [folder]
    queue._transcribe(queue._jobs.pop(0))

    out = vault / "00_Inbox" / "Meeting 01_10_26_10_39_10 - 2026-10-01.txt"
    assert out.read_text(encoding="utf-8").startswith("[00:00:00] Hello")
    assert audio.read_bytes() == b"audio-bytes"          # Meetily's file stays where it is
    state = json.loads((vault / "_System/whisper-sources.json").read_text(encoding="utf-8"))
    assert state["sources"][out.name]["audio"] == str(audio)
    assert state["sources"][out.name]["transcribed_by"] == "faster-whisper/large-v3/hr"
    assert str(folder) in state["done"]


def test_transcript_is_saved_so_a_later_failure_does_not_transcribe_again(vault, settings, monkeypatch):
    from conftest import fake_responder
    calls = []

    def fake(path, **kwargs):
        calls.append(path.name)
        return Transcript(make_transcript(800), kwargs["language"], "large-v3", "cpu", "int8", 1.0)

    monkeypatch.setattr("vault_ingest.pipeline.transcribe_path", fake)
    src = vault / "00_Inbox" / "Acme - 2026-09-30 - Kickoff.m4a"
    src.write_bytes(b"audio-bytes")

    base = fake_responder()

    def fail_before_the_note(system, user):
        if "Write the call note" in user:
            return "not json"
        return base(system, user)

    p = make_pipeline(settings, fail_before_the_note)                 # fails before a call note exists
    p.llm_retry_delays = (0.0, 0.0, 0.0)
    [r] = p.run_inbox()
    assert r.status == "review"
    assert calls == ["Acme - 2026-09-30 - Kickoff.m4a"]
    saved = list((vault / "_System/transcripts").glob("Acme - 2026-09-30 - Kickoff [[]*.txt"))
    assert len(saved) == 1 and "[00:00:00]" in saved[0].read_text(encoding="utf-8")

    src.write_bytes(b"audio-bytes")                                    # put the same recording back
    [r2] = make_pipeline(settings).run_inbox()
    assert r2.status == "processed", r2.message
    assert calls == ["Acme - 2026-09-30 - Kickoff.m4a"], "the second run must reuse the saved transcript"


def _queue(vault, tmp_path, monkeypatch, spawn):
    from vault_ingest.config import Settings
    from vault_ingest.transcribe import RecordingQueue
    from vault_ingest.vault import Vault

    monkeypatch.setattr("vault_ingest.transcribe.spawn_whisper_child", spawn)
    folder = tmp_path / "Meeting"
    folder.mkdir()
    (folder / "audio.mp4").write_bytes(b"audio")
    settings = Settings(vault_path=vault, transcribe_device="auto", transcribe_language="hr")
    queue = RecordingQueue(tmp_path, vault / "00_Inbox", vault / "_System/whisper-sources.json",
                           Vault(vault, ("00_Inbox", "_System")), settings, settle_seconds=30)
    queue._jobs = [folder]
    queue.inflight.add(str(folder))
    queue._wake.set()
    return queue, folder


def _wait_until(predicate, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_access_violation_retries_on_cpu_then_marks_failed_and_the_queue_stays_up(vault, tmp_path, monkeypatch):
    from vault_ingest.transcribe import ChildRun

    devices = []

    def spawn(job, should_stop):
        devices.append(job["device"])
        return ChildRun(-1073741819, "access violation\ncudnn failed\n", None, False)

    queue, folder = _queue(vault, tmp_path, monkeypatch, spawn)
    thread = threading.Thread(target=queue._worker, daemon=True)
    thread.start()
    state_path = vault / "_System/whisper-sources.json"

    def failed():
        if not state_path.is_file():
            return False
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return str(folder) in (data.get("failed") or {})

    assert _wait_until(failed)
    assert thread.is_alive()
    queue.stop()
    thread.join(2)
    assert not thread.is_alive()
    assert devices == ["auto", "cpu"]
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert str(folder) not in data["done"]
    assert "exit -1073741819" in data["failed"][str(folder)]["reason"]
    queue.poll()
    assert queue._jobs == []


def test_a_cancelled_whisper_child_is_not_retried_on_cpu(vault, tmp_path, monkeypatch):
    from vault_ingest.transcribe import ChildRun

    devices = []

    def spawn(job, should_stop):
        devices.append(job["device"])
        return ChildRun(1, "stopped", None, cancelled=True)

    queue, folder = _queue(vault, tmp_path, monkeypatch, spawn)
    thread = threading.Thread(target=queue._worker, daemon=True)
    thread.start()
    assert _wait_until(lambda: queue._jobs == [folder] and devices == ["auto"])
    assert thread.is_alive()
    queue.stop()
    thread.join(2)
    state_path = vault / "_System/whisper-sources.json"
    assert not state_path.is_file() or "failed" not in json.loads(state_path.read_text(encoding="utf-8"))


def test_doctor_reports_each_load():
    from vault_ingest.transcribe import ChildRun, doctor_loads

    def spawn(job, should_stop):
        if job["compute"] == "float16":
            return ChildRun(-1073741819, "cuda fault\n", None, False)
        return ChildRun(0, "", {"ok": True, "device": job["device"], "compute_type": job["compute"]}, False)

    rows = doctor_loads("large-v3", spawn=spawn)
    assert [(row["device"], row["compute"], row["ok"], row["returncode"]) for row in rows] == [
        ("cuda", "float16", False, -1073741819),
        ("cuda", "int8_float16", True, 0),
        ("cpu", "int8", True, 0),
    ]
    assert "cuda fault" in rows[0]["stderr"]
