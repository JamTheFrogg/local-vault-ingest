"""Client picker: search, meeting keys, and how the pipeline uses a choice. No display."""
import datetime as dt
import json
import os
from pathlib import Path

from conftest import fake_responder, make_transcript

from vault_ingest.clients import (ClientInfo, MeetingMap, contact_names, create_client, fold, load_clients,
                                  meeting_key, near_duplicate, rank_clients)
from vault_ingest.llm import FakeLLM
from vault_ingest.picker import (AskDuplicate, AskField, Created, DecideLater, PickerState, Select, Skip,
                                 apply_picker_result)
from vault_ingest.pipeline import Pipeline
from vault_ingest.sessions import SessionStore, overlaps_skipped
from vault_ingest.vault import Vault, get_frontmatter, get_section

TZ = dt.timezone(dt.timedelta(hours=2))


def at(h: int, m: int = 0, s: int = 0) -> dt.datetime:
    return dt.datetime(2026, 9, 30, h, m, s, tzinfo=TZ)


def records() -> list[ClientInfo]:
    return [
        ClientInfo("Acme", [], ["Amra"]),
        ClientInfo("Šehić", ["Hodžić"], []),
        ClientInfo("Đorđević", [], []),
        ClientInfo("Žaba", [], []),
        ClientInfo("Ćović", [], []),
        ClientInfo("Acme Holdings", [], []),
    ]


def row(state: PickerState, kind: str):
    return next(i for i, r in enumerate(state.rows) if r.kind == kind)


# ------------------------------------------------------------------ matching
def test_fold_maps_digraphs_and_diacritics():
    assert fold("Đorđević") == "djordjevic"
    assert fold("Šehić") == "sehic"
    assert fold("Žaba") == "zaba"
    assert fold("Čović") == fold("Ćović") == "covic"
    assert fold("dj") == fold("đ")


def test_fuzzy_ranking_typos_and_diacritics():
    found = records()
    assert rank_clients(found, "acem")[0].name == "Acme"
    assert rank_clients(found, "sehic")[0].name == "Šehić"
    assert rank_clients(found, "djordjevic")[0].name == "Đorđević"
    assert rank_clients(found, "zaba")[0].name == "Žaba"
    assert rank_clients(found, "covic")[0].name == "Ćović"
    assert rank_clients(found, "zzzzzzzz") == []


def test_alias_and_contact_matching():
    found = records()
    alias = rank_clients(found, "hodzic")[0]
    assert alias.name == "Šehić" and alias.via == "alias"
    contact = rank_clients(found, "amra")[0]
    assert contact.name == "Acme" and contact.via == "contact"
    assert contact_names("Acme d.o.o. Contact: Amra (CEO).") == ["Amra"]


def test_near_duplicate_threshold():
    found = [ClientInfo("Acme Holdings", [], []), ClientInfo("Acme", [], [])]
    assert near_duplicate("Acme Holding", found) == "Acme Holdings"
    assert near_duplicate("acme", found) == "Acme"
    assert near_duplicate("Čović", [ClientInfo("Covic", [], [])]) == "Covic"
    assert near_duplicate("Globex", found) is None


def test_meeting_keys():
    assert meeting_key("Google Meet", "Meet - abc-defg-hij - Google Chrome") == "meet:abc-defg-hij"
    assert meeting_key("Google Meet", "Meet - Google Chrome") is None
    assert meeting_key("Google Meet", "Day-Long-Fun - YouTube") is None
    assert meeting_key("Zoom", "Zoom Meeting 123 456 7890") == "zoom:1234567890"
    assert meeting_key("Zoom", "123-456-789") == "zoom:123456789"
    assert meeting_key("Zoom", "Quarterly review - Google Chrome") == "zoom-title:quarterly review"
    assert meeting_key("Teams", "Weekly sync | Microsoft Teams") == "teams:weekly sync"
    assert meeting_key("Zoom (web)", "Launch Meeting - Zoom - Brave") == "zoom-title:launch meeting - zoom"


