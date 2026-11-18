"""LM Studio chat-model lifecycle, GPU pause, and game detection.

The inbox watcher and the meeting companion are separate processes. They share
`_System/gpu-state.json`. Only the configured chat model is loaded and unloaded.
Embedding models (bge-m3 and the rest) are left alone.
"""
from __future__ import annotations

import atexit
import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import httpx

log = logging.getLogger("vault_ingest")

STATE_NAME = "gpu-state.json"


def paused_now(settings) -> bool:
    """True when the shared pause file says work should stop. Used to catch a hold that won a race."""
    path = Path(settings.system) / STATE_NAME
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(isinstance(data, dict) and data.get("paused"))


def pid_alive(pid: int) -> bool:
    """True when `pid` is still a running process. Never uses os.kill (that stops a Windows process)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        import psutil
    except ImportError:
        return True
    return bool(psutil.pid_exists(pid))


TTL_SECONDS = 120
MEETING_FRESH_SECONDS = 180
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FULLSCREEN_VRAM_MB = 500          # a fullscreen window is a game only above this much of its own GPU memory
SERVER_START_TIMEOUT = 60
SERVER_START_FAILED = "LM Studio could not be started"
_TEXT = {"encoding": "utf-8", "errors": "replace"}


def timeout_label(timeout) -> str:
    if isinstance(timeout, (int, float)):
        return f"{float(timeout):.0f}s"
    read = getattr(timeout, "read", None)
    if isinstance(read, (int, float)):
        return f"{float(read):.0f}s"
    return str(timeout)


def run_external(argv, *, timeout: float, label: str, run: Callable | None = None):
    """One external command. Logs before and after. Output is UTF-8, so a stray byte cannot kill the reader thread."""
    log.info("Starting %s (timeout %s)", label, timeout_label(timeout))
    runner = run or subprocess.run
    try:
        proc = runner(list(argv), capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                      creationflags=_NO_WINDOW, **_TEXT)
    except subprocess.TimeoutExpired:
        log.info("Finished %s: timed out after %s", label, timeout_label(timeout))
        raise
    except OSError as e:
        log.info("Finished %s: %s", label, e)
        raise
    log.info("Finished %s: exit %s", label, proc.returncode)
    return proc


def http_call(method: str, url: str, *, timeout, label: str | None = None, client=None, **kw):
    """One HTTP call. Logs before and after, including a timeout or a connection error."""
    label = label or f"HTTP {method} {url}"
    log.info("Starting %s (timeout %s)", label, timeout_label(timeout))
    try:
        if client is not None:
            response = client.request(method, url, timeout=timeout, **kw)
        else:
            response = httpx.request(method, url, timeout=timeout, **kw)
    except httpx.TimeoutException as e:
        log.info("Finished %s: timed out (%s)", label, e)
        raise
    except httpx.HTTPError as e:
        log.info("Finished %s: %s", label, e)
        raise
    log.info("Finished %s: %s", label, response.status_code)
    return response

PATH_MARKERS = (
    "\\steamapps\\common\\",
    "\\Epic Games\\",
    "\\GOG Galaxy\\Games\\",
    "\\XboxGames\\",
)
DEFAULT_NAMES = ("ProjectZomboid64.exe",)
# Programs that live in a game's folder but are not the game: crash reporters and anti-cheat services keep
# running after the game is closed, and must not pause the GPU or mute call detection.
DEFAULT_HELPERS = (
    "crashpad_handler.exe", "crashpad_handler", "UnityCrashHandler64.exe", "UnityCrashHandler32.exe",
    "CrashReportClient.exe", "CrashReporter.exe", "CrashHandler.exe", "BugSplatHD64.exe",
    "EasyAntiCheat.exe", "EasyAntiCheat_EOS.exe", "start_protected_game.exe", "BEService.exe",
    "BEService_x64.exe", "service.exe", "steamerrorreporter.exe", "steamerrorreporter64.exe",
    "UE4PrereqSetup_x64.exe", "vc_redist.x64.exe", "dxsetup.exe",
)
DEFAULT_FULLSCREEN_IGNORE = (
    "chrome.exe", "msedge.exe", "brave.exe", "firefox.exe", "opera.exe",
    "zoom.exe", "ms-teams.exe", "teams.exe", "meetily.exe",
    "code.exe", "cursor.exe", "obsidian.exe",
)

_current: "ModelManager | None" = None


class GpuInterrupted(Exception):
    """The GPU was taken back. The job goes to the front of its queue and nothing is written."""


def current_manager() -> "ModelManager | None":
    return _current


def activate(manager: "ModelManager | None") -> None:
    global _current
    _current = manager


# ------------------------------------------------------------------ busy rules
@dataclass(frozen=True)
class GpuRules:
    poll_seconds: float = 5.0
    resume_after_seconds: float = 60.0
    vram_mb: int = 3000
    hotkey: str = "ctrl+alt+g"
    path_markers: tuple[str, ...] = PATH_MARKERS
    names: frozenset[str] = field(default_factory=lambda: frozenset(n.lower() for n in DEFAULT_NAMES))
    helpers: frozenset[str] = field(default_factory=lambda: frozenset(n.lower() for n in DEFAULT_HELPERS))
    fullscreen_ignore: frozenset[str] = field(
        default_factory=lambda: frozenset(n.lower() for n in DEFAULT_FULLSCREEN_IGNORE))


def rules_from_toml(raw: dict | None) -> GpuRules:
    raw = raw or {}
    defaults = GpuRules()
    names = raw.get("names")
    ignore = raw.get("fullscreen_ignore")
    paths = raw.get("paths")
    helpers = raw.get("helpers")
    return GpuRules(
        poll_seconds=float(raw.get("poll_seconds", defaults.poll_seconds)),
        resume_after_seconds=float(raw.get("resume_after_seconds", defaults.resume_after_seconds)),
        vram_mb=int(raw.get("vram_mb", defaults.vram_mb)),
        hotkey=str(raw.get("hotkey", defaults.hotkey) or ""),
        path_markers=tuple(paths) if paths else defaults.path_markers,
        names=frozenset(str(n).lower() for n in names) if names else defaults.names,
        helpers=frozenset(str(n).lower() for n in helpers) if helpers is not None else defaults.helpers,
        fullscreen_ignore=frozenset(str(n).lower() for n in ignore) if ignore else defaults.fullscreen_ignore,
    )


@dataclass(frozen=True)
class Proc:
    pid: int
    name: str
    exe: str


@dataclass(frozen=True)
class Foreground:
    process: str
    covers_screen: bool


@dataclass(frozen=True)
class BusyHit:
    label: str
    kind: str          # path | name | fullscreen | vram

    def paused_text(self) -> str:
        if self.kind == "vram":
            return "Paused: the GPU is busy - GPU freed"
        return f"Paused: {self.label} is running - GPU freed"


def _norm_path(path: str) -> str:
    return path.replace("/", "\\").lower()


def detect_busy(procs: Iterable[Proc], foreground: Foreground | None, other_vram_mb: int | None,
                ours_loaded: bool, rules: GpuRules, foreground_vram_mb: int | None = None) -> BusyHit | None:
    """The GPU counts as busy when a game, a fullscreen app on the GPU, or someone else's VRAM says so."""
    from .desktop import proc_key

    helpers = {proc_key(h) for h in rules.helpers}
    names = {proc_key(n) for n in rules.names}
    for proc in procs:
        exe = _norm_path(proc.exe or "")
        name = proc_key(proc.name or Path(proc.exe or "").name)
        if name in helpers:
            continue
        for marker in rules.path_markers:
            if _norm_path(marker) in exe:
                return BusyHit(proc.name or Path(proc.exe).name or "a game", "path")
        if name and name in names:
            return BusyHit(proc.name, "name")
    if foreground is not None and foreground.covers_screen:
        if proc_key(foreground.process) not in {proc_key(n) for n in rules.fullscreen_ignore}:
            # A maximized everyday window is not a game. The process also has to be using the GPU.
            if foreground_vram_mb is not None and foreground_vram_mb > FULLSCREEN_VRAM_MB:
                return BusyHit(foreground.process or "a fullscreen app", "fullscreen")
    if not ours_loaded and other_vram_mb is not None and other_vram_mb > rules.vram_mb:
        return BusyHit("the GPU", "vram")
    return None


