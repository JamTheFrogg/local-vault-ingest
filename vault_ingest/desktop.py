"""Everything the meeting companion needs from the operating system, for Windows, macOS and Linux.

Windows uses the Win32 API (user32, the microphone consent store, pycaw). macOS uses Quartz/AppKit (pyobjc)
and CoreAudio. Linux uses X11 (python-xlib or wmctrl) and PulseAudio/PipeWire (`pactl`). Every function
returns an empty result instead of raising when its backend is missing, so detection degrades to what the
platform can offer: on Wayland, for example, calls are still found through the microphone.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("vault_ingest")

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_warned: set[str] = set()


def os_key() -> str:
    """"windows", "macos" or "linux": the keys of per-OS tables in companion.toml."""
    if IS_WINDOWS:
        return "windows"
    if IS_MAC:
        return "macos"
    return "linux"


def warn_once(key: str, message: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message, *args)


def proc_key(name: str | None) -> str:
    """A process name compared across platforms: lower case, without ".exe" or ".app".

    "Zoom.exe" (Windows), "zoom.us" (macOS) and "zoom" (Linux) stay different names, so the rules list all of
    them, but "Chrome.exe" and "chrome" are the same key."""
    n = (name or "").strip().lower()
    for suffix in (".exe", ".app"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    return n


def app_from_path(path: str) -> str:
    """The app a process belongs to: on macOS the outermost "X.app" bundle in its path (so "Google Chrome
    Helper" counts as "Google Chrome"), otherwise the file name."""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    for p in parts:
        if p.lower().endswith(".app"):
            return p[:-4]
    return parts[-1] if parts else ""


# ------------------------------------------------------------------ paths
def _home() -> Path:
    return Path.home()


def app_data_dir(local: bool = False) -> Path:
    """Where apps keep their data: %APPDATA% / %LOCALAPPDATA%, ~/Library/Application Support, ~/.local/share."""
    if IS_WINDOWS:
        key = "LOCALAPPDATA" if local else "APPDATA"
        return Path(os.environ.get(key) or _home() / "AppData" / ("Local" if local else "Roaming"))
    if IS_MAC:
        return _home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME") or _home() / ".local" / "share")


_WINDOWS_VARS = {
    "%USERPROFILE%": lambda: str(_home()),
    "%HOMEPATH%": lambda: str(_home()),
    "%APPDATA%": lambda: str(app_data_dir()),
    "%LOCALAPPDATA%": lambda: str(app_data_dir(local=True)),
    "%PROGRAMFILES%": lambda: "/Applications" if IS_MAC else "/opt",
}


def expand_path(p: str | Path) -> Path:
    """~, $VARS and %VARS%. Off Windows, the usual %VARS% map to their macOS/Linux counterparts and
    backslashes become slashes, so a Windows-style companion.toml still points somewhere sensible."""
    text = str(p)
    if not IS_WINDOWS:
        upper = text.upper()
        for var, value in _WINDOWS_VARS.items():
            i = upper.find(var)
            while i >= 0:
                text = text[:i] + value() + text[i + len(var):]
                upper = text.upper()
                i = upper.find(var)
        text = text.replace("\\", "/")
    return Path(os.path.expandvars(os.path.expanduser(text)))


def pick_os_value(value, default=None):
    """companion.toml values may be a plain value or a table {windows = ..., macos = ..., linux = ...}."""
    if isinstance(value, dict):
        for key in (os_key(), "default", "all"):
            if key in value:
                return value[key]
        return default
    return value if value is not None else default


def default_screenshot_folder() -> Path:
    if IS_WINDOWS:
        return expand_path(r"%USERPROFILE%\Pictures\Screenshots")
    if IS_MAC:
        try:
            out = subprocess.run(["defaults", "read", "com.apple.screencapture", "location"],
                                 capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                return expand_path(out.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
        return _home() / "Desktop"
    pictures = Path(os.environ.get("XDG_PICTURES_DIR") or _home() / "Pictures")
    return pictures / "Screenshots"


def default_meetily_database() -> Path:
    return app_data_dir() / "com.meetily.ai" / "meeting_minutes.sqlite"


def default_recordings_folder() -> Path:
    return _home() / "Music" / "meetily-recordings"


def default_meetily_paths() -> list[str]:
    if IS_WINDOWS:
        return [r"%LOCALAPPDATA%\meetily\meetily.exe", r"%LOCALAPPDATA%\Programs\meetily\meetily.exe",
                r"%ProgramFiles%\meetily\meetily.exe", r"%ProgramFiles%\Meetily\meetily.exe"]
    if IS_MAC:
        return ["/Applications/meetily.app", "/Applications/Meetily.app", "~/Applications/meetily.app"]
    return ["/usr/bin/meetily", "/usr/local/bin/meetily", "~/.local/bin/meetily", "~/Applications/meetily.AppImage",
            "/opt/meetily/meetily"]


# ------------------------------------------------------------------ open things
def open_path(target: str | Path) -> bool:
    """Open a file, folder, app bundle or URL with the system handler."""
    target = str(target)
    try:
        if IS_WINDOWS:
            os.startfile(target)  # type: ignore[attr-defined]
        elif IS_MAC:
            subprocess.Popen(["open", target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError as e:
        log.warning("Could not open %s: %s", target, e)
        return False


def launch_app(path: Path) -> bool:
    """Start a program: an .exe, a macOS .app bundle, or a Linux executable/AppImage."""
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif IS_MAC:
            subprocess.Popen(["open", "-a", str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen([str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError as e:
        log.warning("Could not start %s: %s", path, e)
        return False


def screenshot_tools(dest: Path) -> list[list[str]]:
    """Interactive region-snip commands, in order of preference, that save straight into `dest`."""
    stamp = f"{dt.datetime.now():%Y-%m-%d %H-%M-%S}"
    target = str(Path(dest) / f"Screenshot {stamp}.png")
    if IS_MAC:
        return [["screencapture", "-i", target]]
    if IS_LINUX:
        return [["gnome-screenshot", "-a", "-f", target],
                ["spectacle", "-r", "-b", "-n", "-o", target],
                ["flameshot", "gui", "-p", str(dest)],
                ["sh", "-c", f'grim -g "$(slurp)" "{target}"'],
                ["scrot", "-s", target]]
    return []


def open_snip_tool(dest: Path) -> bool:
    """Win+Shift+S on Windows; `screencapture -i` on macOS; the first snip tool found on Linux. On macOS and
    Linux the snip is saved into the companion's screenshot folder, so it is attached to the meeting."""
    if IS_WINDOWS:
        return open_path("ms-screenclip:")
    Path(dest).mkdir(parents=True, exist_ok=True)
    for argv in screenshot_tools(dest):
        exe = argv[0] if argv[0] != "sh" else "grim"
        if shutil.which(exe) and (argv[0] != "sh" or shutil.which("slurp")):
            try:
                subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return True
            except OSError as e:
                log.warning("Could not start %s: %s", exe, e)
    warn_once("snip", "No screenshot tool found (gnome-screenshot, spectacle, flameshot, grim+slurp or scrot).")
    return False


def capture_screen(path: Path) -> Path:
    """PNG of every monitor into `path`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if IS_MAC and shutil.which("screencapture"):
        subprocess.run(["screencapture", "-x", "-t", "png", str(path)], check=True, timeout=30,
                       stdin=subprocess.DEVNULL, capture_output=True)
        return path
    try:
        from PIL import ImageGrab

        image = ImageGrab.grab(all_screens=True) if IS_WINDOWS else ImageGrab.grab()
        image.save(path)
        return path
    except Exception as e:  # noqa: BLE001  Wayland: PIL cannot grab the screen
        if IS_LINUX:
            for argv in (["grim", str(path)], ["gnome-screenshot", "-f", str(path)],
                         ["spectacle", "-b", "-n", "-f", "-o", str(path)], ["scrot", str(path)]):
                if shutil.which(argv[0]):
                    subprocess.run(argv, check=True, timeout=30, stdin=subprocess.DEVNULL, capture_output=True)
                    if path.is_file():
                        return path
        raise RuntimeError(f"cannot take a screenshot here ({e})") from e


# ------------------------------------------------------------------ windows
@dataclass
class Window:
    hwnd: int            # Win32 HWND, X11 window id, or the macOS window number
    pid: int
    process: str         # process name ("chrome.exe", "Google Chrome", "chrome")
    title: str
    class_name: str = ""  # Win32 window class / X11 WM_CLASS


class _NameCache:
    def __init__(self):
        self.names: dict[int, str] = {}

    def name(self, pid: int) -> str:
        if pid not in self.names:
            try:
                import psutil

                self.names[pid] = psutil.Process(pid).name()
            except Exception:  # noqa: BLE001
                self.names[pid] = ""
        return self.names[pid]

    def forget_except(self, pids: set[int]) -> None:
        for gone in set(self.names) - pids:           # pids get reused; forget the ones that disappeared
            del self.names[gone]


class Win32WindowLister:
    """Visible top-level windows with a title, plus their process names (cached per pid)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes, self.wintypes = ctypes, wintypes
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        H = wintypes.HWND
        u.EnumWindows.argtypes = [self.proc_type, wintypes.LPARAM]
        for fn in (u.IsWindowVisible, u.GetWindowTextLengthW, u.IsIconic, u.SetForegroundWindow):
            fn.argtypes = [H]
        u.GetWindowTextW.argtypes = [H, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.argtypes = [H, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [H, ctypes.POINTER(wintypes.DWORD)]
        u.ShowWindow.argtypes = [H, ctypes.c_int]
        u.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
        self.cache = _NameCache()

    def list(self, visible_only: bool = True) -> list[Window]:
        ctypes, wintypes, user32 = self.ctypes, self.wintypes, self.user32
        raw: list[tuple[int, int, str, str]] = []

        def cb(hwnd, _):
            if visible_only and not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            raw.append((int(hwnd or 0), pid.value, buf.value, cls.value))
            return True

        user32.EnumWindows(self.proc_type(cb), 0)
        self.cache.forget_except({r[1] for r in raw})
        return [Window(h, pid, self.cache.name(pid), title, cls) for h, pid, title, cls in raw]

    def focus(self, window: Window) -> bool:
        user32, hwnd = self.user32, window.hwnd
        if not user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, 5)               # SW_SHOW (Meetily hidden in the tray)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)               # SW_RESTORE
        # a background process may only take the foreground right after a key event
        user32.keybd_event(0x12, 0, 0, 0)            # Alt down
        ok = bool(user32.SetForegroundWindow(hwnd))
        user32.keybd_event(0x12, 0, 2, 0)            # Alt up
        return ok


class MacWindowLister:
    """On-screen windows from Quartz. Titles need Screen Recording permission (System Settings > Privacy &
    Security > Screen Recording) for the app running the companion; without it titles are empty and calls are
    found through the microphone only."""

    def __init__(self):
        import Quartz  # noqa: F401  (pyobjc-framework-Quartz)

        self.Q = Quartz
        self._told = False

    def list(self, visible_only: bool = True) -> list[Window]:
        Q = self.Q
        opts = Q.kCGWindowListExcludeDesktopElements
        if visible_only:
            opts |= Q.kCGWindowListOptionOnScreenOnly
        else:
            opts |= Q.kCGWindowListOptionAll
        info = Q.CGWindowListCopyWindowInfo(opts, Q.kCGNullWindowID) or []
        out: list[Window] = []
        untitled = 0
        for w in info:
            if int(w.get("kCGWindowLayer", 0) or 0) != 0:       # menu bar, dock, overlays
                continue
            title = str(w.get("kCGWindowName") or "")
            owner = str(w.get("kCGWindowOwnerName") or "")
            if not title:
                untitled += 1
                continue
            out.append(Window(int(w.get("kCGWindowNumber", 0) or 0), int(w.get("kCGWindowOwnerPID", 0) or 0),
                              owner, title))
        if not out and untitled and not self._told:
            self._told = True
            log.warning("macOS shows no window titles to this process. Allow it in System Settings > Privacy & "
                        "Security > Screen Recording, then restart the companion. Calls are still found "
                        "through the microphone.")
        return out

    def focus(self, window: Window) -> bool:
        try:
            from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(window.pid)
            return bool(app and app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))
        except Exception as e:  # noqa: BLE001
            log.debug("Could not focus pid %s: %s", window.pid, e)
            return False


class X11WindowLister:
    """Top-level windows from the window manager (EWMH _NET_CLIENT_LIST) via python-xlib, or `wmctrl -lpx`
    when python-xlib is missing. Native Wayland windows are invisible to X11; XWayland ones (Chrome, Zoom,
    Discord by default) are listed."""

    def __init__(self):
        self.cache = _NameCache()
        self.display = None
        try:
            from Xlib import display as xdisplay

            self.display = xdisplay.Display()
            self.root = self.display.screen().root
            self.atoms = {n: self.display.intern_atom(n) for n in (
                "_NET_CLIENT_LIST", "_NET_WM_NAME", "_NET_WM_PID", "UTF8_STRING", "_NET_ACTIVE_WINDOW",
                "_NET_WM_STATE", "_NET_WM_STATE_HIDDEN")}
        except Exception as e:  # noqa: BLE001
            self.display = None
            if not shutil.which("wmctrl"):
                raise RuntimeError(f"no X11 access ({e}); install python-xlib or wmctrl") from e

    def _prop(self, win, name, kind=0):
        try:
            p = win.get_full_property(self.atoms[name], kind)
            return p.value if p is not None else None
        except Exception:  # noqa: BLE001
            return None

    def list(self, visible_only: bool = True) -> list[Window]:
        if self.display is None:
            return self._list_wmctrl()
        out: list[Window] = []
        ids = self._prop(self.root, "_NET_CLIENT_LIST") or []
        for wid in ids:
            try:
                win = self.display.create_resource_object("window", int(wid))
                if visible_only:
                    state = self._prop(win, "_NET_WM_STATE") or []
                    if self.atoms["_NET_WM_STATE_HIDDEN"] in list(state):
                        continue
                raw = self._prop(win, "_NET_WM_NAME", self.atoms["UTF8_STRING"])
                if raw is None:
                    raw = win.get_wm_name()
                title = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
                if not title:
                    continue
                pid_v = self._prop(win, "_NET_WM_PID")
                pid = int(pid_v[0]) if pid_v is not None and len(pid_v) else 0
                cls = win.get_wm_class()
                cls_name = cls[1] if cls else ""
                out.append(Window(int(wid), pid, self.cache.name(pid) if pid else (cls_name or ""), title, cls_name))
            except Exception:  # noqa: BLE001  a window can vanish between the list and the read
                continue
        self.cache.forget_except({w.pid for w in out})
        return out

    def _list_wmctrl(self) -> list[Window]:
        try:
            proc = subprocess.run(["wmctrl", "-lpx"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return []
        return parse_wmctrl(proc.stdout, self.cache.name)

    def focus(self, window: Window) -> bool:
        if shutil.which("wmctrl"):
            return subprocess.run(["wmctrl", "-ia", hex(window.hwnd)], capture_output=True).returncode == 0
        if shutil.which("xdotool"):
            return subprocess.run(["xdotool", "windowactivate", str(window.hwnd)], capture_output=True).returncode == 0
        return False

    def active_pid(self) -> int:
        if self.display is None:
            return 0
        try:
            active = self._prop(self.root, "_NET_ACTIVE_WINDOW")
            if not active:
                return 0
            win = self.display.create_resource_object("window", int(active[0]))
            pid_v = self._prop(win, "_NET_WM_PID")
            return int(pid_v[0]) if pid_v is not None and len(pid_v) else 0
        except Exception:  # noqa: BLE001
            return 0


def parse_wmctrl(text: str, name_of=lambda pid: "") -> list[Window]:
    """`wmctrl -lpx` lines: id, desktop, pid, WM_CLASS, host, title. Desktop -1 is sticky; titles may be empty."""
    out = []
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        try:
            wid, pid = int(parts[0], 16), int(parts[2])
        except ValueError:
            continue
        cls = parts[3].split(".", 1)[-1]
        title = parts[5] if len(parts) > 5 else ""
        if not title.strip():
            continue
        out.append(Window(wid, pid, name_of(pid) if pid > 0 else cls, title, cls))
    return out


class NullWindowLister:
    def list(self, visible_only: bool = True) -> list[Window]:
        return []

    def focus(self, window: Window) -> bool:
        return False


def make_window_lister():
    try:
        if IS_WINDOWS:
            return Win32WindowLister()
        if IS_MAC:
            return MacWindowLister()
        if os.environ.get("DISPLAY"):
            return X11WindowLister()
        log.warning("No X11 display (pure Wayland session): window titles are not visible, so meetings are "
                    "found through the microphone only.")
    except Exception as e:  # noqa: BLE001
        log.warning("Window titles are not available here (%s); meetings are found through the microphone only.", e)
    return NullWindowLister()


def foreground_process() -> str:
    """proc_key of the frontmost app ("chrome", "google chrome", "zoom.us"), "" if unknown."""
    try:
        if IS_WINDOWS:
            import ctypes
            from ctypes import wintypes

            import psutil

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return ""
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return proc_key(psutil.Process(pid.value).name())
        if IS_MAC:
            from AppKit import NSWorkspace

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return proc_key(str(app.localizedName())) if app is not None else ""
        if shutil.which("xdotool"):
            out = subprocess.run(["xdotool", "getactivewindow", "getwindowpid"], capture_output=True, text=True,
                                 timeout=3)
            pid = int(out.stdout.strip() or 0)
        else:
            lister = X11WindowLister()
            pid = lister.active_pid()
        if pid:
            import psutil

            return proc_key(psutil.Process(pid).name())
    except Exception:  # noqa: BLE001
        return ""
    return ""


# ------------------------------------------------------------------ audio streams (Linux)
def pactl_json(kind: str) -> list[dict]:
    """`pactl -f json list <kind>` (PulseAudio 16+ and PipeWire's pipewire-pulse). [] when unavailable."""
    if not shutil.which("pactl"):
        warn_once("pactl", "pactl is not installed; microphone and audio checks are off "
                           "(install pulseaudio-utils or pipewire-pulse).")
        return []
    try:
        proc = subprocess.run(["pactl", "-f", "json", "list", kind], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        warn_once("pactl-json", "This pactl has no JSON output (needs PulseAudio 16+ or PipeWire); "
                                "microphone and audio checks are off.")
        return []
    return data if isinstance(data, list) else []


@dataclass(frozen=True)
class Stream:
    app: str          # process binary, else application name
    pid: int
    path: str         # executable path when known


def pulse_streams(entries: list[dict], monitor_sources: set[str] | None = None) -> list[Stream]:
    """Running (not corked) streams from `pactl -f json list source-outputs|sink-inputs` entries. Recording
    from a ".monitor" source captures what the speakers play (OBS, screen recorders), not the microphone."""
    out = []
    monitor_sources = monitor_sources or set()
    for e in entries:
        if e.get("corked"):
            continue
        if str(e.get("source", "")) in monitor_sources:
            continue
        props = e.get("properties") or {}
        if str(props.get("stream.monitor", "")).lower() == "true":
            continue
        binary = str(props.get("application.process.binary") or "")
        name = str(props.get("application.name") or "")
        try:
            pid = int(props.get("application.process.id") or 0)
        except ValueError:
            pid = 0
        path = ""
        if pid:
            try:
                import psutil

                path = psutil.Process(pid).exe()
            except Exception:  # noqa: BLE001
                path = ""
        app = binary or name
        if app:
            out.append(Stream(app, pid, path or binary))
    return out


def linux_mic_streams() -> list[Stream]:
    sources = pactl_json("sources")
    monitors = {str(s.get("index")) for s in sources if s.get("monitor_of_sink") not in (None, "", "n/a")}
    monitors |= {str(s.get("name")) for s in sources if str(s.get("name", "")).endswith(".monitor")}
    return pulse_streams(pactl_json("source-outputs"), monitors)


def linux_playing() -> set[str]:
    return {proc_key(s.app) for s in pulse_streams(pactl_json("sink-inputs"))}


# ------------------------------------------------------------------ CoreAudio (macOS 14.2+)
def fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


def coreaudio_processes() -> list[tuple[int, bool, bool]]:
    """(pid, records, plays) for every process CoreAudio knows. macOS 14.2+ only; [] elsewhere."""
    if not IS_MAC:
        return []
    import ctypes

    ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")

    class Address(ctypes.Structure):
        _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]

    glob = fourcc("glob")

    def get(obj: int, selector: str, ctype, count: int | None = None):
        addr = Address(fourcc(selector), glob, 0)
        size = ctypes.c_uint32(0)
        if count is None:
            if ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size)) != 0:
                return None
            n = size.value // ctypes.sizeof(ctype)
            buf = (ctype * n)()
        else:
            buf = (ctype * count)()
            size.value = ctypes.sizeof(buf)
        if ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), buf) != 0:
            return None
        return list(buf)

    ids = get(1, "prs#", ctypes.c_uint32)            # kAudioHardwarePropertyProcessObjectList
    if ids is None:
        return []
    out = []
    for obj in ids:
        pid = get(obj, "ppid", ctypes.c_int32, 1)     # kAudioProcessPropertyPID
        rec = get(obj, "piri", ctypes.c_uint32, 1)    # kAudioProcessPropertyIsRunningInput
        play = get(obj, "piro", ctypes.c_uint32, 1)   # kAudioProcessPropertyIsRunningOutput
        if pid:
            out.append((int(pid[0]), bool(rec and rec[0]), bool(play and play[0])))
    return out


def mac_input_in_use() -> bool:
    """Older macOS: only whether the default microphone is running at all (kAudioDevicePropertyDeviceIsRunningSomewhere)."""
    import ctypes

    ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")

    class Address(ctypes.Structure):
        _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]

    dev = ctypes.c_uint32(0)
    size = ctypes.c_uint32(4)
    addr = Address(fourcc("dIn "), fourcc("glob"), 0)
    if ca.AudioObjectGetPropertyData(1, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(dev)) != 0:
        return False
    running = ctypes.c_uint32(0)
    addr = Address(fourcc("gone"), fourcc("glob"), 0)
    if ca.AudioObjectGetPropertyData(dev.value, ctypes.byref(addr), 0, None, ctypes.byref(size),
                                     ctypes.byref(running)) != 0:
        return False
    return bool(running.value)


def _proc_path(pid: int) -> str:
    try:
        import psutil

        return psutil.Process(pid).exe()
    except Exception:  # noqa: BLE001
        return ""


def mac_mic_processes() -> list[tuple[int, str]]:
    """(pid, executable path) of processes recording from a microphone."""
    procs = coreaudio_processes()
    if procs:
        return [(pid, _proc_path(pid)) for pid, rec, _ in procs if rec]
    if mac_input_in_use():
        # no per-app data before macOS 14.2: credit the frontmost app
        warn_once("mac-mic", "This macOS only says that the microphone is in use, not by which app; the "
                             "frontmost app is taken as the caller (macOS 14.2+ names the app).")
        try:
            from AppKit import NSWorkspace

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is not None:
                return [(int(app.processIdentifier()), str(app.bundleURL().path()))]
        except Exception:  # noqa: BLE001
            return []
    return []


def mac_playing() -> set[str]:
    return {proc_key(app_from_path(_proc_path(pid))) for pid, _, play in coreaudio_processes() if play}


# ------------------------------------------------------------------ audio playing (all)
class AudioProbe:
    """proc_keys of processes playing audio right now. Disables itself once if its backend fails."""

    def __init__(self):
        self.ok = True
        self._com = False

    def playing(self) -> set[str]:
        if not self.ok:
            return set()
        try:
            if IS_WINDOWS:
                return self._windows()
            if IS_MAC:
                return mac_playing()
            return linux_playing()
        except Exception as e:  # noqa: BLE001
            self.ok = False
            log.warning("Can't read audio sessions (%s); a meeting now ends end_grace_seconds after its "
                        "window was last seen.", e)
            return set()

    def _windows(self) -> set[str]:
        # pycaw is COM: the thread calling it must have joined a COM apartment first
        import comtypes

        try:
            comtypes.CoInitialize()
        except OSError:
            pass                                            # already initialised on this thread
        from pycaw.pycaw import AudioUtilities

        names = set()
        for s in AudioUtilities.GetAllSessions():
            if s.Process is not None and s.State == 1:      # AudioSessionStateActive
                names.add(proc_key(s.Process.name()))
        return names


# ------------------------------------------------------------------ screen geometry
def work_area(root=None) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the usable screen (above the taskbar / below the menu bar, beside the Dock
    as far as Tk can tell)."""
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        return rect.left, rect.top, rect.right, rect.bottom
    if root is not None:
        try:
            w, h = int(root.winfo_screenwidth()), int(root.winfo_screenheight())
            if IS_MAC:
                try:
                    from AppKit import NSScreen

                    screen = NSScreen.mainScreen()
                    full, vis = screen.frame(), screen.visibleFrame()
                    top = int(full.size.height - vis.size.height - vis.origin.y)
                    return (int(vis.origin.x), top, int(vis.origin.x + vis.size.width),
                            int(top + vis.size.height))
                except Exception:  # noqa: BLE001
                    return 0, 25, w, h - 80                  # menu bar, a Dock at the bottom
            return 0, 0, w, h - 48                           # a panel at the bottom
        except Exception:  # noqa: BLE001
            pass
    return 0, 0, 1280, 800


def ui_font() -> str:
    if IS_WINDOWS:
        return "Segoe UI"
    if IS_MAC:
        return "Helvetica Neue"
    return "DejaVu Sans"

