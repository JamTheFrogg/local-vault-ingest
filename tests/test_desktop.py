"""Cross-platform pieces of the meeting companion: process names, paths, per-OS config, Linux audio streams,
window parsing, the game check and the helper-process protocol. None of this needs a display."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vault_ingest import desktop
from vault_ingest.desktop import Stream, Window, app_from_path, parse_wmctrl, pick_os_value, proc_key, pulse_streams
from vault_ingest.meeting_signals import MicUser, classify, mic_config_from_toml
from vault_ingest.watcher_meetings import GameProbe, Rule, load_config, match_windows, mic_match

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ names and paths
def test_process_names_compare_across_platforms():
    assert proc_key("Chrome.EXE") == proc_key("chrome") == "chrome"
    assert proc_key("zoom.us") == "zoom.us"
    assert proc_key("Google Chrome.app") == "google chrome"
    assert proc_key(None) == ""


def test_app_from_path_credits_helpers_to_their_bundle():
    helper = ("/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Helpers/"
              "Google Chrome Helper.app/Contents/MacOS/Google Chrome Helper")
    assert app_from_path(helper) == "Google Chrome"
    assert app_from_path("/usr/lib/discord/Discord") == "Discord"
    assert app_from_path(r"C:\Program Files\Zoom\Zoom.exe") == "Zoom.exe"


def test_windows_variables_work_on_macos_and_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop, "IS_WINDOWS", False)
    monkeypatch.setattr(desktop, "IS_MAC", False)
    monkeypatch.setattr(desktop, "_home", lambda: tmp_path)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert desktop.expand_path(r"%USERPROFILE%\Pictures\Screenshots") == tmp_path / "Pictures" / "Screenshots"
    assert desktop.expand_path(r"%appdata%\com.meetily.ai\x.sqlite") == tmp_path / ".local/share/com.meetily.ai/x.sqlite"
    monkeypatch.setattr(desktop, "IS_MAC", True)
    assert desktop.expand_path(r"%LOCALAPPDATA%\meetily") == tmp_path / "Library/Application Support/meetily"


def test_per_os_values():
    here = desktop.os_key()
    assert pick_os_value({here: "mine", "default": "other"}) == "mine"
    assert pick_os_value({"default": "fallback"}) == "fallback"
    assert pick_os_value({"plan9": "x"}, "builtin") == "builtin"
    assert pick_os_value("plain") == "plain"
    assert pick_os_value(None, 3) == 3


def test_companion_toml_loads_with_per_os_tables(tmp_path):
    cfg = load_config(ROOT / "companion.toml")
    assert cfg.meetily_paths and "%" not in str(cfg.screenshot_folder)
    if not desktop.IS_WINDOWS:
        assert not any(p.lower().endswith(".exe") for p in cfg.meetily_paths)
        assert cfg.phone_enabled is False                   # USB phone import is Windows-only


# ------------------------------------------------------------------ Linux streams and windows
PACTL_SOURCES = [
    {"index": 55, "name": "alsa_output.pci.analog-stereo.monitor", "monitor_of_sink": "54"},
    {"index": 56, "name": "alsa_input.pci.analog-stereo", "monitor_of_sink": "n/a"},
]
PACTL_OUTPUTS = [
    {"index": 1, "source": 56, "corked": False,
     "properties": {"application.name": "Chromium", "application.process.binary": "chrome",
                    "application.process.id": "0"}},
    {"index": 2, "source": 56, "corked": True,
     "properties": {"application.name": "Discord", "application.process.binary": "Discord"}},
    {"index": 3, "source": 55, "corked": False,                     # OBS recording the speakers
     "properties": {"application.name": "OBS", "application.process.binary": "obs"}},
]


def test_pulse_streams_skip_paused_and_monitor_recordings():
    streams = pulse_streams(PACTL_OUTPUTS, {"55"})
    assert [s.app for s in streams] == ["chrome"]


def test_linux_mic_users_match_the_shipped_mic_apps(monkeypatch):
    import tomllib

    raw = tomllib.loads((ROOT / "companion.toml").read_text(encoding="utf-8"))
    cfg = mic_config_from_toml(raw.get("microphone"), raw.get("mic_apps"))
    chrome = MicUser("/opt/google/chrome/chrome", process="chrome")
    discord = MicUser("/usr/share/discord/Discord", process="discord")
    zoom_mac = MicUser("/Applications/zoom.us.app/Contents/MacOS/zoom.us", process="zoom.us")
    teams_mac = MicUser("/Applications/Microsoft Teams.app/Contents/MacOS/MSTeams", process="microsoft teams")
    chrome_mac = MicUser("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", process="google chrome")
    assert classify(cfg, chrome).app == "Browser call"
    assert classify(cfg, chrome_mac).app == "Browser call"
    assert classify(cfg, discord).app == "Discord"
    assert classify(cfg, zoom_mac).app == "Zoom"
    assert classify(cfg, teams_mac).app == "Teams"
    obs = MicUser("/usr/bin/obs", process="obs")
    assert cfg.ignore.search(obs.id)


def test_a_linux_whatsapp_web_call_is_named_after_its_tab():
    import tomllib

    raw = tomllib.loads((ROOT / "companion.toml").read_text(encoding="utf-8"))
    cfg = mic_config_from_toml(raw.get("microphone"), raw.get("mic_apps"))
    windows = [Window(7, 10, "chrome", "WhatsApp - Chromium")]
    m = mic_match(cfg, [MicUser("/usr/lib/chromium/chromium", process="chrome")], windows)
    assert m is not None and m.app == "Browser call"


def test_window_rules_match_macos_and_linux_process_names():
    cfg = load_config(ROOT / "companion.toml")
    meet_mac = Window(1, 2, "Google Chrome", "Meet - abc-defg-hij")
    meet_linux = Window(1, 2, "chrome", "Meet - abc-defg-hij - Google Chrome")
    zoom_mac = Window(3, 4, "zoom.us", "Zoom Meeting")
    assert match_windows(cfg.rules, [meet_mac]).app == "Google Meet"
    assert match_windows(cfg.rules, [meet_linux]).app == "Google Meet"
    assert match_windows(cfg.rules, [zoom_mac]).app == "Zoom"
    rule = Rule(app="X", processes=frozenset({"chrome.exe"}))
    assert rule._keys == {"chrome"}


def test_wmctrl_output_is_parsed():
    text = ("0x03a00007  0 4242   google-chrome.Google-chrome  host Meet - abc-defg-hij - Google Chrome\n"
            "0x01000003 -1 1500   gnome-shell.Gnome-shell     host \n"
            "garbage\n")
    wins = parse_wmctrl(text, lambda pid: {4242: "chrome"}.get(pid, ""))
    assert wins == [Window(0x03A00007, 4242, "chrome", "Meet - abc-defg-hij - Google Chrome", "Google-chrome")]


def test_snip_tools_save_into_the_screenshot_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "IS_MAC", False)
    monkeypatch.setattr(desktop, "IS_LINUX", True)
    tools = desktop.screenshot_tools(tmp_path)
    assert tools and all(str(tmp_path) in " ".join(t) for t in tools)


# ------------------------------------------------------------------ games
def test_game_helpers_do_not_count_as_playing(monkeypatch):
    from vault_ingest.gpu import GpuRules, Proc, detect_busy

    rules = GpuRules()
    crash = Proc(1, "crashpad_handler.exe", r"D:\Steam\steamapps\common\ABI\crashpad_handler.exe")
    anticheat = Proc(2, "service.exe", r"D:\Steam\steamapps\common\ABI\ACE\service.exe")
    assert detect_busy([crash, anticheat], None, 0, False, rules) is None
    game = Proc(3, "UAGame.exe", r"D:\Steam\steamapps\common\ABI\UAGame.exe")
    assert detect_busy([crash, game], None, 0, False, rules).label == "UAGame.exe"
    linux_game = Proc(4, "hl2_linux", "/home/me/.local/share/Steam/steamapps/common/HL2/hl2_linux")
    assert detect_busy([linux_game], None, 0, False, rules).kind == "path"


def test_game_probe_needs_a_window_when_it_knows_the_windows(monkeypatch):
    from vault_ingest.gpu import GpuRules, Proc

    procs = [Proc(10, "UAGame.exe", r"D:\Steam\steamapps\common\ABI\UAGame.exe")]
    monkeypatch.setattr("vault_ingest.gpu.list_processes", lambda: procs)
    pids: set[int] = set()
    probe = GameProbe(GpuRules(), window_pids=lambda: pids)
    assert probe.running(0) is None                  # running in the background without a window
    pids.add(10)
    assert probe.running(31) == "UAGame.exe"
    assert GameProbe(GpuRules()).running(0) == "UAGame.exe"   # no window list: old behaviour


# ------------------------------------------------------------------ helper processes
def test_helper_protocol():
    from vault_ingest.ui_child import parse_event, pynput_hotkey

    assert parse_event("click 3\n") == ("click", "3")
    assert parse_event("hotkey ctrl+alt+c") == ("hotkey", "ctrl+alt+c")
    assert parse_event("noise") is None and parse_event("click ") is None
    assert pynput_hotkey("Ctrl+Alt+C") == "<ctrl>+<alt>+c"
    assert pynput_hotkey("command+option+s") == "<cmd>+<alt>+s"


def test_helper_rejects_unknown_modes():
    proc = subprocess.run([sys.executable, "-m", "vault_ingest.ui_child", "nope", "{}"], capture_output=True,
                          text=True, timeout=30, cwd=ROOT)
    assert proc.returncode == 2


def test_applescript_strings_are_escaped():
    from vault_ingest.notify import applescript_string

    assert applescript_string('say "hi" \\ bye') == '"say \\"hi\\" \\\\ bye"'


# ------------------------------------------------------------------ single instance
def test_a_reused_pid_does_not_block_the_companion(tmp_path):
    from vault_ingest.watcher_meetings import acquire_lock

    class V:
        def write(self, path, text):
            Path(path).write_text(text, encoding="utf-8")

    lock = tmp_path / "companion.lock"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        time.sleep(0.5)
        lock.write_text(f"{other.pid}\n", encoding="utf-8")    # a live process that is not the companion
        assert acquire_lock(V(), lock)
        assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())
        lock.write_text("999999\n", encoding="utf-8")         # a dead pid
        assert acquire_lock(V(), lock)
    finally:
        other.kill()


def test_a_live_companion_still_holds_the_lock(tmp_path):
    from vault_ingest.watcher_meetings import acquire_lock

    class V:
        def write(self, path, text):
            Path(path).write_text(text, encoding="utf-8")

    lock = tmp_path / "companion.lock"
    fake = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)", "-m", "vault_ingest", "companion"])
    try:
        time.sleep(0.5)
        lock.write_text(f"{fake.pid}\n", encoding="utf-8")
        assert not acquire_lock(V(), lock)
    finally:
        fake.kill()


@pytest.mark.parametrize("platform_flags", [(True, False, False), (False, True, False), (False, False, True)])
def test_defaults_exist_on_every_platform(monkeypatch, platform_flags):
    win, mac, linux = platform_flags
    monkeypatch.setattr(desktop, "IS_WINDOWS", win)
    monkeypatch.setattr(desktop, "IS_MAC", mac)
    monkeypatch.setattr(desktop, "IS_LINUX", linux)
    assert desktop.default_meetily_paths()
    assert desktop.default_meetily_database().name == "meeting_minutes.sqlite"
    assert desktop.ui_font()
    assert isinstance(Stream("a", 1, ""), Stream)