def test_preselect_from_meeting_map(tmp_path):
    mmap = MeetingMap(tmp_path / "meeting-map.json")
    mmap.put("meet:abc-defg-hij", "Šehić")
    key = meeting_key("Google Meet", "Meet - abc-defg-hij")
    state = PickerState(records(), preselect=mmap.get(key))
    assert state.rows[0].client == "Šehić"
    assert state.hint == "Enter to confirm"
    assert state.confirm() == Select("Šehić", "")
    assert [r.kind for r in state.rows[-4:]] == ["create", "personal", "unsorted", "skip"]
    assert state.rows[-1].label == "Don't ingest this meeting"
    phone = PickerState(records(), skip_label="Not a meeting / skip")
    assert phone.rows[-1].kind == "skip" and phone.rows[-1].label == "Not a meeting / skip"


def test_keyboard_create_flow_and_escape():
    state = PickerState(records())
    state.set_query("Picker Test")
    state.selected = row(state, "create")
    assert isinstance(state.confirm(), AskField)
    assert state.submit_field("") == AskField("company")
    assert state.submit_field("") == AskField("note")
    created = state.submit_field("weekly sync")
    assert created == Created("Picker Test", None, None, "weekly sync", "")

    dup = PickerState([ClientInfo("Acme Holdings", [], [])])
    dup.set_query("Acme Holding")
    dup.selected = row(dup, "create")
    asked = dup.confirm()
    assert isinstance(asked, AskDuplicate) and asked.existing == "Acme Holdings"
    assert dup.answer_duplicate(True) == Select("Acme Holdings", "")

    later = PickerState(records(), preselect="Acme")
    assert isinstance(later.cancel(), DecideLater)
    later.selected = row(later, "skip")
    assert later.confirm() == Skip("")
    later.selected = row(later, "unsorted")
    assert later.confirm() == Select("Unsorted", "")


def test_create_client_writes_snapshot_and_aliases(vault, settings):
    name = create_client(settings, "Šehić", aliases=["Sehic"], contact="Amra Hodžić",
                         company="Šehić d.o.o.", note="weekly")
    assert name == "Šehić"
    root = vault / "Clients" / "Šehić"
    for sub in ("Calls", "Transcripts", "Work"):
        assert (root / sub).is_dir()
    text = (root / "Šehić.md").read_text(encoding="utf-8")
    aliases = get_frontmatter(text)["aliases"]
    assert aliases == ["Sehic", "Amra Hodžić", "Šehić d.o.o."]
    snap = get_section(text, "Snapshot")
    assert "Contact: Amra Hodžić" in snap and "Company: Šehić d.o.o." in snap and "- weekly" in snap
    loaded = load_clients(vault / "Clients")
    sehic = next(c for c in loaded if c.name == "Šehić")
    assert "Amra Hodžić" in sehic.contacts and "Sehic" in sehic.aliases


def test_confirm_writes_the_session_and_the_map(vault, settings):
    v = Vault(vault, ("00_Inbox", "Clients", "_System"))
    store = SessionStore(vault / "_System/meetings", v)
    session = store.start("Google Meet", "Meet - abc-defg-hij", "chrome.exe", at(14),
                          meeting_key="meet:abc-defg-hij")
    mmap = MeetingMap(vault / "_System/meeting-map.json", v)
    apply_picker_result(Select("Acme", "Weekly"), session, store, settings, mmap)
    data = json.loads(session.path.read_text(encoding="utf-8"))
    assert data["client"] == "Acme" and data["call_title"] == "Weekly" and data["skip"] is False
    assert mmap.get("meet:abc-defg-hij") == "Acme"
    apply_picker_result(Skip("Weekly"), session, store, settings, mmap)
    data = json.loads(session.path.read_text(encoding="utf-8"))
    assert data["skip"] is True and data["client"] is None


