import dataclasses
import datetime as dt
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import httpx
import pytest
from PIL import Image

from conftest import fake_responder, make_transcript

from vault_ingest.chunker import Chunk, chunk_turns
from vault_ingest.config import PROJECT_DIR, Settings
from vault_ingest.llm import FakeLLM, ImageInput, LocalStudioLLM, VisionUnsupported
from vault_ingest.meetily import MeetilyExporter, export_name
from vault_ingest.parsers import Turn, parse_file
from vault_ingest.pipeline import Pipeline, chunk_for_offset
from vault_ingest.schemas import ScreenshotDescription
from vault_ingest.screenshots import ScreenshotWatcher, created_ts
from vault_ingest.sessions import (Session, SessionStore, hms, match_session, session_for_time, to_local,
                                   transcript_windows)
from vault_ingest.vault import Vault, get_frontmatter, get_section
from vault_ingest.watcher_meetings import (Companion, Ended, Match, MeetingTracker, Started, Window, load_config,
                                           match_windows, pynput_hotkey)

TZ = dt.timezone(dt.timedelta(hours=2))
SESSION_NAME = "2026-09-30 14-00-00 Google Meet.json"
ATTACH = "Clients/Acme/Calls/attachments/2026-09-30 Kickoff"


def at(h: int, m: int = 0, s: int = 0, day: int = 30) -> dt.datetime:
    return dt.datetime(2026, 9, day, h, m, s, tzinfo=TZ)


def png(path: Path, color=(200, 30, 30)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), color).save(path)
    return path


def session(start, end=None, app="Google Meet", sid=None) -> Session:
    return Session(id=sid or f"s-{start:%H%M}", app=app, window_title="Meet - abc-defg-hij", start=start, end=end)


@pytest.fixture(scope="module")
def cfg():
    return load_config(PROJECT_DIR / "companion.toml")


@pytest.fixture
def system_vault(vault: Path) -> Vault:
    return Vault(vault, ("00_Inbox", "_System"))


# ------------------------------------------------------------------ window-title rules
@pytest.mark.parametrize("process,title,cls,app", [
    ("chrome.exe", "Meet - abc-defg-hij - Google Chrome", "", "Google Meet"),
    ("msedge.exe", "abc-defg-hij - Microsoft Edge", "", "Google Meet"),
    ("firefox.exe", "Meet - Weekly sync — Mozilla Firefox", "", "Google Meet"),
    ("Zoom.exe", "Zoom Meeting", "", "Zoom"),
    ("Zoom.exe", "Zoom Workplace - Meeting", "", "Zoom"),
    ("zoom.exe", "Zoom Workplace", "ConfMultiTabContentWndClass", "Zoom"),
    ("brave.exe", "Zoom Meeting - Brave", "", "Zoom (web)"),
    ("opera.exe", "Launch Meeting - Zoom - Opera", "", "Zoom (web)"),
    ("ms-teams.exe", "Meeting with Amra | Microsoft Teams", "", "Teams"),
    ("Teams.exe", "Call with Amra | Microsoft Teams", "", "Teams"),
])
def test_meeting_windows_match(cfg, process, title, cls, app):
    m = match_windows(cfg.rules, [Window(1, 1, "explorer.exe", "Program Manager"), Window(2, 2, process, title, cls)])
    assert m is not None and m.app == app and m.window.title == title


@pytest.mark.parametrize("process,title,cls", [
    ("chrome.exe", "Google Meet - Google Chrome", ""),                  # Meet landing page, no call
    ("chrome.exe", "zoom meeting - Google Search - Google Chrome", ""),
    ("chrome.exe", "Day-Long-Fun - YouTube - Google Chrome", ""),        # Meet codes are lower case
    ("Zoom.exe", "Zoom Workplace", "ZPPTMainFrmWndClassEx"),             # Zoom home window
    ("Zoom.exe", "Zoom", ""),
    ("ms-teams.exe", "Chat | Microsoft Teams", ""),
    ("ms-teams.exe", "Calendar | Microsoft Teams", ""),
    ("notepad.exe", "Meet - abc-defg-hij", ""),                          # not a browser
])
def test_other_windows_do_not_match(cfg, process, title, cls):
    assert match_windows(cfg.rules, [Window(1, 1, process, title, cls)]) is None