def list_processes() -> list[Proc]:
    try:
        import psutil
    except ImportError:
        return []
    found = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        info = proc.info or {}
        found.append(Proc(int(info.get("pid") or 0), str(info.get("name") or ""), str(info.get("exe") or "")))
    return found


def foreground_window() -> Foreground | None:
    """True when the foreground window covers its monitor (fullscreen or borderless)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    if user32.IsIconic(hwnd):
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    MONITOR_DEFAULTTONEAREST = 2
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    mon = info.rcMonitor
    slack = 8
    covers = (rect.left <= mon.left + slack and rect.top <= mon.top + slack
              and rect.right >= mon.right - slack and rect.bottom >= mon.bottom - slack)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = ""
    try:
        import psutil
        name = psutil.Process(pid.value).name()
    except Exception:  # noqa: BLE001
        name = ""
    return Foreground(name, bool(covers))


# ------------------------------------------------------------------ pause file
@dataclass
class GpuState:
    paused: bool = False
    reason: str = ""
    manual: bool = False
    until: float | None = None
    inbox: int = 0
    recordings: int = 0
    phase: str = "idle"          # idle | processing | paused
    transcribing: bool = False
    transcribing_pid: int = 0

    @property
    def waiting(self) -> int:
        return int(self.inbox) + int(self.recordings)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "GpuState":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class GpuStateStore:
    """`_System/gpu-state.json`, shared by the watcher, the companion, and the CLI."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._thread = threading.RLock()

    @contextmanager
    def _exclusive(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        fh = open(lock_path, "a+b")
        try:
            if fh.seek(0, os.SEEK_END) == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    def _read(self) -> GpuState:
        if not self.path.is_file():
            return GpuState()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return GpuState()
        if not isinstance(data, dict):
            return GpuState()
        return GpuState.from_json(data)

    def _write(self, state: GpuState) -> None:
        payload = json.dumps(state.to_json(), ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    def load(self) -> GpuState:
        with self._thread, self._exclusive():
            return self._read()

    def update(self, **fields) -> GpuState:
        with self._thread, self._exclusive():
            state = self._read()
            for key, value in fields.items():
                if not hasattr(state, key):
                    raise AttributeError(key)
                setattr(state, key, value)
            if state.paused:
                state.phase = "paused"
            self._write(state)
            return state

    def claim_pause(self, *, reason: str, manual: bool, until: float | None) -> bool:
        """True when this call is the one that should toast and write the digest."""
        with self._thread, self._exclusive():
            state = self._read()
            changed = not (state.paused and state.reason == reason and state.manual == manual
                           and state.until == until)
            state.paused = True
            state.reason = reason
            state.manual = manual
            state.until = until
            state.phase = "paused"
            self._write(state)
            return changed

    def claim_auto_resume(self) -> bool:
        with self._thread, self._exclusive():
            state = self._read()
            if not state.paused or state.manual:
                return False
            state.paused = False
            state.reason = ""
            state.until = None
            state.phase = "idle"
            self._write(state)
            return True

    def force_resume(self) -> bool:
        with self._thread, self._exclusive():
            state = self._read()
            changed = state.paused or state.manual
            state.paused = False
            state.manual = False
            state.reason = ""
            state.until = None
            if state.phase == "paused":
                state.phase = "idle"
            self._write(state)
            return changed

    def transcription_active(self) -> bool:
        """True while a live process has said Whisper is running. A dead owner pid clears the flag."""
        if not self.path.is_file():
            return False
        state = self.load()
        if not state.transcribing:
            return False
        if pid_alive(state.transcribing_pid):
            return True
        self.update(transcribing=False, transcribing_pid=0)
        return False

    def clear_expired_hold(self, now: float) -> bool:
        """Drop a timed manual hold whose clock has run out. True when it was cleared."""
        with self._thread, self._exclusive():
            state = self._read()
            if not (state.manual and state.until is not None and now >= state.until):
                return False
            state.paused = False
            state.manual = False
            state.reason = ""
            state.until = None
            state.phase = "idle"
            self._write(state)
            return True


def transcribing_now(settings) -> bool:
    """True when a live process has a Whisper child running. Shared via gpu-state.json."""
    return GpuStateStore(Path(settings.system) / STATE_NAME).transcription_active()


def mark_transcribing(settings, active: bool) -> None:
    """Publish or clear the shared Whisper flag. The owner pid is this process."""
    store = GpuStateStore(Path(settings.system) / STATE_NAME)
    if active:
        store.update(transcribing=True, transcribing_pid=os.getpid())
        return
    if not store.path.is_file():
        return
    state = store.load()
    if state.transcribing_pid in (0, os.getpid()):
        store.update(transcribing=False, transcribing_pid=0)


def meeting_active(meetings_dir: Path, *, now: dt.datetime | None = None,
                   fresh_seconds: float = MEETING_FRESH_SECONDS) -> bool:
    """A companion session with no end time, seen recently, means a meeting is in progress."""
    folder = Path(meetings_dir)
    if not folder.is_dir():
        return False
    now = now or dt.datetime.now().astimezone()
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("end"):
            continue
        stamp = data.get("last_seen") or data.get("start")
        if not stamp:
            return True
        try:
            seen = dt.datetime.fromisoformat(str(stamp))
        except ValueError:
            return True
        if seen.tzinfo is None:
            seen = seen.astimezone()
        if abs((now - seen).total_seconds()) <= fresh_seconds:
            return True
    return False


# ------------------------------------------------------------------ LM Studio models
def _model_id(row: dict) -> str:
    return str(row.get("id") or row.get("modelKey") or row.get("identifier") or row.get("key") or "")


def is_embedding(row: dict) -> bool:
    kind = str(row.get("type") or row.get("model_type") or "").lower()
    if kind in ("embeddings", "embedding"):
        return True
    blob = " ".join(str(row.get(k) or "") for k in ("id", "identifier", "modelKey", "key", "path")).lower()
    return "embed" in blob or "bge-m3" in blob


def is_chat(row: dict) -> bool:
    if is_embedding(row):
        return False
    kind = str(row.get("type") or row.get("model_type") or "").lower()
    return kind in ("", "llm", "vlm")


def _loaded(row: dict) -> bool:
    return str(row.get("state") or row.get("status") or "").lower() in ("loaded", "running")


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def gpu_offload_detail(row: dict) -> str | None:
    """How this loaded model is using the GPU, or None when it looks CPU-only / unknown."""
    for key in ("gpuLayers", "gpu_layers", "gpuOffloadedLayers"):
        if key in row and _number(row[key]) > 0:
            return f"{row[key]} GPU layers"
    for key in ("gpuOffloadRatio", "gpu_offload", "gpu_ratio"):
        if key in row and _number(row[key]) > 0:
            return f"GPU offload {row[key]}"
    return None


def embedding_offenders(rows: Iterable[dict]) -> list[tuple[str, str]]:
    found = []
    for row in rows:
        if not isinstance(row, dict) or not is_embedding(row):
            continue
        if not _loaded(row) and "state" in row and str(row.get("state")).lower() not in ("loaded", "running"):
            # `lms ps` only lists loaded models and often has no state field
            if "state" in row or "status" in row:
                continue
        detail = gpu_offload_detail(row)
        if detail:
            found.append((_model_id(row) or "embedding", detail))
    return found


def choose_chat_model(configured: str, models: list[dict]) -> str | None:
    configured = (configured or "auto").strip()
    chats = [m for m in models if is_chat(m) and _model_id(m)]
    if configured.lower() != "auto":
        return configured
    loaded = [m for m in chats if _loaded(m)]
    if loaded:
        return _model_id(loaded[0])
    if chats:
        return _model_id(chats[0])
    return None


def fits_in_dedicated(used: int | None, total: int | None) -> bool | None:
    """True when the card still has dedicated memory free. None when nvidia-smi gave no numbers."""
    if used is None or total is None or total <= 0:
        return None
    return used < total


def parse_vram_mb(text: str) -> int | None:
    for line in (text or "").splitlines():
        match = re.search(r"(\d+)", line)
        if match:
            return int(match.group(1))
    return None


def parse_compute_apps(text: str) -> list[tuple[str, str, int]]:
    """pid, process name, and MB from `nvidia-smi --query-compute-apps` csv."""
    rows = []
    for line in (text or "").splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 3:
            continue
        pid, mem = parts[0], parts[-1]
        name = ",".join(parts[1:-1]).strip()
        match = re.search(r"(\d+)", mem)
        if not pid or not match:
            continue
        rows.append((pid, name or "?", int(match.group(1))))
    return rows


def embedding_status(rows: Iterable[dict]) -> str:
    """One status line. GPU layers come from the row; otherwise the model is on CPU."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if not isinstance(row, dict) or not is_embedding(row):
            continue
        mid = _model_id(row)
        if mid:
            grouped.setdefault(mid, []).append(row)
    parts = []
    for mid, group in grouped.items():
        loaded = any(_loaded(r) or ("state" not in r and "status" not in r) for r in group)
        if not loaded:
            continue
        detail = next((gpu_offload_detail(r) for r in group if gpu_offload_detail(r)), None)
        parts.append(f"{mid} ({detail})" if detail else f"{mid} (on CPU)")
    if not parts:
        return "Embedding: none loaded"
    return "Embedding: " + ", ".join(parts)


def parse_other_vram_mb(text: str, own_pids: set[int]) -> int:
    total = 0
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
            used = int(re.search(r"(\d+)", parts[-1]).group(1))  # type: ignore[union-attr]
        except (TypeError, ValueError, AttributeError):
            continue
        if pid not in own_pids:
            total += used
    return total


class WorkGate:
    """Set by the guard (or the hotkey) so an in-flight LM Studio request or Whisper pass stops."""

    def __init__(self):
        self.cancel = threading.Event()
        self._clients: list = []
        self._lock = threading.Lock()

    def bind(self, client) -> None:
        with self._lock:
            if client not in self._clients:
                self._clients.append(client)

    def trip(self) -> None:
        self.cancel.set()
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                log.debug("Could not close the LM Studio client", exc_info=True)

    def clear(self) -> None:
        self.cancel.clear()

    def check(self) -> None:
        if self.cancel.is_set():
            raise GpuInterrupted("GPU hold")


def finish_chat(manager: "ModelManager", *, more_waiting: bool, interrupted: bool) -> None:
    """Drop the chat model after a failed job, an interrupt, or the last job in the queue."""
    if interrupted or not more_waiting:
        manager.unload_chat()


class ModelManager:
    """Load and unload the one chat model this tool is allowed to touch."""

    def __init__(self, settings, *, run: Callable | None = None, which: Callable | None = None,
                 models_fn: Callable[[], list[dict]] | None = None,
                 ps_fn: Callable[[], list[dict]] | None = None,
                 nvidia: Callable[[list[str]], str | None] | None = None):
        self.s = settings
        self._run = run or subprocess.run
        self._which = which or shutil.which
        self._models_fn = models_fn
        self._ps_fn = ps_fn
        self._nvidia = nvidia
        self._loaded_id: str | None = None
        self._held = False                 # we loaded it (or decided to keep it) for this process
        self.whisper_active = False
        self.gate: WorkGate | None = None
        self._exit_hook: Callable[[], None] | None = None

    @property
    def managing(self) -> bool:
        return self.s.effective_mode == "local" and bool(getattr(self.s, "local_manage_models", True))

    def ours_loaded(self) -> bool:
        if self.whisper_active:
            return True
        if not self.managing:
            return False
        if self._loaded_id and self._is_loaded(self._loaded_id):
            return True
        return False

    # ---- nvidia-smi
    def _nvidia_text(self, args: list[str]) -> str | None:
        if self._nvidia is not None:
            return self._nvidia(args)
        exe = self._which("nvidia-smi")
        if not exe:
            return None
        label = "nvidia-smi " + " ".join(args[:1])
        try:
            proc = run_external([exe, *args], timeout=15, label=label, run=self._run)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.info("nvidia-smi failed: %s", e)
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout or ""

    def vram_of(self, process_name: str) -> int | None:
        """Dedicated GPU memory of one process, or None when nvidia-smi did not list it."""
        text = self._nvidia_text(["--query-compute-apps=pid,process_name,used_gpu_memory",
                                  "--format=csv,noheader,nounits"])
        if text is None:
            return None
        want = (process_name or "").lower()
        best = None
        for _pid, name, mb in parse_compute_apps(text):
            if Path(name).name.lower() == want or name.lower() == want:
                best = mb if best is None else max(best, mb)
        return best

    def vram_mb(self) -> int | None:
        used, _total = self.vram_used_total()
        return used

    def vram_used_total(self) -> tuple[int | None, int | None]:
        text = self._nvidia_text(["--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"])
        if not text:
            return None, None
        parts = [p.strip() for p in text.split(",")]
        if len(parts) < 2:
            return parse_vram_mb(text), None
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None, None

    def other_vram_mb(self) -> int | None:
        text = self._nvidia_text(["--query-compute-apps=pid,used_gpu_memory", "--format=csv,noheader,nounits"])
        if text is None:
            return None
        return parse_other_vram_mb(text, {os.getpid()})

    def log_vram(self, when: str) -> None:
        used = self.vram_mb()
        if used is None:
            log.info("VRAM %s: nvidia-smi unavailable", when)
        else:
            log.info("VRAM %s: %d MB", when, used)

    def log_dedicated(self, when: str) -> None:
        """Dedicated used/total, then every compute app holding some of it."""
        used, total = self.vram_used_total()
        if used is None:
            log.info("Dedicated VRAM %s: nvidia-smi unavailable", when)
        elif total is None:
            log.info("Dedicated VRAM %s: %d MB", when, used)
        else:
            log.info("Dedicated VRAM %s: %d / %d MB", when, used, total)
        self.log_gpu_processes(when)

    def log_gpu_processes(self, when: str) -> None:
        text = self._nvidia_text(["--query-compute-apps=pid,process_name,used_gpu_memory",
                                  "--format=csv,noheader,nounits"])
        if text is None:
            log.info("GPU processes %s: nvidia-smi unavailable", when)
            return
        apps = parse_compute_apps(text)
        if not apps:
            log.info("GPU processes %s: none", when)
            return
        detail = "; ".join(f"{name} pid {pid} {mb} MB" for pid, name, mb in apps)
        log.info("GPU processes %s: %s", when, detail)

    def log_embedding_placement(self) -> None:
        """Say whether each loaded embedding model is on the CPU. A GPU one gets the existing warning."""
        rows = list(self.list_models()) + list(self.ps())
        offenders = {mid for mid, _detail in embedding_offenders(rows)}
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or not is_embedding(row):
                continue
            mid = _model_id(row)
            if not mid or mid in seen:
                continue
            loaded = _loaded(row) or ("state" not in row and "status" not in row)
            if not loaded:
                continue
            seen.add(mid)
            if mid not in offenders:
                log.info("Embedding %s is on CPU.", mid)
        self.check_embeddings()

    # ---- lms / REST
    def _lms(self, *args: str, timeout: float = 180):
        exe = self._which("lms")
        if not exe:
            from .llm import LLMUnavailable
            raise LLMUnavailable("The `lms` command is not on PATH, so the chat model cannot be managed.")
        label = "lms " + " ".join(args)
        try:
            proc = run_external([exe, *args], timeout=timeout, label=label, run=self._run)
        except (OSError, subprocess.TimeoutExpired) as e:
            from .llm import LLMUnavailable
            raise LLMUnavailable(f"`lms {' '.join(args)}` failed: {e}") from e
        out = " ".join(((proc.stdout or "") + " " + (proc.stderr or "")).split())[-400:]
        if proc.returncode != 0:
            from .llm import LLMUnavailable
            raise LLMUnavailable(f"`lms {' '.join(args)}` exited {proc.returncode}: {out}")
        if out:
            log.info("lms %s: %s", args[0], out)
        return proc

    def _root(self) -> str:
        base = (self.s.openai_compat_base_url or "http://localhost:1234/v1").rstrip("/")
        return re.sub(r"/v1$", "", base)

    def list_models(self) -> list[dict]:
        if self._models_fn is not None:
            return list(self._models_fn())
        url = f"{self._root()}/api/v0/models"
        try:
            response = http_call("GET", url, timeout=10, label="LM Studio model list")
        except httpx.HTTPError as e:
            log.info("LM Studio model list is not reachable: %s", e)
            return []
        if response.status_code != 200:
            return []
        try:
            data = response.json().get("data") or []
        except ValueError:
            return []
        return [m for m in data if isinstance(m, dict)]

    def ps(self) -> list[dict]:
        if self._ps_fn is not None:
            return list(self._ps_fn())
        exe = self._which("lms")
        if not exe:
            return []
        try:
            proc = run_external([exe, "ps", "--json"], timeout=30, label="lms ps", run=self._run)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []
        try:
            data = json.loads(proc.stdout or "[]")
        except ValueError:
            return []
        if isinstance(data, dict):
            data = data.get("models") or data.get("data") or []
        return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []

    def server_reachable(self, timeout: float = 3) -> bool:
        """True when port 1234 answers. A missing server is not an error."""
        if self._models_fn is not None:
            return True
        try:
            http_call("GET", f"{self._root()}/api/v0/models", timeout=timeout, label="LM Studio port 1234")
            return True
        except httpx.HTTPError:
            return False

    def ensure_server(self) -> None:
        if self.server_reachable():
            return
        log.info("LM Studio server is not reachable; trying `lms server start`.")
        try:
            self._lms("server", "start", timeout=SERVER_START_TIMEOUT)
        except Exception as e:  # noqa: BLE001  start can time out or exit non-zero
            from .llm import LLMUnavailable
            raise LLMUnavailable(SERVER_START_FAILED) from e
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.server_reachable():
                log.info("LM Studio server is up.")
                return
            time.sleep(1)
        from .llm import LLMUnavailable
        raise LLMUnavailable(SERVER_START_FAILED)

    def _is_loaded(self, model_id: str, models: list[dict] | None = None) -> bool:
        rows = self.list_models() if models is None else models
        want = model_id.lower()
        return any(_loaded(m) and _model_id(m).lower() == want for m in rows)

    def _wait_until(self, model_id: str, loaded: bool, timeout: float = 120) -> bool:
        deadline = time.time() + timeout
        while True:
            if self._is_loaded(model_id) == loaded:
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.5)

    def _register_exit(self) -> None:
        if self._exit_hook is not None:
            return

        def hook() -> None:
            try:
                self.unload_chat()
            except Exception:  # noqa: BLE001
                log.debug("atexit unload failed", exc_info=True)

        self._exit_hook = hook
        atexit.register(hook)

    def managed_loaded_ids(self, models: list[dict] | None = None) -> list[str]:
        rows = self.list_models() if models is None else models
        configured = (self.s.llm_model or "auto").strip()
        found = []
        for row in rows:
            if not is_chat(row) or not _loaded(row):
                continue
            mid = _model_id(row)
            if not mid:
                continue
            if configured.lower() != "auto" and mid != configured:
                continue
            found.append(mid)
        return found

    def ensure_loaded(self) -> str | None:
        """Start the server if needed and load the chat model. No-op when management is off."""
        if not self.managing:
            return None
        if self.gate is not None:
            self.gate.check()
        if paused_now(self.s) or transcribing_now(self.s):
            why = "paused" if paused_now(self.s) else "transcription"
            log.info("Not loading the chat model (%s).", why)
            raise GpuInterrupted(why)
        if self._held and self._loaded_id and self._is_loaded(self._loaded_id):
            log.info("Chat model %s is already loaded.", self._loaded_id)
            return self._loaded_id
        self.ensure_server()
        models = self.list_models()
        model_id = choose_chat_model(self.s.llm_model, models)
        if not model_id:
            from .llm import NoModelLoaded
            raise NoModelLoaded("Load a model in LM Studio (Developer tab) and start the server.")
        context = int(getattr(self.s, "local_context", 8192) or 8192)
        self.log_embedding_placement()
        self.log_dedicated("before load")
        # One slot: LM Studio's default of 4 copies the KV cache and, at 16k, spilled this 8 GB card.
        self._lms("load", model_id, "--gpu", "max", "--context-length", str(context),
                  "--parallel", "1", timeout=300)
        if not self._wait_until(model_id, True):
            log.warning("`lms load %s` finished, but the model did not report state=loaded.", model_id)
        self._loaded_id = model_id
        self._held = True
        self._register_exit()
        self.log_vram("after load")
        log.info("Chat model %s is ready (context %d, GPU max, parallel 1).", model_id, context)
        if self.gate is not None and self.gate.cancel.is_set():
            self.unload_chat()
            raise GpuInterrupted("GPU hold")
        return model_id

    def unload_chat(self) -> None:
        """Unload the chat model we manage. Embeddings are never unloaded. Safe to call twice."""
        if not self.managing:
            return
        if not self.server_reachable():
            log.info("LM Studio is not running; skipping unload.")
            self._loaded_id = None
            self._held = False
            return
        targets = []
        if self._loaded_id:
            targets.append(self._loaded_id)
        else:
            targets.extend(self.managed_loaded_ids())
        if not targets:
            self._held = False
            self._loaded_id = None
            return
        for model_id in targets:
            if not self._is_loaded(model_id):
                log.info("Chat model %s is not loaded.", model_id)
                continue
            log.info("Unloading chat model %s.", model_id)
            try:
                self._lms("unload", model_id)
            except Exception:  # noqa: BLE001
                log.warning("Could not unload chat model %s", model_id, exc_info=True)
                continue
            self._wait_until(model_id, False, timeout=60)
            self.log_vram("after unload")
        self._loaded_id = None
        self._held = False

    def check_embeddings(self) -> None:
        rows = list(self.list_models()) + list(self.ps())
        for model_id, detail in embedding_offenders(rows):
            log.warning(
                "Embedding model %s is using the GPU (%s). It should stay on the CPU. "
                "Fix: lms unload %s && lms load %s --gpu off",
                model_id, detail, model_id, model_id)

    def startup_cleanup(self) -> bool:
        """Unload a leftover chat model. False when LM Studio could not be reached, so the caller can try later."""
        if not self.server_reachable():
            log.info("LM Studio is not reachable; the startup check will be tried again.")
            return False
        try:
            self.check_embeddings()
        except Exception:  # noqa: BLE001
            log.debug("Could not check embedding GPU placement", exc_info=True)
        if not self.managing:
            return True
        for model_id in self.managed_loaded_ids():
            log.info("Unloading leftover chat model %s (no job is running).", model_id)
            self._loaded_id = model_id
            self.unload_chat()
        return True

    def close(self) -> None:
        try:
            self.unload_chat()
        finally:
            if self._exit_hook is not None:
                atexit.unregister(self._exit_hook)
                self._exit_hook = None


# ------------------------------------------------------------------ guard, tray, toasts
def show_toast(text: str, on_click: Callable[[], None] | None = None, *,
               open_path: str | None = None, click_file: str | None = None) -> None:
    """Toast from a child process. `open_path` is opened on click; `click_file` is created on click."""
    del on_click  # clicks are handled in the child so this process never loads winrt
    try:
        subprocess.Popen(
            [sys.executable, "-m", "vault_ingest.notify", text[:200], open_path or "", click_file or ""],
            creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001
        log.info("Toast unavailable (%s): %s", e, text)


def append_digest(vault, system: Path, title: str) -> None:
    path = Path(system) / "digest.md"
    stamp = dt.datetime.now().astimezone().replace(microsecond=0).strftime("%Y-%m-%d %H:%M")
    entry = f"## {stamp} — {title}\n"
    if path.is_file():
        text = path.read_text(encoding="utf-8").rstrip() + "\n\n" + entry
    else:
        text = ("# Ingest digest\n\nCalls moved after they were processed, "
                "and GPU pause/resume events.\n\n" + entry)
    if not text.endswith("\n"):
        text += "\n"
    if vault is not None:
        vault.write(path, text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _icon_image(color: tuple[int, int, int]):
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (64, 64), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill=color)
    return image


class GpuService:
    """Polls for a busy GPU, pauses both queues, and unloads what this process loaded."""

    def __init__(self, settings, rules: GpuRules, vault, *, is_companion: bool = False,
                 detect: Callable[[], BusyHit | None] | None = None,
                 meetings_dir: Path | None = None):
        self.s = settings
        self.rules = rules
        self.vault = vault
        self.is_companion = is_companion
        self.manager = ModelManager(settings)
        self.manager.gate = WorkGate()
        self.gate = self.manager.gate
        self.store = GpuStateStore(Path(settings.system) / STATE_NAME)
        self.meetings_dir = Path(meetings_dir) if meetings_dir is not None else Path(settings.system) / "meetings"
        self._detect = detect
        self._idle_since: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._icon = None
        self._icons: dict = {}
        self._hotkeys = None
        self.own_ui = False
        self.server_checked = False
        # True once the chat model was unloaded for the current pause. Every later tick of the same pause
        # skips the unload (it used to hit LM Studio twice every few seconds for the whole pause).
        self._freed = False
        # (label, action, default) items the companion puts at the top of the tray menu; `default` runs on click
        self.extra_menu: list[tuple[str, Callable[[], None], bool]] = []

    def start(self, *, tray: bool = False) -> None:
        activate(self.manager)
        try:
            self.server_checked = bool(self.manager.startup_cleanup())
        except Exception:  # noqa: BLE001
            log.exception("Could not clean up a leftover chat model")
            self.server_checked = False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="gpu-guard", daemon=True)
        self._thread.start()
        if tray:
            self.claim_ui()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.manager.close()
        except Exception:  # noqa: BLE001
            log.exception("Could not unload the chat model while stopping")
        if current_manager() is self.manager:
            activate(None)
        self._drop_tray(announce=False)

    def retry_startup_cleanup(self) -> bool:
        """One more leftover-model check. Stops for good once LM Studio has answered."""
        if self.server_checked:
            return True
        try:
            self.server_checked = bool(self.manager.startup_cleanup())
        except Exception:  # noqa: BLE001
            log.exception("Could not clean up a leftover chat model")
            self.server_checked = False
        return self.server_checked

    def start_blocked(self, now: dt.datetime | None = None) -> str | None:
        """Why a new GPU job must wait, or None when it may start. Does not cancel a running job."""
        if self.store.transcription_active():
            return "transcription"
        state = self.store.load()
        if state.paused:
            return state.reason or "paused"
        if meeting_active(self.meetings_dir, now=now):
            return "meeting"
        return None

    def set_counts(self, *, inbox: int | None = None, recordings: int | None = None,
                   phase: str | None = None) -> None:
        fields = {}
        if inbox is not None:
            fields["inbox"] = inbox
        if recordings is not None:
            fields["recordings"] = recordings
        if phase is not None:
            fields["phase"] = phase
        if fields:
            state = self.store.update(**fields)
            self._paint(state.phase)

    def detect(self) -> BusyHit | None:
        if self._detect is not None:
            return self._detect()
        try:
            procs = list_processes()
            # Cheap check first: a known game process answers without asking nvidia-smi
            # (running nvidia-smi every few seconds during a game can itself cause hitches).
            quick = detect_busy(procs, None, None, True, self.rules)
            if quick is not None:
                return quick
            foreground = foreground_window()
            foreground_vram = self.manager.vram_of(foreground.process) if foreground and foreground.process else None
            ours = self.manager.ours_loaded() or self.store.transcription_active()
            return detect_busy(procs, foreground, self.manager.other_vram_mb(),
                               ours, self.rules, foreground_vram_mb=foreground_vram)
        except Exception:  # noqa: BLE001
            log.exception("GPU busy check failed")
            return None

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if not self.is_companion and self.own_ui and _companion_running(self.s.system / "companion.lock"):
            self._drop_tray(announce=True)
        if self.store.clear_expired_hold(now):
            log.info("The one-hour GPU hold ended.")
            self.gate.clear()
            self._idle_since = None
            if self.detect() is None:
                waiting = self.store.load().waiting
                self._announce(f"Resumed processing ({waiting} calls waiting)")
                self._paint("idle")
                return
        state = self.store.load()
        if state.manual and state.paused:
            self.gate.trip()
            self._free_once()
            self._paint("paused")
            return
        hit = self.detect()
        if hit is not None:
            self._idle_since = None
            self._pause(hit.paused_text(), manual=False, until=None)
            return
        state = self.store.load()
        if state.paused and not state.manual:
            if self._idle_since is None:
                self._idle_since = now
                log.info("GPU is free; processing resumes in %.0f seconds if it stays free.",
                         self.rules.resume_after_seconds)
            elif now - self._idle_since >= self.rules.resume_after_seconds:
                self._auto_resume()
            return
        self._idle_since = None
        if not state.paused:
            self._freed = False
            self.gate.clear()
            self._paint(state.phase if state.phase != "paused" else "idle")

    def free(self, *, hours: float | None) -> None:
        until = None if hours is None else time.time() + hours * 3600
        reason = "until you resume" if hours is None else "for 1 hour"
        text = "Paused: GPU held until you resume" if hours is None else "Paused: GPU held for 1 hour"
        self._pause(text, manual=True, until=until, reason=reason)

    def resume(self) -> None:
        self._freed = False
        waiting = self.store.load().waiting
        changed = self.store.force_resume()
        self._idle_since = None
        self.gate.clear()
        self._paint("idle")
        if changed:
            self._announce(f"Resumed processing ({waiting} calls waiting)")

    def toggle(self) -> None:
        if self.store.load().paused:
            self.resume()
        else:
            self.free(hours=None)

    def status_lines(self) -> list[str]:
        return format_status(self.manager, self.store.load())

    def _pause(self, text: str, *, manual: bool, until: float | None, reason: str | None = None) -> None:
        why = reason if reason is not None else text
        changed = self.store.claim_pause(reason=why, manual=manual, until=until)
        self.gate.trip()
        if changed:
            self._freed = False
        self._free_once()
        self._paint("paused")
        if changed:
            self._announce(text)

    def _free_once(self) -> None:
        """Unload the chat model once per pause, not on every poll."""
        if self._freed:
            return
        try:
            self.manager.unload_chat()
        except Exception:  # noqa: BLE001
            log.exception("Could not unload the chat model while pausing")
        self._freed = True

    def _auto_resume(self) -> None:
        waiting = self.store.load().waiting
        if not self.store.claim_auto_resume():
            self.gate.clear()
            return
        self._idle_since = None
        self.gate.clear()
        self._paint("idle")
        self._announce(f"Resumed processing ({waiting} calls waiting)")

    def _announce(self, text: str) -> None:
        log.info("%s", text)
        show_toast(text)
        try:
            append_digest(self.vault, self.s.system, text)
        except Exception:  # noqa: BLE001
            log.exception("Could not append the GPU event to the digest")

    def _loop(self) -> None:
        while not self._stop.wait(self.rules.poll_seconds):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("GPU check failed")

    def claim_ui(self) -> bool:
        """One tray icon and one pause hotkey. The companion wins when it is running."""
        if not self.is_companion and _companion_running(self.s.system / "companion.lock"):
            return False
        self.own_ui = True
        self.start_tray()
        return True

    def attach_hotkey(self, start_hotkeys) -> None:
        if not self.own_ui or not self.rules.hotkey or self._hotkeys is not None:
            return
        self._hotkeys = start_hotkeys({self.rules.hotkey: self.toggle})

    def _tray_actions(self) -> list[tuple[str, Callable[[], None], bool] | None]:
        """The menu, top to bottom. None is a separator."""
        items: list = list(self.extra_menu)
        if items:
            items.append(None)
        items += [("Free GPU for 1 hour", lambda: self.free(hours=1), False),
                  ("Free GPU until I resume", lambda: self.free(hours=None), False),
                  ("Resume", self.resume, False),
                  ("Status", lambda: show_toast(" | ".join(self.status_lines())), False)]
        return items

    def _start_child_tray(self) -> None:
        """macOS and Linux: the icon runs in a helper process (macOS only allows it on a main thread)."""
        from .ui_child import ChildUI

        actions = self._tray_actions()
        spec_items: list = []
        by_id: dict[str, Callable[[], None]] = {}
        for i, item in enumerate(actions):
            if item is None:
                spec_items.append("-")
                continue
            label, action, default = item
            by_id[str(i)] = action
            spec_items.append({"id": str(i), "label": label, "default": default})

        def on_event(kind: str, item_id: str) -> None:
            action = by_id.get(item_id)
            if kind == "click" and action is not None:
                threading.Thread(target=self._safe, args=(action,), name="tray-action", daemon=True).start()

        try:
            self._icon = ChildUI("tray", {"title": "Vault ingest", "icon": "idle", "items": spec_items}, on_event)
        except Exception as e:  # noqa: BLE001
            log.warning("No tray icon (%s). Install pystray, and on Linux a tray: "
                        "libayatana-appindicator or a desktop with a system tray.", e)
            self._icon = None
            return
        self._icons = {"idle": "idle", "processing": "processing", "paused": "paused"}
        log.info("Tray icon is up. Hotkey %s toggles pause.", self.rules.hotkey or "(off)")

    def start_tray(self) -> None:
        if self._icon is not None:
            return
        if sys.platform != "win32":
            self._start_child_tray()
            return
        try:
            import pystray
        except ImportError:
            log.warning("pystray is not installed, so there is no tray icon. Run: pip install pystray")
            return
        try:
            self._icons = {
                "idle": _icon_image((120, 120, 120)),
                "processing": _icon_image((70, 130, 220)),
                "paused": _icon_image((220, 160, 40)),
            }
        except Exception as e:  # noqa: BLE001
            log.warning("Could not draw the tray icon: %s", e)
            return
        def run(action):
            def handler(_icon, _item):
                threading.Thread(target=self._safe, args=(action,), name="tray-action", daemon=True).start()
            return handler

        extra = [pystray.MenuItem(label, run(action), default=default) for label, action, default in self.extra_menu]
        if extra:
            extra.append(pystray.Menu.SEPARATOR)
        menu = pystray.Menu(
            *extra,
            pystray.MenuItem("Free GPU for 1 hour", lambda _icon, _item: self.free(hours=1)),
            pystray.MenuItem("Free GPU until I resume", lambda _icon, _item: self.free(hours=None)),
            pystray.MenuItem("Resume", lambda _icon, _item: self.resume()),
            pystray.MenuItem("Status", lambda _icon, _item: show_toast(" | ".join(self.status_lines()))),
        )
        self._icon = pystray.Icon("vault-ingest", self._icons["idle"], "Vault ingest", menu=menu)
        threading.Thread(target=self._icon.run, name="vault-ingest-tray", daemon=True).start()
        log.info("Tray icon is up. Hotkey %s toggles pause.", self.rules.hotkey or "(off)")

    @staticmethod
    def _safe(action: Callable[[], None]) -> None:
        try:
            action()
        except Exception:  # noqa: BLE001
            log.exception("Tray action failed")

    def _drop_tray(self, *, announce: bool) -> None:
        had_icon = self._icon is not None
        self.own_ui = False
        if self._hotkeys is not None:
            try:
                self._hotkeys.stop()
            except Exception:  # noqa: BLE001
                log.debug("Could not stop the pause hotkey", exc_info=True)
            self._hotkeys = None
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                log.debug("Could not stop the tray icon", exc_info=True)
            self._icon = None
        if announce and had_icon:
            log.info("The meeting companion has the tray icon.")

    def _paint(self, phase: str) -> None:
        if self._icon is None or phase not in self._icons:
            return
        if hasattr(self._icon, "send"):                     # helper-process tray (macOS, Linux)
            self._icon.send(f"icon {phase}")
            self._icon.send(f"title Vault ingest — {phase}")
            return
        try:
            self._icon.icon = self._icons[phase]
            self._icon.title = f"Vault ingest — {phase}"
        except Exception:  # noqa: BLE001
            log.debug("Could not update the tray icon", exc_info=True)


def format_status(manager: ModelManager, state: GpuState) -> list[str]:
    try:
        models = manager.list_models()
    except Exception:  # noqa: BLE001
        models = []
    loaded = manager.managed_loaded_ids(models)
    configured = (manager.s.llm_model or "auto").strip()
    if loaded:
        chat = ", ".join(loaded) + " (loaded)"
    elif configured.lower() == "auto":
        chat = "none loaded"
    else:
        chat = f"{configured} (not loaded)"
    try:
        extra = manager.ps()
    except Exception:  # noqa: BLE001
        extra = []
    vram = manager.vram_mb()
    if state.paused and state.manual and state.until is None:
        why = f"yes — {state.reason} (until you resume)"
    elif state.paused and state.manual and state.until is not None:
        left = max(0, int(state.until - time.time()))
        why = f"yes — {state.reason} ({left // 60} min left)"
    elif state.paused:
        why = f"yes — {state.reason} (resumes 60s after the GPU is free)"
    else:
        why = "no"
    lines = [
        f"Chat model: {chat}",
        embedding_status(list(models) + list(extra)),
        f"VRAM: {vram if vram is not None else 'unknown'} MB",
        f"Queue: {state.inbox} inbox, {state.recordings} recordings",
        f"Paused: {why}",
        f"Phase: {state.phase}",
    ]
    return lines


def _companion_running(lock_path: Path) -> bool:
    if not lock_path.is_file():
        return False
    try:
        pid = int(lock_path.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return False
    if pid == os.getpid():
        return False
    try:
        import psutil
        if not psutil.pid_exists(pid):
            return False
        cmd = " ".join(psutil.Process(pid).cmdline()).lower()
    except Exception:  # noqa: BLE001
        return False
    return "companion" in cmd


def open_service(settings, vault, *, is_companion: bool) -> GpuService:
    rules = GpuRules()
    try:
        from .watcher_meetings import DEFAULT_CONFIG, load_config
        if DEFAULT_CONFIG.is_file():
            rules = load_config().gpu
    except SystemExit as e:
        log.warning("%s", e)
    except Exception:  # noqa: BLE001
        log.exception("Could not read GPU rules from companion.toml")
    return GpuService(settings, rules, vault, is_companion=is_companion)
