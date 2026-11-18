"""Meeting signals besides window titles.

* Microphone: which app is recording from the microphone right now. Windows keeps this in the
  microphone consent store (the data behind the "app is using your microphone" icon), Linux in
  PulseAudio/PipeWire (`pactl list source-outputs`), macOS 14.2+ in CoreAudio. Any call app shows up there,
  WhatsApp, Viber and Discord included, and a Meet call in a background tab too.
* Participant screenshot: a picture of the whole desktop taken when Record is clicked, so the vision
  model can read the names on the participant tiles.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import desktop
from .desktop import foreground_process, proc_key  # noqa: F401  (foreground_process is re-exported)

log = logging.getLogger("vault_ingest")

MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
FILETIME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
BROWSERS = ("chrome.exe", "msedge.exe", "brave.exe", "firefox.exe", "opera.exe", "vivaldi.exe", "arc.exe",
            # macOS and Linux process names
            "google chrome", "microsoft edge", "brave browser", "chromium", "chromium-browser", "chrome",
            "firefox-esr", "opera", "vivaldi", "vivaldi-bin", "arc", "safari", "msedge", "brave")
BROWSER_KEYS = frozenset(proc_key(b) for b in BROWSERS)


@dataclass(frozen=True)
class MicUser:
    """One app holding the microphone. `id` is a package family name (Store apps such as WhatsApp or the new
    Teams) or an exe path with backslashes (desktop apps such as Chrome or Zoom)."""
    id: str
    since: float = 0.0               # epoch seconds Windows says the app started using the microphone
    process: str = ""                # macOS/Linux: the app's process name, which its windows carry

    @property
    def exe(self) -> str:
        """Lower-case process name whose windows belong to this app: the exe file name for a Windows desktop
        app, "" for a Windows Store app, the app's process name on macOS and Linux."""
        if self.process:
            return self.process.lower()
        tail = self.id.replace("/", "\\").rsplit("\\", 1)[-1]
        return tail.lower() if tail.lower().endswith(".exe") else ""


def filetime_to_epoch(value: int) -> float:
    return (FILETIME_EPOCH + dt.timedelta(microseconds=int(value) // 10)).timestamp()


def is_active(start: int, stop: int) -> bool:
    """While an app holds the microphone Windows sets LastUsedTimeStart and leaves LastUsedTimeStop at 0."""
    return bool(start) and (not stop or stop < start)


def read_mic_users() -> list[MicUser]:
    """Apps recording from the microphone right now. [] when this platform cannot tell."""
    if desktop.IS_MAC:
        return mac_mic_users()
    if desktop.IS_LINUX:
        return linux_mic_users()
    return windows_mic_users()


def linux_mic_users() -> list[MicUser]:
    """PulseAudio/PipeWire recording streams. `id` is the executable path (else the binary or app name), so
    the same `match` regexes work as on Windows."""
    return [MicUser(st.path or st.app, 0.0, process=desktop.proc_key(st.app)) for st in desktop.linux_mic_streams()]


def mac_mic_users() -> list[MicUser]:
    """CoreAudio processes recording from a microphone. A helper ("Google Chrome Helper") is credited to its
    app bundle ("Google Chrome"), which owns the call's window."""
    out = []
    for pid, path in desktop.mac_mic_processes():
        app = desktop.app_from_path(path) if path else ""
        out.append(MicUser(path or app or f"pid {pid}", 0.0, process=desktop.proc_key(app)))
    return out


def windows_mic_users() -> list[MicUser]:
    """Apps that hold the microphone right now (HKCU consent store). [] off Windows or on any error."""
    if sys.platform != "win32":
        return []
    import winreg

    out: list[MicUser] = []

    def values(key) -> tuple[int, int] | None:
        try:
            start = int(winreg.QueryValueEx(key, "LastUsedTimeStart")[0] or 0)
            stop = int(winreg.QueryValueEx(key, "LastUsedTimeStop")[0] or 0)
            return start, stop
        except OSError:
            return None

    def scan(key, non_packaged: bool) -> None:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(key, i)
            except OSError:
                return
            i += 1
            try:
                with winreg.OpenKey(key, name) as sub:
                    if name == "NonPackaged" and not non_packaged:
                        scan(sub, True)
                        continue
                    times = values(sub)
            except OSError:
                continue
            if times and is_active(*times):
                app_id = name.replace("#", "\\") if non_packaged else name
                out.append(MicUser(app_id, filetime_to_epoch(times[0])))

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MIC_KEY) as root:
            scan(root, False)
    except OSError as e:
        log.debug("Microphone consent store not readable: %s", e)
    return out


# ------------------------------------------------------------------ config
@dataclass
class MicApp:
    app: str                          # name in the popup and the session file, e.g. "WhatsApp"
    match: re.Pattern                 # searched in the app id (package name or exe path), case-insensitive
    processes: tuple[str, ...] = ()   # where to look for a window title to name the call
    title: re.Pattern | None = None   # prefer a window whose title matches this


@dataclass
class MicConfig:
    enabled: bool = True
    # an app that is not listed in `apps` and holds the microphone also counts, named "Call (<exe>)"
    any_app: bool = True
    # the microphone has to be held this long (a WhatsApp voice message is not a call)
    start_after_seconds: float = 15.0
    # Windows can leave an app marked as "using the microphone" after a crash; ignore marks older than this
    max_hours: float = 8.0
    ignore: re.Pattern | None = None
    # while a game is running (same rules as the GPU pause), the microphone does not start a meeting:
    # that is game voice chat (Discord, in-game voice), not a client call
    skip_while_gaming: bool = True
    apps: list[MicApp] = field(default_factory=list)


def _re(pattern: str | None, where: str) -> re.Pattern | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.I)
    except re.error as e:
        raise SystemExit(f"companion.toml: bad regex in {where}: {pattern!r} ({e})") from e


