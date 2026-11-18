"""Meeting companion: detect meetings from window titles and from which app holds the microphone (any call
app, WhatsApp included), offer to record with Meetily, take a participant screenshot when Record is clicked,
log sessions, collect screenshots and export finished Meetily transcripts into the inbox."""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import desktop
from .clients import meeting_key
from .config import PROJECT_DIR, Settings
from .desktop import AudioProbe, Window, expand_path, pick_os_value, proc_key
from .gpu import GpuRules, rules_from_toml
from .meeting_signals import MicConfig, MicUser, call_title, classify, mic_config_from_toml
from .screenshots import ScreenshotWatcher
from .sessions import Session, SessionStore, now_local, to_local
from .vault import Vault

log = logging.getLogger("vault_ingest")

DEFAULT_CONFIG = PROJECT_DIR / "companion.toml"
HEARTBEAT_SECONDS = 60
RECENT_SESSION_HOURS = 24


# ------------------------------------------------------------------ config
@dataclass
class Rule:
    app: str
    processes: frozenset[str]
    title: re.Pattern | None = None
    class_re: re.Pattern | None = None
    exclude: re.Pattern | None = None
    _keys: frozenset[str] = field(default=frozenset(), init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # "chrome.exe" in a rule also matches "chrome" on Linux; macOS names ("Google Chrome") are listed too
        self._keys = frozenset(proc_key(p) for p in self.processes)

    def matches(self, w: Window) -> bool:
        if proc_key(w.process) not in self._keys:
            return False
        if self.title is not None and not self.title.search(w.title):
            return False
        if self.class_re is not None and not self.class_re.search(w.class_name):
            return False
        return not (self.exclude is not None and self.exclude.search(w.title))


@dataclass
class Match:
    rule: Rule
    window: Window

    @property
    def app(self) -> str:
        return self.rule.app


@dataclass
class CompanionConfig:
    rules: list[Rule]
    poll_seconds: float = 3.0
    start_after_matches: int = 2
    end_grace_seconds: float = 60.0
    audio_keeps_meeting_alive: bool = True
    popup_enabled: bool = True
    picker_hotkey: str = "ctrl+alt+c"
    meetily_paths: list[str] = field(default_factory=desktop.default_meetily_paths)
    screenshot_folder: Path = field(default_factory=desktop.default_screenshot_folder)
    screenshot_extensions: tuple[str, ...] = (".png", ".jpg", ".jpeg")
    attach_after_meeting_seconds: float = 120.0
    catch_up_minutes: float = 10.0
    hotkey: str = "ctrl+alt+s"
    meetily_export: bool = True
    meetily_database: Path = field(default_factory=desktop.default_meetily_database)
    meetily_poll_seconds: float = 30.0
    meetily_settle_seconds: float = 120.0
    recordings_folder: Path = field(default_factory=desktop.default_recordings_folder)
    audio_settle_seconds: float = 30.0
    gpu: GpuRules = field(default_factory=GpuRules)
    phone_enabled: bool = desktop.IS_WINDOWS   # USB phone import uses Windows' portable-device API
    phone_poll_seconds: float = 5.0
    phone_device_name: str = "Faris's A33"
    phone_usb_id: str = r"USB\VID_04E8&PID_6860"
    phone_folder: str = "Internal storage/Recordings/Voice Recorder"
    phone_settle_seconds: float = 30.0
    mic: MicConfig = field(default_factory=MicConfig)
    participant_screenshot: bool = True
    participant_followup_seconds: float = 120.0


def _compile(pattern: str | None, where: str) -> re.Pattern | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.I)
    except re.error as e:
        raise SystemExit(f"companion.toml: bad regex in {where}: {pattern!r} ({e})") from e


