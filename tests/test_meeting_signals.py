"""Microphone detection (WhatsApp and any other call app), participant screenshots, popup reopen."""
import dataclasses
import datetime as dt
import json
import time
from pathlib import Path

import pytest
from PIL import Image

from vault_ingest.clients import meeting_key
from vault_ingest.config import PROJECT_DIR
from vault_ingest.meeting_signals import MicUser, MicWatcher, classify, is_active
from vault_ingest.schemas import ScreenshotDescription
from vault_ingest.screenshots import ScreenshotWatcher
from vault_ingest.sessions import SessionStore
from vault_ingest.vault import Vault
from vault_ingest.watcher_meetings import Companion, Window, load_config, mic_match

WHATSAPP = MicUser("5319275A.WhatsAppDesktop_cv1g1gvanyjgm")
CHROME = MicUser(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
MEETILY = MicUser(r"C:\Users\Faris\AppData\Local\meetily\meetily.exe")
TEAMS = MicUser("MSTeams_8wekyb3d8bbwe")
UNKNOWN = MicUser(r"C:\Tools\SomeCaller\caller.exe")


@pytest.fixture(scope="module")
def cfg():
    return load_config(PROJECT_DIR / "companion.toml")


def test_registry_marks_an_app_active_while_stop_is_zero():
    assert is_active(133000000000000000, 0)
    assert not is_active(133000000000000000, 133000000000000500)
    assert not is_active(0, 0)


def test_known_apps_are_named_and_meetily_is_ignored(cfg):
    assert classify(cfg.mic, WHATSAPP).app == "WhatsApp"
    assert classify(cfg.mic, CHROME).app == "Browser call"
    assert classify(cfg.mic, TEAMS).app == "Teams"
    assert classify(cfg.mic, UNKNOWN).app == "Call (caller)"
    watcher = MicWatcher(cfg.mic, reader=lambda: [MEETILY], clock=lambda: 0)
    assert watcher(0) == [] and watcher(100) == []


def test_microphone_must_be_held_before_it_counts(cfg):
    users = [WHATSAPP]
    watcher = MicWatcher(cfg.mic, reader=lambda: users)
    assert watcher(0) == []
    assert watcher(10) == []                                 # a voice message
    assert watcher(cfg.mic.start_after_seconds) == [WHATSAPP]
    users.clear()
    assert watcher(30) == []


def test_whatsapp_call_is_named_after_the_contact_window(cfg):
    windows = [Window(5, 50, "WhatsApp.Root.exe", "WhatsApp"),
               Window(6, 50, "WhatsApp.Root.exe", "Dino Sušić"),
               Window(7, 70, "chrome.exe", "Inbox - Gmail - Google Chrome")]
    m = mic_match(cfg.mic, [WHATSAPP], windows)
    assert m.app == "WhatsApp" and m.window.title == "Dino Sušić" and m.window.process == "WhatsApp.Root.exe"
    assert meeting_key("WhatsApp", "Dino Sušić") == "whatsapp:dino susic"
    assert meeting_key("WhatsApp", "WhatsApp") is None


def test_browser_call_does_not_take_an_unrelated_tab_title(cfg):
    m = mic_match(cfg.mic, [CHROME], [Window(7, 70, "chrome.exe", "Inbox - Gmail - Google Chrome")])
    assert m.app == "Browser call" and m.window.title == "Browser call" and m.window.process == "chrome.exe"


def test_listed_apps_win_over_unknown_ones(cfg):
    assert mic_match(cfg.mic, [UNKNOWN, WHATSAPP], []).app == "WhatsApp"


def test_companion_starts_a_whatsapp_meeting_from_the_microphone(cfg, vault, settings):
    system_vault = Vault(vault, ("00_Inbox", "_System"))
    store = SessionStore(vault / "_System/meetings", system_vault)
    users, shown = [WHATSAPP], []
    watcher = MicWatcher(cfg.mic, reader=lambda: users)
    c = Companion(settings, dataclasses.replace(cfg, screenshot_folder=vault / "nope"), system_vault, store,
                  on_started=shown.append, mic=watcher)
    windows = [Window(6, 50, "WhatsApp.Root.exe", "Amra")]
    base = time.time()
    for k in range(0, 10):                                    # 27 s of the microphone being held
        c.tick(base + 3 * k, windows)
    assert [s.app for s in shown] == ["WhatsApp"] and shown[0].window_title == "Amra"
    assert c.latest_session() is shown[0]
    users.clear()
    for k in range(10, 40):
        c.tick(base + 3 * k, windows)
    assert c.session is None
    assert c.latest_session() is shown[0]                     # the tray icon can still reopen it


def test_participant_capture_is_attached_to_the_meeting(vault, tmp_path):
    from vault_ingest.sessions import Session, to_local

    system_vault = Vault(vault, ("00_Inbox", "_System"))
    store = SessionStore(vault / "_System/meetings", system_vault)
    session = store.start("Teams", "Meeting with Amra | Microsoft Teams", "ms-teams.exe",
                          to_local(time.time() - 60))
    shots = ScreenshotWatcher(tmp_path / "snips", vault / "_System/screenshots", system_vault, store,
                              lambda: [session])
    capture = tmp_path / "capture.png"
    Image.new("RGB", (40, 30), (10, 10, 200)).save(capture)
    entry = shots.add_capture(capture)
    assert entry["kind"] == "participants" and entry["session"] == session.id
    data = json.loads(session.path.read_text(encoding="utf-8"))
    assert data["screenshots"][0]["kind"] == "participants"
    assert data["screenshots"][0]["path"].endswith("participants.png")


def test_people_are_cleaned():
    d = ScreenshotDescription(caption="Teams grid", people=["Dino Sušić", " dino  sušić ", "", "Amer"])
    assert d.people == ["Dino Sušić", "Amer"]
    assert ScreenshotDescription(caption="x", people="Dino, Amer; Indira").people == ["Dino", "Amer", "Indira"]
    assert ScreenshotDescription(caption="x").people == []


def test_microphone_does_not_start_a_meeting_while_a_game_runs(cfg, vault, settings):
    system_vault = Vault(vault, ("00_Inbox", "_System"))
    store = SessionStore(vault / "_System/meetings", system_vault)
    users, shown = [WHATSAPP], []
    game = {"name": "UAGame.exe"}
    watcher = MicWatcher(cfg.mic, reader=lambda: users)
    c = Companion(settings, dataclasses.replace(cfg, screenshot_folder=vault / "nope"), system_vault, store,
                  on_started=shown.append, mic=watcher, gaming=lambda now: game["name"])
    windows = [Window(6, 50, "WhatsApp.Root.exe", "Amra")]
    base = time.time()
    for k in range(0, 20):
        c.tick(base + 3 * k, windows)
    assert shown == [] and c.session is None
    game["name"] = None                                       # game closed: the call counts again
    for k in range(20, 30):
        c.tick(base + 3 * k, windows)
    assert [s.app for s in shown] == ["WhatsApp"]


def test_game_probe_uses_the_gpu_rules(monkeypatch):
    from vault_ingest.gpu import GpuRules, Proc
    from vault_ingest.watcher_meetings import GameProbe

    procs = [Proc(1, "UAGame.exe", r"C:\Program Files (x86)\Steam\steamapps\common\ABInfinite\UAGame.exe")]
    monkeypatch.setattr("vault_ingest.gpu.list_processes", lambda: procs)
    probe = GameProbe(GpuRules())
    assert probe.running(0) == "UAGame.exe"
    procs.clear()
    assert probe.running(10) == "UAGame.exe"                  # cached for 30 s
    assert probe.running(31) is None