def mic_config_from_toml(raw: dict | None, apps: Iterable[dict] | None) -> MicConfig:
    raw = raw or {}
    cfg = MicConfig()
    cfg.enabled = bool(raw.get("enabled", cfg.enabled))
    cfg.any_app = bool(raw.get("any_app", cfg.any_app))
    cfg.start_after_seconds = float(raw.get("start_after_seconds", cfg.start_after_seconds))
    cfg.max_hours = float(raw.get("max_hours", cfg.max_hours))
    cfg.skip_while_gaming = bool(raw.get("skip_while_gaming", cfg.skip_while_gaming))
    ignore = raw.get("ignore") or []
    if isinstance(ignore, str):
        ignore = [ignore]
    cfg.ignore = _re("|".join(f"(?:{p})" for p in ignore), "[microphone] ignore") if ignore else None
    for i, a in enumerate(apps or [], 1):
        where = f"mic_apps {i} ({a.get('app', '?')})"
        if not a.get("app") or not a.get("match"):
            raise SystemExit(f"companion.toml: {where} needs `app` and `match`")
        cfg.apps.append(MicApp(app=str(a["app"]), match=_re(a["match"], where),
                               processes=tuple(str(p).lower() for p in a.get("processes") or ()),
                               title=_re(a.get("title"), where)))
    return cfg


# ------------------------------------------------------------------ watcher
class MicWatcher:
    """Reports apps that have held the microphone for at least `start_after_seconds` (by our own clock, so a
    clock difference with Windows' timestamps does not matter)."""

    def __init__(self, cfg: MicConfig, reader: Callable[[], list[MicUser]] = read_mic_users,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.reader = reader
        self.clock = clock
        self.first_seen: dict[str, float] = {}
        self.ok = True

    def __call__(self, now: float | None = None) -> list[MicUser]:
        if not self.cfg.enabled or not self.ok:
            return []
        now = self.clock() if now is None else now
        try:
            users = self.reader()
        except Exception as e:  # noqa: BLE001
            self.ok = False
            log.warning("Can't read which apps use the microphone (%s); only window titles detect meetings.", e)
            return []
        wall = time.time()
        current: dict[str, MicUser] = {}
        for u in users:
            if self.cfg.ignore is not None and self.cfg.ignore.search(u.id):
                continue
            if u.since and self.cfg.max_hours and wall - u.since > self.cfg.max_hours * 3600:
                continue                                  # a stale mark left by a crashed app
            current[u.id] = u
        for gone in set(self.first_seen) - set(current):
            del self.first_seen[gone]
            log.info("Microphone released by %s", gone)
        for new in set(current) - set(self.first_seen):
            self.first_seen[new] = now
            log.info("Microphone in use by %s", new)
        return [u for k, u in current.items() if now - self.first_seen[k] >= self.cfg.start_after_seconds]


def classify(cfg: MicConfig, user: MicUser) -> MicApp | None:
    for a in cfg.apps:
        if a.match.search(user.id):
            return a
    if cfg.any_app:
        exe = user.exe
        if exe:
            name = exe[:-4] if exe.endswith(".exe") else (user.process or exe)
        else:                                             # Store app: "5319275A.WhatsAppDesktop_cv1g..." -> "WhatsAppDesktop"
            name = user.id.split("_", 1)[0].rsplit(".", 1)[-1] or user.id
        return MicApp(app=f"Call ({name})", match=re.compile(re.escape(user.id), re.I),
                      processes=(exe,) if exe else ())
    return None


def call_title(app: MicApp, windows: list, fallback_process: str = "") -> tuple[str, object | None]:
    """A window title to name the call, and that window. Prefers `app.title` matches, then any title that is
    more than the bare app name (a WhatsApp call window is titled with the contact)."""
    procs = {proc_key(p) for p in app.processes} or ({proc_key(fallback_process)} if fallback_process else set())
    own = [w for w in windows if proc_key(w.process) in procs and w.title.strip()]
    if app.title is not None:
        for w in own:
            if app.title.search(w.title):
                return w.title, w
        if procs & BROWSER_KEYS:
            return f"{app.app}", None             # some other tab is in front; its title says nothing about the call
    bare = {app.app.lower(), "whatsapp", "viber", "discord", "slack", "signal", "telegram", "skype", "messenger"}
    for w in own:
        if w.title.strip().lower() not in bare:
            return w.title, w
    if own:
        return own[0].title, own[0]
    return f"{app.app} call", None


# ------------------------------------------------------------------ screenshots
def capture_desktop(dest_dir: Path, label: str = "participants") -> Path:
    """PNG of every monitor. The caller hides its own popup first."""
    path = Path(dest_dir) / f"{dt.datetime.now():%Y-%m-%d %H-%M-%S} {label}.png"
    return desktop.capture_screen(path)