def test_rules_can_be_added_in_the_config_file(tmp_path):
    f = tmp_path / "companion.toml"
    f.write_text('[[rules]]\napp = "Whereby"\nprocesses = ["chrome.exe"]\ntitle = \'^Whereby - \'\n', encoding="utf-8")
    c = load_config(f)
    assert match_windows(c.rules, [Window(1, 1, "Chrome.exe", "Whereby - room - Google Chrome")]).app == "Whereby"
    f.write_text('[[rules]]\napp = "Bad"\nprocesses = ["x.exe"]\ntitle = "("\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="bad regex"):
        load_config(f)


def test_config_paths_expand_environment_variables(cfg):
    assert "%" not in str(cfg.screenshot_folder) and "%" not in str(cfg.meetily_database)
    assert cfg.poll_seconds == 3 and cfg.start_after_matches == 2 and cfg.end_grace_seconds == 60
    assert cfg.picker_hotkey == "ctrl+alt+c" and cfg.hotkey == "ctrl+alt+s"


def test_hotkey_format():
    assert pynput_hotkey("Ctrl+Alt+S") == "<ctrl>+<alt>+s"
    assert pynput_hotkey("win + shift + f9") == "<cmd>+<shift>+<f9>"


# ------------------------------------------------------------------ debounce (fake clock)
def meet(cfg) -> Match:
    return match_windows(cfg.rules, [Window(1, 1, "chrome.exe", "Meet - abc-defg-hij - Google Chrome")])


def test_meeting_starts_after_two_matches_and_survives_tab_switches(cfg):
    t, m = MeetingTracker(2, 60), meet(cfg)
    assert t.update(0, m) == []
    assert t.update(3, None) == []                       # a single sighting is not a meeting
    assert t.update(6, m) == []
    [ev] = t.update(9, m)
    assert isinstance(ev, Started) and ev.at == 6
    for now in range(12, 66, 3):                         # switched to another tab for 57 s
        assert t.update(now, None) == []
    assert t.update(66, m) == []                         # back on the Meet tab
    for now in range(69, 126, 3):
        assert t.update(now, None) == []
    [ev] = t.update(126, None)
    assert isinstance(ev, Ended) and ev.at == 66         # ends when it was last seen
    assert t.active is None


def test_audio_keeps_a_hidden_browser_meeting_alive(cfg):
    t, m = MeetingTracker(2, 60), meet(cfg)
    t.update(0, m)
    t.update(3, m)
    for now in range(6, 600, 3):
        assert t.update(now, None, still_audible=True) == []
    [ev] = t.update(660, None, still_audible=False)
    assert ev.at == 597


def test_companion_writes_session_and_toast(cfg, vault, settings, system_vault):
    store = SessionStore(vault / "_System/meetings", system_vault)
    shown, opened, audible = [], [], {"chrome.exe"}

    class FakeNotifier:
        def meeting(self, s):
            shown.append(s)

    c = Companion(settings, dataclasses.replace(cfg, screenshot_folder=vault / "nope"), system_vault, store,
                  notifier=FakeNotifier(), audio=lambda: audible, open_recorder=lambda: opened.append(1) or True)
    base = at(14).timestamp()
    loading_tab = [Window(1, 1, "chrome.exe", "Meet - Google Chrome")]
    meet_tab = [Window(1, 1, "chrome.exe", "Meet - abc-defg-hij - Google Chrome")]
    other_tab = [Window(1, 1, "chrome.exe", "Inbox - Gmail - Google Chrome")]
    c.tick(base, loading_tab)
    c.tick(base + 3, loading_tab)
    assert [s.app for s in shown] == ["Google Meet"]
    c.tick(base + 6, meet_tab)                            # the title settles once the call page has loaded
    c.record_clicked(shown[0])
    assert opened == [1]
    for k in range(3, 200):                               # Gmail tab in front, Meet audio still playing
        c.tick(base + 3 * k, other_tab)
    assert c.session is not None
    audible.clear()
    for k in range(200, 222):
        c.tick(base + 3 * k, other_tab)
    assert c.session is None

    [f] = (vault / "_System/meetings").glob("*.json")
    data = json.loads(f.read_text(encoding="utf-8"))
    start = to_local(base)
    assert f.name == f"{start:%Y-%m-%d %H-%M-%S} Google Meet.json"
    assert data["app"] == "Google Meet" and data["window_title"] == "Meet - abc-defg-hij - Google Chrome"
    assert data["recorded_prompt_clicked"] is True and data["screenshots"] == []
    assert data["start"] == start.isoformat() and data["start"][-6] in "+-"      # local time with UTC offset
    assert data["end"] == to_local(base + 3 * 199).isoformat()
    assert data["duration"] == 3 * 199


# ------------------------------------------------------------------ screenshots -> sessions
def test_screenshot_is_assigned_by_time():
    a = session(at(10), at(10, 30), sid="a")
    b = session(at(11), None, sid="b")
    assert session_for_time([a, b], at(10, 15), 120) is a
    assert session_for_time([a, b], at(10, 31, 59), 120) is a       # ended < 2 minutes ago
    assert session_for_time([a, b], at(10, 33), 120) is None
    assert session_for_time([a, b], at(11, 45), 120) is b           # still running
    assert session_for_time([a, b], at(9, 59), 120) is None


def test_screenshots_are_copied_and_logged(vault, tmp_path, system_vault):
    shots_dir = tmp_path / "Screenshots"
    old = png(shots_dir / "Screenshot old.png")
    store = SessionStore(vault / "_System/meetings", system_vault)
    sessions: list[Session] = []
    w = ScreenshotWatcher(shots_dir, vault / "_System/screenshots", system_vault, store, lambda: sessions,
                          catch_up_minutes=0)
    w.prime()

    # outside a meeting: copied and listed as unassigned
    free = png(shots_dir / "Screenshot 1.png")
    assert w.poll() == []                                 # first sighting: maybe still being written
    [entry] = w.poll()
    created = to_local(created_ts(free.stat()))
    copy = vault / "_System/screenshots" / f"{created:%Y-%m-%d}" / f"{created:%Y-%m-%d %H-%M-%S}.png"
    assert copy.is_file() and free.is_file() and old.is_file()       # originals are never moved
    assert entry["session"] is None and entry["path"] == copy.relative_to(vault).as_posix()

    # during a meeting: also added to the session file with its offset
    live = store.start("Google Meet", "Meet - abc-defg-hij", "chrome.exe", created - dt.timedelta(minutes=5))
    sessions.append(live)
    inside = png(shots_dir / "Screenshot 2.png", (10, 200, 10))
    w.poll()
    [entry2] = w.poll()
    offset = hms((to_local(created_ts(inside.stat())) - live.start).total_seconds())
    data = json.loads(live.path.read_text(encoding="utf-8"))
    [logged] = data["screenshots"]
    assert logged["offset"] == offset and logged["path"] == entry2["path"] and entry2["session"] == live.id
    assert logged["timestamp"][-6] in "+-"

    index = json.loads((vault / "_System/screenshots/index.json").read_text(encoding="utf-8"))["screenshots"]
    assert [e["session"] for e in index] == [None, live.id]
    assert w.poll() == []                                 # each file is taken once


# ------------------------------------------------------------------ sessions -> transcripts
def test_transcript_matches_the_session_with_most_overlap():
    first = session(at(14), at(14, 40), sid="first")
    second = session(at(14, 45), at(15, 30), sid="second")
    windows = transcript_windows(created=at(16), modified=at(15, 31), duration_seconds=45 * 60)
    assert match_session([first, second], "2026-09-30", windows, 15 * 60) is second
    assert match_session([first, second], "2026-10-01", windows, 15 * 60) is None


def test_transcript_session_tolerance():
    s = session(at(13), at(13, 30))
    windows = transcript_windows(created=at(18), modified=at(14, 10), duration_seconds=30 * 60)   # 13:40-14:10
    assert match_session([s], "2026-09-30", windows, 15 * 60) is s
    assert match_session([s], "2026-09-30", windows, 5 * 60) is None


# ------------------------------------------------------------------ pipeline with screenshots
def add_session(vault: Path) -> None:
    for name, c in (("2026-09-30 14-05-00.png", (200, 0, 0)), ("2026-09-30 14-40-00.png", (0, 0, 200))):
        png(vault / "_System/screenshots/2026-09-30" / name, c)
    data = {"id": "m1", "app": "Google Meet", "window_title": "Meet - abc-defg-hij",
            "start": "2026-09-30T14:00:00+02:00", "end": "2026-09-30T14:50:00+02:00", "duration": 3000,
            "recorded_prompt_clicked": True,
            "screenshots": [
                {"path": "_System/screenshots/2026-09-30/2026-09-30 14-05-00.png",
                 "timestamp": "2026-09-30T14:05:00+02:00", "offset": "00:05:00"},
                {"path": "_System/screenshots/2026-09-30/2026-09-30 14-40-00.png",
                 "timestamp": "2026-09-30T14:40:00+02:00", "offset": "00:40:00"}]}
    (vault / "_System/meetings").mkdir(parents=True, exist_ok=True)
    (vault / "_System/meetings" / SESSION_NAME).write_text(json.dumps(data), encoding="utf-8")


def drop_call(vault: Path, name="Acme - 2026-09-30 - Kickoff.txt") -> Path:
    f = vault / "00_Inbox" / name
    f.write_text(make_transcript(2500), encoding="utf-8")
    end = at(14, 48).timestamp()
    os.utime(f, (end, end))
    return f


def describe(system, user, image: ImageInput) -> str:
    assert image.media_type == "image/png" and image.data.startswith(b"\x89PNG")
    return json.dumps({"caption": "Pricing slide for the Q4 launch", "visible_text": "Basic 29 EUR\nPro 49 EUR",
                       "kind": "Slide"})


def part_with(vault: Path, needle: str) -> list[Path]:
    return [p for p in sorted((vault / "Clients/Acme/Transcripts/2026-09-30 Kickoff").glob("*.md"))
            if needle in p.read_text(encoding="utf-8")]


def test_call_with_screenshots(vault, settings):
    add_session(vault)
    drop_call(vault)
    llm = FakeLLM(fake_responder(), describe)
    [r] = Pipeline(settings, llm, concurrency=2).run_inbox()
    assert r.status == "processed", r.message

    text = r.call_note.read_text(encoding="utf-8")
    fm = get_frontmatter(text)
    assert fm["meeting_session"] == f"_System/meetings/{SESSION_NAME}" and fm["screenshot_count"] == 2
    assert fm["meeting_start"] == "2026-09-30T14:00:00+02:00" and fm["meeting_end"] == "2026-09-30T14:50:00+02:00"
    assert fm["duration"] == "00:50:00"
    section = get_section(text, "Screenshots")
    assert "- [00:05:00 into the call] Pricing slide for the Q4 launch" in section
    assert f"![[{ATTACH}/2026-09-30 14-05-00.png]]" in section
    assert "> [!note]- Text in image\n> Basic 29 EUR\n> Pro 49 EUR" in section
    assert text.index("## Screenshots") < text.index("## Transcript")

    for name in ("2026-09-30 14-05-00.png", "2026-09-30 14-40-00.png"):
        assert (vault / ATTACH / name).is_file()
        assert (vault / "_System/screenshots/2026-09-30" / name).is_file()          # originals kept

    # each screenshot sits in the transcript part covering its time
    line = f"📷 [[{ATTACH}/2026-09-30 14-05-00.png]] — Pricing slide for the Q4 launch (Basic 29 EUR Pro 49 EUR)"
    [p5] = part_with(vault, line)
    [p40] = part_with(vault, f"📷 [[{ATTACH}/2026-09-30 14-40-00.png]]")
    for part, sec in ((p5, 300), (p40, 2400)):
        pfm = get_frontmatter(part.read_text(encoding="utf-8"))
        assert sum(int(x) * k for x, k in zip(str(pfm["time_start"]).split(":"), (3600, 60, 1))) <= sec
    assert p5 != p40

    call_prompt = next(u for _, u in llm.calls if "Write the call note" in u)
    assert "- 00:05:00: Pricing slide for the Q4 launch; text: Basic 29 EUR Pro 49 EUR" in call_prompt
    assert all("00:05:00 into the call" in u or "00:40:00 into the call" in u for _, u, _ in llm.image_calls)
    rec = next(iter(json.loads((vault / "_System/ingest-log.json").read_text(encoding="utf-8")).values()))
    assert rec["meeting_session"] == f"_System/meetings/{SESSION_NAME}" and rec["screenshots"] == 2


def test_model_without_vision_still_attaches_screenshots(vault, settings):
    add_session(vault)
    drop_call(vault)
    llm = FakeLLM(fake_responder())                      # no vision
    [r] = Pipeline(settings, llm, concurrency=2).run_inbox()
    assert r.status == "processed", r.message
    text = r.call_note.read_text(encoding="utf-8")
    section = get_section(text, "Screenshots")
    assert "- [00:05:00 into the call] Screenshot" in section and "[!note]" not in section
    assert get_frontmatter(text)["screenshot_count"] == 2
    assert (vault / ATTACH / "2026-09-30 14-05-00.png").is_file()
    assert part_with(vault, f"📷 [[{ATTACH}/2026-09-30 14-05-00.png]] — Screenshot")
    call_prompt = next(u for _, u in llm.calls if "Write the call note" in u)
    assert "00:05:00: a screenshot was taken (no description available)" in call_prompt


def test_call_without_a_session_is_unchanged(vault, settings):
    drop_call(vault)
    [r] = Pipeline(settings, FakeLLM(fake_responder()), concurrency=2).run_inbox()
    text = r.call_note.read_text(encoding="utf-8")
    assert "## Screenshots" not in text and "📷" not in "".join(p.read_text(encoding="utf-8") for p in r.chunk_notes)
    assert not {"meeting_session", "screenshot_count", "meeting_start", "duration"} & set(get_frontmatter(text))
    assert not (vault / "Clients/Acme/Calls/attachments").exists()


def test_dry_run_mentions_the_session(vault, settings):
    add_session(vault)
    drop_call(vault)
    [r] = Pipeline(settings, None, dry_run=True).run_inbox()
    assert any("meeting session: 2026-09-30 14-00-00 Google Meet.json" in line and "2 screenshot(s)" in line
               for line in r.planned)
    assert not (vault / "Clients/Acme/Calls/attachments").exists()


def test_chunk_for_offset():
    chunks = [Chunk([Turn("A", "x", "00:00:00")]), Chunk([Turn("B", "y", "00:10:00")]),
              Chunk([Turn(None, "z", None)]), Chunk([Turn("A", "w", "00:30:00")])]
    assert [chunk_for_offset(chunks, s) for s in (0, 599, 600, 1500, 1800, 9999)] == [0, 0, 1, 1, 3, 3]


def test_speakerless_timed_lines_keep_times_in_every_part(tmp_path):
    f = tmp_path / "meetily.txt"
    f.write_text("\n".join(f"[{hms(i * 20)}] We looked at the Q4 launch plan and the numbers are tight today "
                           f"and the budget is still open for discussion." for i in range(250)), encoding="utf-8")
    turns = parse_file(f)
    assert len(turns) == 1                                               # merged, as before
    chunks = chunk_turns(turns, 600, 900, 100)
    starts = [c.start for c in chunks]
    assert len(chunks) >= 3 and all(starts) and starts == sorted(starts) and starts[0] == "00:00:00"
    assert all(c.end and c.end > c.start for c in chunks)


# ------------------------------------------------------------------ vision requests
def studio(vault, handler) -> LocalStudioLLM:
    s = Settings(vault_path=vault, prompts_dir=PROJECT_DIR / "prompts", llm_mode="local",
                 llm_provider="openai_compat", llm_model="gemma-4-12b")
    return LocalStudioLLM(s, auto_start=False, transport=httpx.MockTransport(handler))


def tiny_image(tmp_path) -> ImageInput:
    return ImageInput.from_file(png(tmp_path / "x.png"))


def test_lm_studio_sends_base64_image(vault, tmp_path):
    seen = {}

    def handler(req):
        body = json.loads(req.content)
        seen["content"] = body["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"caption": "A red square.", "visible_text": "", "kind": "other"})}}]})

    d = studio(vault, handler).complete_json("sys", "describe", ScreenshotDescription, image=tiny_image(tmp_path))
    assert d.caption == "A red square."
    text, image = seen["content"]
    assert text["type"] == "text" and image["type"] == "image_url"
    assert image["image_url"]["url"].startswith("data:image/png;base64,")


def test_lm_studio_text_only_model_raises_vision_unsupported(vault, tmp_path):
    def handler(req):
        return httpx.Response(400, json={"error": "Model does not support images. Please use a model that does."})

    llm = studio(vault, handler)
    with pytest.raises(VisionUnsupported):
        llm.complete_json("sys", "describe", ScreenshotDescription, image=tiny_image(tmp_path))
    assert llm.structured                                   # structured output is still used for text prompts


def test_big_screenshots_are_downscaled(tmp_path):
    p = tmp_path / "big.png"
    Image.new("RGB", (3840, 2160), (5, 5, 5)).save(p)
    img = ImageInput.from_file(p)
    with Image.open(__import__("io").BytesIO(img.data)) as im:
        assert max(im.size) == 1568


def test_screenshot_description_is_capped():
    d = ScreenshotDescription.model_validate({"caption": "c", "visible_text": " ".join(["w"] * 200), "kind": "poster"})
    assert d.visible_text.split() == ["w"] * 80 + ["…"] and d.kind == "other"


# ------------------------------------------------------------------ Meetily export
def meetily_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE meetings (id TEXT PRIMARY KEY, title TEXT, created_at TEXT, updated_at TEXT, folder_path TEXT);
        CREATE TABLE transcripts (id TEXT PRIMARY KEY, meeting_id TEXT, transcript TEXT, timestamp TEXT,
            summary TEXT, action_items TEXT, key_points TEXT, audio_start_time REAL, audio_end_time REAL,
            duration REAL, speaker TEXT);
        CREATE TABLE summary_processes (meeting_id TEXT PRIMARY KEY, status TEXT, created_at TEXT, updated_at TEXT,
            error TEXT, result TEXT, start_time TEXT, end_time TEXT, chunk_count INTEGER, processing_time REAL,
            metadata TEXT, result_backup TEXT, result_backup_timestamp TEXT);
    """)
    con.commit()
    con.close()
    return path


def add_meeting(db: Path, mid: str, title: str, segments, status=None) -> None:
    con = sqlite3.connect(db)
    con.execute("INSERT INTO meetings VALUES (?, ?, '2026-09-30T12:00:00+00:00', NULL, NULL)", (mid, title))
    for i, (start, speaker, text) in enumerate(segments):
        con.execute("INSERT INTO transcripts (id, meeting_id, transcript, timestamp, audio_start_time, speaker) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (f"{mid}-{i}", mid, text, f"t{i}", start, speaker))
    if status:
        con.execute("INSERT INTO summary_processes (meeting_id, status) VALUES (?, ?)", (mid, status))
    con.commit()
    con.close()


def digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_meetily_recording_is_exported_once_finished(vault, tmp_path, system_vault):
    db = meetily_db(tmp_path / "meeting_minutes.sqlite")
    add_meeting(db, "old", "Recorded before the companion", [(0.0, None, "Earlier call.")])
    clock = [1000.0]
    ex = MeetilyExporter(db, system_vault, vault / "00_Inbox", vault / "_System/meetily-exported.json", 120,
                         clock=lambda: clock[0])
    assert ex.poll() == []                                   # first run: existing recordings are left alone
    add_meeting(db, "m1", "Weekly sync", [(0.0, "", "Welcome everyone to the weekly sync."),
                                         (5.0, None, "We need to talk about\nthe budget."),
                                         (65.0, "amra", "The photos are booked.")])
    before = digest(db)
    assert ex.poll() == []
    clock[0] += 119
    assert ex.poll() == []                                   # not settled yet
    clock[0] += 2
    [out] = ex.poll()
    assert out == vault / "00_Inbox" / "Weekly sync - 2026-09-30.txt"
    assert out.read_text(encoding="utf-8").splitlines() == [
        "[00:00:00] Welcome everyone to the weekly sync.",
        "[00:00:05] We need to talk about the budget.",
        "[00:01:05] Amra: The photos are booked."]
    clock[0] += 1000
    assert ex.poll() == []                                   # exported once
    assert digest(db) == before                              # Meetily's database is never written
    state = json.loads((vault / "_System/meetily-exported.json").read_text(encoding="utf-8"))
    assert set(state["exported"]) == {"m1"} and state["baseline"] == ["old"]
    assert [t.start for t in parse_file(out)][0] == "00:00:00"

    add_meeting(db, "m2", "Acme - 2026-09-30 - Kickoff", [(0.0, None, "Hello.")], status="completed")
    [out2] = ex.poll()                                       # finished summary: no need to wait
    assert out2.name == "Acme - 2026-09-30 - Kickoff.txt"


def test_export_names():
    created = dt.datetime(2026, 9, 30, 12, tzinfo=dt.timezone.utc)
    assert export_name("Weekly sync", created) == "Weekly sync - 2026-09-30.txt"
    assert export_name("Acme - 2026-09-30 - Kickoff", created) == "Acme - 2026-09-30 - Kickoff.txt"
    assert export_name("a/b: c?", created) == "a b c - 2026-09-30.txt"
    assert export_name("  ", created).startswith("Meetily recording - ")


def test_title_and_date_only_name_asks_the_llm_for_the_client(vault, settings):
    f = vault / "00_Inbox" / "Weekly sync - 2026-09-30.txt"
    f.write_text(make_transcript(800), encoding="utf-8")
    llm = FakeLLM(fake_responder(client_guess=("Acme", 0.93)))
    [r] = Pipeline(settings, llm, concurrency=2).run_inbox()
    assert r.status == "processed" and r.client == "Acme"
    assert r.call_note == vault / "Clients/Acme/Calls/2026-09-30 Weekly sync.md"
    assert not (vault / "Clients/Weekly sync").exists()
    assert any("Which client is this call with" in u for _, u in llm.calls)