def load_config(path: str | Path | None = None) -> CompanionConfig:
    path = Path(path) if path else DEFAULT_CONFIG
    if not path.is_file():
        raise SystemExit(f"Companion config not found: {path}")
    with open(path, "rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"{path} is not valid TOML: {e}") from e

    rules = []
    for i, r in enumerate(raw.get("rules") or [], 1):
        where = f"rule {i} ({r.get('app', '?')})"
        if not r.get("processes") or not (r.get("title") or r.get("class")):
            raise SystemExit(f"companion.toml: {where} needs `processes` and a `title` or `class` pattern")
        rules.append(Rule(app=str(r.get("app") or "Meeting"),
                          processes=frozenset(p.lower() for p in r["processes"]),
                          title=_compile(r.get("title"), where), class_re=_compile(r.get("class"), where),
                          exclude=_compile(r.get("exclude"), where)))
    det, pop, shots, mt = (raw.get(k) or {} for k in ("detection", "popup", "screenshots", "meetily"))
    cfg = CompanionConfig(rules=rules)
    cfg.poll_seconds = float(det.get("poll_seconds", cfg.poll_seconds))
    cfg.start_after_matches = max(1, int(det.get("start_after_matches", cfg.start_after_matches)))
    cfg.end_grace_seconds = float(det.get("end_grace_seconds", cfg.end_grace_seconds))
    cfg.audio_keeps_meeting_alive = bool(det.get("audio_keeps_meeting_alive", cfg.audio_keeps_meeting_alive))
    cfg.popup_enabled = bool(pop.get("enabled", cfg.popup_enabled))
    cfg.picker_hotkey = str(pick_os_value(pop.get("picker_hotkey"), cfg.picker_hotkey) or "")
    paths = pick_os_value(pop.get("meetily_paths"), None)
    if paths:
        cfg.meetily_paths = [str(p) for p in ([paths] if isinstance(paths, str) else paths)]
    folder = pick_os_value(shots.get("folder"), None)
    if folder:
        cfg.screenshot_folder = expand_path(folder)
    cfg.screenshot_extensions = tuple(e.lower() for e in shots.get("extensions", cfg.screenshot_extensions))
    cfg.attach_after_meeting_seconds = float(shots.get("attach_after_meeting_seconds",
                                                       cfg.attach_after_meeting_seconds))
    cfg.catch_up_minutes = float(shots.get("catch_up_minutes", cfg.catch_up_minutes))
    cfg.hotkey = str(pick_os_value(shots.get("hotkey"), cfg.hotkey) or "")
    cfg.meetily_export = bool(mt.get("export", cfg.meetily_export))
    database = pick_os_value(mt.get("database"), None)
    if database:
        cfg.meetily_database = expand_path(database)
    cfg.meetily_poll_seconds = float(mt.get("poll_seconds", cfg.meetily_poll_seconds))
    cfg.meetily_settle_seconds = float(mt.get("settle_seconds", cfg.meetily_settle_seconds))
    recordings = pick_os_value(mt.get("recordings"), None)
    if recordings:
        cfg.recordings_folder = expand_path(recordings)
    cfg.audio_settle_seconds = float(mt.get("audio_settle_seconds", cfg.audio_settle_seconds))
    cfg.gpu = rules_from_toml(raw.get("GPU_BUSY_RULES"))
    phone = raw.get("phone") or {}
    if "enabled" in phone:
        cfg.phone_enabled = bool(phone["enabled"]) and desktop.IS_WINDOWS
    cfg.phone_poll_seconds = float(phone.get("poll_seconds", cfg.phone_poll_seconds))
    if phone.get("device_name"):
        cfg.phone_device_name = str(phone["device_name"])
    if phone.get("usb_id"):
        cfg.phone_usb_id = str(phone["usb_id"])
    if phone.get("folder"):
        cfg.phone_folder = str(phone["folder"])
    cfg.phone_settle_seconds = float(phone.get("settle_seconds", cfg.phone_settle_seconds))
    cfg.mic = mic_config_from_toml(raw.get("microphone"), raw.get("mic_apps"))
    cfg.participant_screenshot = bool(pop.get("participant_screenshot", cfg.participant_screenshot))
    cfg.participant_followup_seconds = float(pop.get("participant_followup_seconds",
                                                     cfg.participant_followup_seconds))
    return cfg


def match_windows(rules: list[Rule], windows: Iterable[Window]) -> Match | None:
    windows = list(windows)
    for rule in rules:
        for w in windows:
            if rule.matches(w):
                return Match(rule, w)
    return None


def mic_match(cfg: MicConfig, users: Iterable[MicUser], windows: Iterable[Window]) -> Match | None:
    """A call found through the microphone, named after the app's window (a WhatsApp call window carries the
    contact's name). Listed apps win over "any app"; the first user in a group wins."""
    windows = list(windows)
    found = [(app, u) for u in users if (app := classify(cfg, u)) is not None]
    if not found:
        return None
    found.sort(key=lambda pair: pair[0].app.startswith("Call ("))
    app, user = found[0]
    title, w = call_title(app, windows, user.exe)
    process = w.process if w is not None else (user.exe or (app.processes[0] if app.processes else app.app))
    rule = Rule(app=app.app, processes=frozenset(proc_key(p) for p in (app.processes or (process,))))
    window = w if w is not None else Window(0, 0, process, title)
    if w is not None and window.title != title:
        window = Window(w.hwnd, w.pid, w.process, title, w.class_name)
    return Match(rule, window)


# ------------------------------------------------------------------ debounce
@dataclass
class Started:
    match: Match
    at: float              # time of the first matching poll


@dataclass
class Ended:
    match: Match
    at: float              # last time the meeting was seen


class MeetingTracker:
    """Starts after `start_after` matching polls in a row; ends `end_grace` seconds after the last sighting."""

    def __init__(self, start_after: int = 2, end_grace: float = 60.0):
        self.start_after = start_after
        self.end_grace = end_grace
        self.active: Match | None = None
        self.hits = 0
        self.first_hit = 0.0
        self.last_seen = 0.0

    def update(self, now: float, match: Match | None, still_audible: bool = False) -> list[Started | Ended]:
        if self.active is None:
            if match is None:
                self.hits = 0
                return []
            if self.hits == 0:
                self.first_hit = now
            self.hits += 1
            if self.hits < self.start_after:
                return []
            self.active, self.last_seen, self.hits = match, now, 0
            return [Started(match, self.first_hit)]
        if match is not None or still_audible:
            self.last_seen = now
            return []
        if now - self.last_seen >= self.end_grace:
            ended = Ended(self.active, self.last_seen)
            self.active = None
            return [ended]
        return []


# ------------------------------------------------------------------ Windows: windows, audio, focus
def WindowLister():
    """Visible top-level windows with a title, for this platform (see `desktop.make_window_lister`)."""
    return desktop.make_window_lister()


def find_meetily(paths: Iterable[str]) -> Path | None:
    """The first Meetily that exists: an .exe, a macOS .app bundle (a folder) or a Linux executable."""
    for p in paths:
        exe = expand_path(p)
        if exe.is_file() or (exe.suffix.lower() == ".app" and exe.is_dir()):
            return exe
    return None


def is_meetily_window(w: Window, exe: Path | None) -> bool:
    if desktop.IS_WINDOWS:
        name = (exe.name if exe else "meetily.exe").lower()
        return w.process.lower() == name and w.class_name == "Tauri Window"
    return proc_key(w.process) == proc_key(exe.stem if exe else "meetily")


def open_meetily(paths: Iterable[str], windows=None) -> bool:
    """Bring Meetily to the front, or start it. Never clicks anything inside Meetily."""
    exe = find_meetily(paths)
    if windows is not None:
        own = [w for w in windows.list(visible_only=False) if is_meetily_window(w, exe)]
        if own and windows.focus(own[0]):
            log.info("Brought Meetily to the front.")
            return True
    if exe and desktop.launch_app(exe):          # Meetily is single-instance: this also focuses a running one
        log.info("Started %s", exe)
        return True
    log.warning("Meetily not found. Set [popup] meetily_paths in companion.toml.")
    return False


def open_snip_overlay(folder: Path | None = None) -> None:
    """Win+Shift+S on Windows; on macOS/Linux a region snip saved into the screenshot folder."""
    desktop.open_snip_tool(folder or desktop.default_screenshot_folder())


from .ui_child import pynput_hotkey  # noqa: E402  (one combo parser for both)


def start_hotkeys(combos: dict[str, Callable[[], None]]):
    mapping = {pynput_hotkey(combo): action for combo, action in combos.items() if combo}
    if not mapping:
        return None
    if desktop.IS_MAC:
        # pynput's macOS listener must not share a process with Tk; run it in a helper process
        try:
            from .ui_child import ChildHotkeys

            listener = ChildHotkeys({c: a for c, a in combos.items() if c})
            log.info("Hotkeys: %s (macOS asks once for Accessibility/Input Monitoring permission).",
                     ", ".join(combo for combo in combos if combo))
            return listener
        except Exception as e:  # noqa: BLE001
            log.warning("Could not register hotkeys (%s): %s", ", ".join(combos), e)
            return None
    try:
        from pynput import keyboard

        listener = keyboard.GlobalHotKeys(mapping)
        listener.daemon = True
        listener.start()
        log.info("Hotkeys: %s.", ", ".join(combo for combo in combos if combo))
        return listener
    except Exception as e:  # noqa: BLE001
        log.warning("Could not register hotkeys (%s): %s", ", ".join(combos), e)
        return None


def start_hotkey(combo: str, action: Callable[[], None] = open_snip_overlay):
    return start_hotkeys({combo: action} if combo else {})


class Notifier:
    """Windows toast with "Record with Meetily" / "Ignore" buttons (callbacks work while this process runs)."""

    def __init__(self, on_record: Callable[[Session], None]):
        if not desktop.IS_WINDOWS:
            raise RuntimeError("Record buttons in notifications are Windows-only")
        from windows_toasts import InteractableWindowsToaster

        self.on_record = on_record
        self.toaster = InteractableWindowsToaster("Meeting companion")
        self._shown: list = []                       # keep toasts alive so their callbacks fire

    def meeting(self, session: Session) -> None:
        from windows_toasts import Toast, ToastButton

        text = f"Meeting detected: {session.app} — {session.window_title}"
        if len(text) > 160:
            text = text[:157] + "…"

        def activated(args):
            if getattr(args, "arguments", "") == "record":
                self.on_record(session)

        toast = Toast([text], on_activated=activated)
        try:
            toast.AddAction(ToastButton("Record with Meetily", "record"))
            toast.AddAction(ToastButton("Ignore", "ignore"))
        except Exception as e:  # noqa: BLE001  no buttons: clicking the toast itself opens Meetily
            log.info("Toast buttons unavailable (%s); clicking the toast opens Meetily.", e)
            toast = Toast([text], on_activated=lambda _args: self.on_record(session))
        self._shown = self._shown[-9:] + [toast]
        self.toaster.show_toast(toast)


# ------------------------------------------------------------------ game check
class GameProbe:
    """Name of a running game (GPU pause rules: Steam/Epic/GOG/Xbox folders, listed names), checked at most
    every 30 seconds.

    With `window_pids`, a game only counts while one of its processes has a window open. A game's crash
    reporter or anti-cheat service (crashpad_handler.exe, service.exe...) keeps running from the game folder
    after the game is closed; that is not someone playing, so a WhatsApp or Discord call still counts."""

    def __init__(self, rules: GpuRules, every: float = 30.0,
                 window_pids: Callable[[], set[int]] | None = None):
        self.rules, self.every = rules, every
        self.window_pids = window_pids
        self._at = -1e18
        self._game: str | None = None

    def running(self, now: float) -> str | None:
        if now - self._at >= self.every:
            from .gpu import detect_busy, list_processes
            procs = list_processes()
            if self.window_pids is not None:
                try:
                    pids = self.window_pids()
                except Exception:  # noqa: BLE001
                    pids = None
                if pids is not None:
                    procs = [p for p in procs if p.pid in pids]
            hit = detect_busy(procs, None, None, True, self.rules)
            self._game = hit.label if hit is not None else None
            self._at = now
        return self._game


# ------------------------------------------------------------------ companion
class Companion:
    def __init__(self, s: Settings, cfg: CompanionConfig, vault: Vault, store: SessionStore,
                 notifier=None, audio: Callable[[], set[str]] | None = None,
                 open_recorder: Callable[[], bool] | None = None, clock: Callable[[], float] = time.time,
                 on_started: Callable[[Session], None] | None = None,
                 on_retitle: Callable[[Session], None] | None = None,
                 mic: Callable[[float], list[MicUser]] | None = None,
                 gaming: Callable[[float], str | None] | None = None):
        self.s, self.cfg, self.vault, self.store = s, cfg, vault, store
        self.mic = mic
        self.gaming = gaming
        self._gaming_logged: str | None = None
        self.notifier = notifier
        self.audio = audio
        self.open_recorder = open_recorder
        self.on_started = on_started
        self.on_retitle = on_retitle
        self.clock = clock
        self.tracker = MeetingTracker(cfg.start_after_matches, cfg.end_grace_seconds)
        self.session: Session | None = None
        self.sessions: list[Session] = []
        self.last_windows: list[Window] = []
        self._last_beat = 0.0
        self.shots = ScreenshotWatcher(cfg.screenshot_folder, s.system / "screenshots", vault, store,
                                       lambda: list(self.sessions), cfg.screenshot_extensions,
                                       cfg.attach_after_meeting_seconds, cfg.catch_up_minutes, clock)

    def load_recent(self) -> None:
        today = dt.date.today()
        days = [(today - dt.timedelta(days=d)).isoformat() for d in (0, 1)]
        self.sessions = self.store.load(days)
        closed = self.store.close_stale(self.sessions)
        if closed:
            log.info("Closed %d meeting session(s) left open by an earlier run.", closed)

    def record_clicked(self, session: Session) -> None:
        self.store.update(session, recorded_prompt_clicked=True)
        log.info("Record with Meetily clicked for %s.", session.app)
        if self.open_recorder:
            self.open_recorder()

    def tick(self, now: float, windows: list[Window]) -> list[Started | Ended]:
        self.last_windows = windows
        m = match_windows(self.cfg.rules, windows)
        if m is None and self.mic is not None:
            try:
                m = mic_match(self.cfg.mic, self.mic(now), windows)
            except Exception:  # noqa: BLE001  the microphone check must never stop window detection
                log.exception("Microphone check failed")
            if m is not None and self.cfg.mic.skip_while_gaming and self.gaming is not None:
                game = None
                try:
                    game = self.gaming(now)
                except Exception:  # noqa: BLE001
                    log.exception("Game check failed")
                if game:
                    if self._gaming_logged != game:
                        log.info("Microphone in use by %s while %s is running; game voice chat, not a meeting.",
                                 m.app, game)
                        self._gaming_logged = game
                    m = None
                else:
                    self._gaming_logged = None
        audible = False
        if self.tracker.active is not None and m is None and self.cfg.audio_keeps_meeting_alive and self.audio:
            audible = proc_key(self.tracker.active.window.process) in {proc_key(n) for n in self.audio()}
        events = self.tracker.update(now, m, audible)
        for ev in events:
            if isinstance(ev, Started):
                w = ev.match.window
                self.session = self.store.start(ev.match.app, w.title, w.process, to_local(ev.at),
                                                meeting_key=meeting_key(ev.match.app, w.title))
                self.sessions.append(self.session)
                self._last_beat = now
                log.info("Meeting started: %s — %s", ev.match.app, w.title)
                if self.on_started is not None:
                    try:
                        self.on_started(self.session)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Could not show the client picker: %s", e)
                elif self.notifier is not None:
                    try:
                        self.notifier.meeting(self.session)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Could not show the meeting popup: %s", e)
            elif self.session is not None:
                self.store.update(self.session, end=to_local(ev.at), last_seen=to_local(ev.at))
                log.info("Meeting ended: %s (%ss)", self.session.app, self.session.duration)
                self.session = None
        # pages like Meet show a generic title while loading; keep the one the meeting settles on
        if (not events and self.session is not None and m is not None and m.app == self.session.app
                and m.window.title != self.session.window_title):
            key = meeting_key(m.app, m.window.title)
            changes = {"window_title": m.window.title}
            if key:
                changes["meeting_key"] = key
            self.store.update(self.session, **changes)
            if key and self.on_retitle is not None:
                try:
                    self.on_retitle(self.session)
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not update the client picker: %s", e)
        if self.session is not None and now - self._last_beat >= HEARTBEAT_SECONDS:
            self.store.update(self.session, last_seen=to_local(self.tracker.last_seen))
            self._last_beat = now
        cutoff = to_local(now) - dt.timedelta(hours=RECENT_SESSION_HOURS)
        self.sessions = [x for x in self.sessions if x is self.session or x.effective_end >= cutoff]
        return events

    def latest_session(self, within_minutes: float = 30) -> Session | None:
        """The meeting going on now, else the one that ended in the last `within_minutes` (for reopening the
        popup after it was closed by accident)."""
        if self.session is not None:
            return self.session
        cutoff = now_local() - dt.timedelta(minutes=within_minutes)
        recent = [x for x in self.sessions if x.effective_end >= cutoff]
        return max(recent, key=lambda x: x.effective_end) if recent else None

    def stop(self) -> None:
        if self.session is not None:
            self.store.update(self.session, end=to_local(self.tracker.last_seen))
            log.info("Companion stopping; closed the open meeting session.")
            self.session = None


# ------------------------------------------------------------------ single instance
def acquire_lock(vault: Vault, path: Path) -> bool:
    if path.exists():
        try:
            pid = int(path.read_text(encoding="utf-8").split()[0])
            if pid != os.getpid() and _lock_owner_is_companion(pid, path):
                return False
        except (ValueError, IndexError, OSError):
            pass
        log.info("Taking over a stale companion lock (%s).", path)
    vault.write(path, f"{os.getpid()}\n")
    return True


def _lock_owner_is_companion(pid: int, path: Path) -> bool:
    """True only if `pid` is still the companion that wrote `path`.

    After a reboot Windows hands the old PID to some other process. That process
    can be protected (AccessDenied) or have started after the lock was written;
    either way it is not our companion, so the lock is stale.
    """
    import psutil

    try:
        if not psutil.pid_exists(pid):
            return False
        proc = psutil.Process(pid)
        # a process that started after the lock was written cannot have written it
        if proc.create_time() > path.stat().st_mtime + 5:
            return False
        cmd = " ".join(proc.cmdline()).lower()
    except (psutil.Error, OSError):
        return False
    return "vault_ingest" in cmd and "companion" in cmd


PICKER_RETRY_SECONDS = 30
LM_RETRY_SECONDS = 30


class PickerStartup:
    """Open the client picker. If the window is not ready, try again instead of giving up for the session."""

    def __init__(self, factory, wire):
        self.factory = factory
        self.wire = wire
        self.window = None
        self.next_try = 0.0
        self.ready = False

    def poll(self, now: float) -> bool:
        if self.ready:
            return True
        window = self.window
        if window is not None and not window.failed:
            if window.ready.is_set():
                self._become_ready(window)
            return self.ready
        if now < self.next_try:
            return False
        try:
            window = self.factory()
        except Exception as e:  # noqa: BLE001
            log.warning("Client picker unavailable (%s); trying again later.", e)
            self.window = None
            self.next_try = now + PICKER_RETRY_SECONDS
            return False
        self.window = window
        if getattr(window, "failed", False):
            log.warning("Client picker unavailable (the client picker window did not start); trying again later.")
            self.next_try = now + PICKER_RETRY_SECONDS
            return False
        if not window.ready.is_set():
            log.info("Client picker is still starting.")
            return False
        self._become_ready(window)
        return True

    def _become_ready(self, window) -> None:
        self.ready = True
        self.wire(window)
        log.info("Client picker ready.")


def run_companion(s: Settings, cfg: CompanionConfig, hotkey: bool = True) -> int:
    vault = Vault(s.vault_path, (s.inbox_dir, s.system_dir))
    if not acquire_lock(vault, s.system / "companion.lock"):
        log.error("The meeting companion is already running (see %s).", s.system / "companion.lock")
        return 1
    from .gpu import show_toast
    from .meeting_signals import MicWatcher, capture_desktop, foreground_process

    store = SessionStore(s.system / "meetings", vault)
    lister = WindowLister()
    audio = AudioProbe()
    mic = MicWatcher(cfg.mic) if cfg.mic.enabled else None
    companion = None
    games = GameProbe(cfg.gpu, window_pids=lambda: {w.pid for w in companion.last_windows} if companion else None)
    companion = Companion(s, cfg, vault, store, audio=audio.playing,
                          open_recorder=lambda: open_meetily(cfg.meetily_paths, lister), mic=mic,
                          gaming=games.running)
    companion.load_recent()
    companion.shots.prime()
    capture_dir = PROJECT_DIR / "state" / "captures"

    def participant_shot(label: str = "participants") -> None:
        """Whole-desktop screenshot, attached to the meeting so the vision model can read who is in it."""
        if not cfg.participant_screenshot:
            return
        try:
            path = capture_desktop(capture_dir, label)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not take the participant screenshot: %s", e)
            return
        try:
            companion.shots.add_capture(path, kind="participants")
        except Exception:  # noqa: BLE001
            log.exception("Could not file the participant screenshot")
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    def followup_shot(session) -> None:
        """People who join late: one more screenshot, only if the meeting window is in front."""
        if companion.session is not session:
            return
        front = foreground_process()
        if session.process and front != proc_key(session.process):
            log.info("No follow-up participant screenshot: %s is in front, not %s.", front or "?", session.process)
            return
        participant_shot()

    def record(session) -> None:
        participant_shot()                    # before Meetily comes to the front and covers the meeting
        if session is not None:
            companion.record_clicked(session)
            if cfg.participant_screenshot and cfg.participant_followup_seconds > 0:
                timer = threading.Timer(cfg.participant_followup_seconds, followup_shot, args=(session,))
                timer.daemon = True
                timer.start()
        elif companion.open_recorder:
            companion.open_recorder()

    reopen_hint = "click the Vault ingest tray icon" + (f" or press {cfg.picker_hotkey}" if cfg.picker_hotkey else "")

    def announce(session) -> None:
        show_toast(f"{session.app} meeting detected. Pick the client in the popup (bottom right); "
                   f"closed it? {reopen_hint[0].upper()}{reopen_hint[1:]}.")

    picker_startup = None
    main_thread_window = None
    meeting_map = None
    phone_mon = None
    if cfg.popup_enabled:
        from .clients import MeetingMap, load_clients
        from .picker import DecideLater, apply_picker_result
        from .picker_ui import PickerWindow

        meeting_map = MeetingMap(s.system / "meeting-map.json", vault)

        def on_result(session, action):
            if session is not None and getattr(session, "app", None) == "Phone":
                if phone_mon is not None:
                    phone_mon.on_action(action)
                return
            if isinstance(action, DecideLater):
                return
            apply_picker_result(action, session, store, s, meeting_map)

        on_record = record

        def show_meeting(session):
            window = picker_startup.window if picker_startup is not None and picker_startup.ready else None
            if window is not None:
                window.show(session, activate=False)
                announce(session)
            elif companion.notifier is not None:
                try:
                    companion.notifier.meeting(session)
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not show the meeting popup: %s", e)

        companion.on_started = show_meeting

        def wire(window):
            def started(session):
                window.show(session, activate=False)
                announce(session)

            companion.on_started = started
            companion.on_retitle = window.refresh_session
            refresh_hotkeys()

        def make_picker():
            if main_thread_window is not None:
                return main_thread_window
            return PickerWindow(lambda: load_clients(s.clients), on_result, on_record, meeting_map, wait=False)

        if desktop.IS_MAC:
            main_thread_window = PickerWindow(lambda: load_clients(s.clients), on_result, on_record, meeting_map,
                                              wait=False, own_thread=False)
        picker_startup = PickerStartup(make_picker, wire)

    from .gpu import GpuService

    def open_popup() -> None:
        """Tray icon click: bring back the popup for the meeting going on (or the one that just ended)."""
        window = picker_startup.window if picker_startup is not None and picker_startup.ready else None
        if window is None:
            show_toast("The meeting popup is still starting. Try again in a few seconds.")
            return
        window.show(companion.latest_session(), activate=True)

    def manual_shot() -> None:
        time.sleep(0.5)                       # let the tray menu close first
        participant_shot("manual")
        show_toast("Screenshot saved to the meeting.")

    gpu = GpuService(s, cfg.gpu, vault, is_companion=True)
    if cfg.popup_enabled:
        gpu.extra_menu = [("Show meeting popup", open_popup, True),
                          ("Take participant screenshot now", manual_shot, False)]
    gpu.start(tray=True)

    exporter = None
    recordings = None
    if s.transcript_source == "whisper":
        from .sessions import overlaps_skipped
        from .transcribe import RecordingQueue

        def client_names() -> list[str]:
            if not s.clients.is_dir():
                return []
            return sorted(p.name for p in s.clients.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))

        def skip_recording(when: dt.datetime) -> bool:
            day = when.date()
            days = [(day + dt.timedelta(days=offset)).isoformat() for offset in (-1, 0, 1)]
            return overlaps_skipped(store.load(days), when)

        recordings = RecordingQueue(cfg.recordings_folder, s.inbox, s.system / "whisper-sources.json", vault, s,
                                    cfg.audio_settle_seconds, clients=client_names, should_skip=skip_recording)
        recordings.gpu = gpu
        recordings.start()
        log.info("Meetily transcripts are ignored (TRANSCRIPT_SOURCE=whisper). Recordings: %s", cfg.recordings_folder)
    elif cfg.meetily_export:
        from .meetily import MeetilyExporter

        exporter = MeetilyExporter(cfg.meetily_database, vault, s.inbox, s.system / "meetily-exported.json",
                                   cfg.meetily_settle_seconds)
    listener = None

    def refresh_hotkeys():
        nonlocal listener
        combos: dict[str, Callable[[], None]] = {}
        if hotkey and cfg.hotkey:
            combos[cfg.hotkey] = lambda: open_snip_overlay(cfg.screenshot_folder)
        window = picker_startup.window if picker_startup is not None and picker_startup.ready else None
        if hotkey and window is not None and cfg.picker_hotkey:
            combos[cfg.picker_hotkey] = lambda: window.show(companion.latest_session(), activate=True)
        if hotkey and gpu.own_ui and cfg.gpu.hotkey:
            combos[cfg.gpu.hotkey] = gpu.toggle
        if listener is not None:
            try:
                listener.stop()
            except Exception:  # noqa: BLE001
                log.debug("Could not stop the hotkeys", exc_info=True)
            listener = None
        listener = start_hotkeys(combos)

    if picker_startup is not None:
        picker_startup.poll(time.time())
    if cfg.popup_enabled and desktop.IS_WINDOWS and (picker_startup is None or not picker_startup.ready):
        try:
            companion.notifier = Notifier(record)
        except Exception as e:  # noqa: BLE001
            log.warning("Toasts unavailable (%s); meetings are still logged.", e)
    if listener is None:
        refresh_hotkeys()

    log.info("Meeting companion running (vault %s). Rules: %s. Screenshots: %s. Ctrl+C to stop.",
             s.vault_path, ", ".join(sorted({r.app for r in cfg.rules})), cfg.screenshot_folder)
    if not find_meetily(cfg.meetily_paths):
        log.warning("Meetily not found in [popup] meetily_paths; the Record button will only log.")
    if cfg.mic.enabled:
        log.info("Microphone detection on: %s%s.", ", ".join(a.app for a in cfg.mic.apps) or "no listed apps",
                 " and any other app" if cfg.mic.any_app else "")
    if cfg.phone_enabled and not desktop.IS_WINDOWS:
        log.info("USB phone import is Windows-only; it is off here.")
    if cfg.phone_enabled and desktop.IS_WINDOWS:
        from .phone import PHONE_SKIP_LABEL, ImportLedger, PhoneMonitor, PickerNotReady
        from .phone_mtp import ShellPhone

        def picker_status():
            window = picker_startup.window if picker_startup is not None and picker_startup.ready else None
            if window is None or not getattr(window, "shown", False):
                return False, None
            return True, getattr(window.session, "app", None)

        def ask_phone(rec, heading, _respond):
            window = picker_startup.window if picker_startup is not None and picker_startup.ready else None
            if window is None:
                raise PickerNotReady()
            session = Session(id=rec.phone_path, app="Phone", window_title=rec.name, start=now_local(),
                              call_title=Path(rec.name).stem)
            log.info("Asking about %s", heading)
            window.show(session, activate=True, skip_label=PHONE_SKIP_LABEL, heading=heading,
                        title=Path(rec.name).stem, hide_record=True)

        def make_client(action):
            if meeting_map is None:
                return None
            return apply_picker_result(action, None, None, s, meeting_map)

        state_dir = PROJECT_DIR / "state"
        phone_mon = PhoneMonitor(
            ShellPhone(cfg.phone_device_name, cfg.phone_usb_id, cfg.phone_folder),
            ImportLedger(state_dir / "phone_imports.json"),
            s.inbox, s.review, s.system / "audio", s.clients, s.system / "ingest-log.json",
            state_dir / "phone-tmp", settle_seconds=cfg.phone_settle_seconds,
            toast=show_toast, ask=ask_phone, create_client=make_client, picker_status=picker_status)
        phone_mon.start(cfg.phone_poll_seconds)
        log.info("Phone import watching %s (%s)", cfg.phone_device_name, cfg.phone_folder)
    next_export = 0.0
    next_lm = time.time() + LM_RETRY_SECONDS
    stop = threading.Event()

    def loop() -> None:
        nonlocal next_export, next_lm
        while not stop.is_set():
            now = time.time()
            if picker_startup is not None and not picker_startup.ready:
                try:
                    picker_startup.poll(now)
                except Exception:  # noqa: BLE001
                    log.exception("Client picker retry failed")
            if not gpu.server_checked and now >= next_lm:
                if gpu.retry_startup_cleanup():
                    log.info("LM Studio is reachable.")
                else:
                    log.info("LM Studio is still not reachable; checking again later.")
                    next_lm = now + LM_RETRY_SECONDS
            try:
                companion.tick(now, lister.list())
            except Exception:  # noqa: BLE001
                log.exception("Meeting detection failed")
            try:
                companion.shots.poll()
            except Exception:  # noqa: BLE001
                log.exception("Screenshot scan failed")
            if exporter is not None and now >= next_export:
                next_export = now + cfg.meetily_poll_seconds
                try:
                    exporter.poll()
                except Exception:  # noqa: BLE001
                    log.exception("Meetily export failed")
            if recordings is not None:
                try:
                    recordings.poll()
                    gpu.set_counts(recordings=len(recordings._jobs) + (1 if recordings.inflight else 0))
                except Exception:  # noqa: BLE001
                    log.exception("Meetily recording scan failed")
            stop.wait(cfg.poll_seconds)
    try:
        if main_thread_window is not None:
            # macOS: the window owns the main thread, detection runs beside it
            worker = threading.Thread(target=loop, name="companion-loop", daemon=True)
            worker.start()
            main_thread_window.run()
            if main_thread_window.failed:
                worker.join()                 # no window could start: keep detecting without it
            stop.set()
            worker.join(timeout=10)
        else:
            loop()
    except KeyboardInterrupt:
        pass
    finally:
        if phone_mon is not None:
            phone_mon.close()
        gpu.stop()
        if recordings is not None:
            recordings.stop()
        stop.set()
        companion.stop()
        if picker_startup is not None and picker_startup.window is not None:
            picker_startup.window.close()
        if listener is not None:
            listener.stop()
    return 0


def main_companion(args) -> int:
    from .cli import setup_logging
    from .config import load_settings

    overrides = {"vault_path": args.vault} if getattr(args, "vault", None) else {}
    s = load_settings(args.env, **overrides)
    setup_logging(s.system / "companion.log", args.verbose)
    return run_companion(s, load_config(args.config), hotkey=not args.no_hotkey)