# ------------------------------------------------------------------ pipeline
def write_session(vault: Path, **extra) -> Path:
    data = {"id": "pick1", "app": "Google Meet", "window_title": "Meet - abc-defg-hij",
            "start": "2026-09-30T14:00:00+02:00", "end": "2026-09-30T14:50:00+02:00",
            "screenshots": [], **extra}
    folder = vault / "_System/meetings"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "2026-09-30 14-00-00 Google Meet.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def drop(vault: Path, name: str = "zoom_recording_2026-09-30.txt") -> Path:
    f = vault / "00_Inbox" / name
    f.write_text(make_transcript(2500), encoding="utf-8")
    end = at(14, 48).timestamp()
    os.utime(f, (end, end))
    return f


def log_record(vault: Path) -> dict:
    return next(iter(json.loads((vault / "_System/ingest-log.json").read_text(encoding="utf-8")).values()))


def test_pipeline_uses_the_picker_client(vault, settings):
    write_session(vault, client="Globex", call_title="Launch review")
    drop(vault)
    llm = FakeLLM(fake_responder())
    [r] = Pipeline(settings, llm, concurrency=2).run_inbox()
    assert r.status == "processed" and r.client == "Globex", r.message
    note = vault / "Clients/Globex/Calls/2026-09-30 Launch review.md"
    assert note.is_file() and get_frontmatter(note.read_text(encoding="utf-8"))["client"] == "Globex"
    assert not any("Which client is this call with" in user for _, user in llm.calls)
    rec = log_record(vault)
    assert rec["identified_by"] == "picker" and rec["profile_lines"]
    saved = json.loads((vault / "_System/meetings/2026-09-30 14-00-00 Google Meet.json").read_text(encoding="utf-8"))
    assert saved["processed_client"] == "Globex"


def test_unsorted_is_a_normal_client_folder(vault, settings):
    write_session(vault, client="Unsorted")
    drop(vault)
    [r] = Pipeline(settings, FakeLLM(fake_responder()), concurrency=2).run_inbox()
    assert r.status == "processed" and r.client == "Unsorted", r.message
    assert (vault / "Clients/Unsorted/Unsorted.md").is_file()
    assert log_record(vault)["identified_by"] == "picker"


def test_skip_ingests_nothing(vault, settings):
    write_session(vault, skip=True)
    src = drop(vault, "Acme - 2026-09-30 - Kickoff.txt")
    llm = FakeLLM(fake_responder())
    [r] = Pipeline(settings, llm, concurrency=2).run_inbox()
    assert r.status == "skipped" and not src.exists()
    assert list((vault / "_System/skipped").glob("*.txt"))
    assert llm.calls == []
    assert not list((vault / "Clients/Acme/Calls").glob("*.md"))


def test_no_selection_still_uses_the_llm(vault, settings):
    write_session(vault)
    drop(vault)
    llm = FakeLLM(fake_responder())
    [r] = Pipeline(settings, llm, concurrency=2).run_inbox()
    assert r.status == "processed" and r.client == "Acme", r.message
    assert any("Which client is this call with" in user for _, user in llm.calls)
    assert log_record(vault)["identified_by"].startswith("llm")


def test_reassignment_moves_the_call_and_lists_profile_lines(vault, settings):
    write_session(vault, client="Acme", call_title="Kickoff")
    drop(vault)
    pipe = Pipeline(settings, FakeLLM(fake_responder()), concurrency=2)
    [r] = pipe.run_inbox()
    assert r.status == "processed", r.message
    profile = (vault / "Clients/Acme/Acme.md").read_text(encoding="utf-8")
    assert "Book product photographer" in profile
    store = SessionStore(vault / "_System/meetings", pipe.vault)
    [session] = store.load()
    store.update(session, client="Globex")
    assert pipe.sync_meeting_selections() == 1

    assert not list((vault / "Clients/Acme/Calls").glob("*.md"))
    note = vault / "Clients/Globex/Calls/2026-09-30 Kickoff.md"
    text = note.read_text(encoding="utf-8")
    assert get_frontmatter(text)["client"] == "Globex"
    assert "Clients/Globex/Globex" in text and "Clients/Acme/" not in text
    chunk = next((vault / "Clients/Globex/Transcripts/2026-09-30 Kickoff").glob("*.md"))
    assert get_frontmatter(chunk.read_text(encoding="utf-8"))["client"] == "Globex"
    assert (vault / "Clients/Globex/Transcripts/raw").glob("*.txt")
    assert not list((vault / "Clients/Acme/Transcripts/raw").glob("*.txt"))

    still = (vault / "Clients/Acme/Acme.md").read_text(encoding="utf-8")
    assert "Book product photographer" in still
    digest = (vault / "_System/digest.md").read_text(encoding="utf-8")
    assert "Book product photographer" in digest and "Moved call from Acme to Globex" in digest
    saved = json.loads(session.path.read_text(encoding="utf-8"))
    assert saved["processed_client"] == "Globex"
    assert pipe.sync_meeting_selections() == 0


def test_dont_ingest_skips_transcription(vault, tmp_path, monkeypatch):
    from vault_ingest.config import Settings
    from vault_ingest.transcribe import RecordingQueue

    folder = tmp_path / "rec"
    folder.mkdir()
    (folder / "audio.mp4").write_bytes(b"audio")
    (folder / "metadata.json").write_text(
        json.dumps({"meeting_name": "Standup", "created_at": "2026-10-01T08:00:00+00:00"}), encoding="utf-8")
    settings = Settings(vault_path=vault)

    def explode(*_a, **_k):
        raise AssertionError("a skipped meeting must not be transcribed")

    monkeypatch.setattr("vault_ingest.transcribe.transcribe_path", explode)
    queue = RecordingQueue(tmp_path, vault / "00_Inbox", vault / "_System/whisper-sources.json",
                           Vault(vault, ("00_Inbox", "_System")), settings, settle_seconds=0,
                           should_skip=lambda _when: True)
    queue._transcribe(folder)
    assert not list((vault / "00_Inbox").glob("*.txt"))
    state = json.loads((vault / "_System/whisper-sources.json").read_text(encoding="utf-8"))
    assert str(folder) in state["done"]


def test_overlaps_skipped():
    from vault_ingest.sessions import Session

    live = Session(id="s", app="Zoom", window_title="Zoom", start=at(14), end=at(15), skip=True)
    assert overlaps_skipped([live], at(14, 30))
    assert not overlaps_skipped([live], at(16))
    live.skip = False
    assert not overlaps_skipped([live], at(14, 30))


def test_new_client_name_is_asked_in_the_popup():
    state = PickerState(records())
    state.selected = row(state, "create")
    assert state.confirm() == AskField("name")                 # nothing typed: the popup asks for the name
    assert state.submit_field("  ") == AskField("name")
    assert state.submit_field("Bilal Plakari") == AskField("contact")
    state.submit_field("")
    state.submit_field("")
    assert state.submit_field("") == Created("Bilal Plakari", None, None, None, "")


def test_personal_call_asks_who_and_files_under_personal():
    state = PickerState(records(), title="Wedding venue", person="Amra")
    state.selected = row(state, "personal")
    assert state.confirm() == AskField("person") and state.field_value == "Amra"
    assert state.submit_field("") == AskField("person")
    assert state.submit_field("Amra") == Select("Personal", "Amra - Wedding venue")
    typed = PickerState(records())
    typed.set_query("Emin")
    typed.selected = row(typed, "personal")
    typed.confirm()
    assert typed.field_value == "Emin" and typed.submit_field("Emin") == Select("Personal", "Emin")
    again = PickerState(records(), preselect="Personal")
    assert again.rows[again.selected].kind == "personal" and again.hint == "Enter to confirm"


def test_escape_in_a_field_goes_back_to_the_list():
    state = PickerState(records())
    state.selected = row(state, "create")
    state.confirm()
    state.back()
    assert state.mode == "browse" and state.rows
