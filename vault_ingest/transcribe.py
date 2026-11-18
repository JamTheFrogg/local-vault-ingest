"""Local transcription with faster-whisper. The packages load only when a transcription actually runs."""
from __future__ import annotations

import datetime as dt
import gc
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .parsers import AUDIO_EXTENSIONS
from .sessions import hms

log = logging.getLogger("vault_ingest")

LOGPROB_MIN = -1.0
NO_SPEECH_MAX = 0.6
MERGE_GAP_SECONDS = 2.0
MAX_LINE_SECONDS = 30.0          # a line never covers more than this, so chunk times stay usable
PROMPT_CHAR_BUDGET = 800          # about 200 tokens
TEMPERATURES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
_ENGINE_LOCK = threading.Lock()


class TranscriptionError(RuntimeError):
    pass


class TranscriberUnavailable(TranscriptionError):
    """Whisper cannot run right now (missing package, GPU libraries, or model). Leave the file for later."""


@dataclass
class ChildRun:
    """One Whisper child. `cancelled` means the parent stopped it; that is not a crash to retry."""

    returncode: int
    stderr: str
    payload: dict | None
    cancelled: bool = False


STDERR_TAIL_LINES = 20
DOCTOR_LOADS = (("cuda", "float16"), ("cuda", "int8_float16"), ("cpu", "int8"))


@dataclass
class Segment:
    start: float
    end: float
    text: str
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    speaker: str | None = None


@dataclass
class Transcript:
    text: str
    language: str
    model: str
    device: str
    compute_type: str
    seconds: float = 0.0

    @property
    def transcribed_by(self) -> str:
        return f"faster-whisper/{self.model}/{self.language}"


def whisper_options(language: str, initial_prompt: str | None = None, clip_end: float | None = None) -> dict:
    """Decode options. `language` is always a real code: auto-detect is refused."""
    code = (language or "").strip().lower()
    if not code or code == "auto":
        raise ValueError("TRANSCRIBE_LANGUAGE must be a language code such as 'hr' (auto-detect is disabled)")
    opts = {
        "language": code,
        "vad_filter": True,
        "condition_on_previous_text": False,
        "beam_size": 5,
        "temperature": list(TEMPERATURES),
    }
    if initial_prompt:
        opts["initial_prompt"] = initial_prompt
    if clip_end:
        opts["clip_timestamps"] = f"0,{clip_end:g}"
    return opts


def is_hallucination(seg: Segment, previous: str | None) -> bool:
    text = " ".join(seg.text.split())
    if not text:
        return True
    if seg.avg_logprob < LOGPROB_MIN or seg.no_speech_prob > NO_SPEECH_MAX:
        return True
    return previous is not None and text.casefold() == previous.casefold()


def format_segments(segments: Iterable[Segment], merge_gap: float = MERGE_GAP_SECONDS,
                    max_line_seconds: float = MAX_LINE_SECONDS) -> str:
    """`[HH:MM:SS] text` lines. Close segments of the same speaker join the previous line.

    A line is never longer than `max_line_seconds` (a single Whisper segment that is already
    longer stays one line). That keeps a timestamp at least that often through continuous speech.
    """
    lines: list[list] = []
    for seg in segments:
        text = " ".join(seg.text.split())
        seg = Segment(seg.start, seg.end, text, seg.avg_logprob, seg.no_speech_prob, seg.speaker)
        if is_hallucination(seg, lines[-1][2] if lines else None):
            continue
        if (lines and lines[-1][3] == seg.speaker and seg.start - lines[-1][1] < merge_gap
                and seg.end - lines[-1][0] <= max_line_seconds):
            lines[-1][2] = f"{lines[-1][2]} {text}"
            lines[-1][1] = seg.end
            continue
        lines.append([seg.start, seg.end, text, seg.speaker])
    out = []
    for start, _end, text, speaker in lines:
        who = f"{speaker}: " if speaker else ""
        out.append(f"[{hms(start)}] {who}{text}")
    return "\n".join(out) + ("\n" if out else "")


def interleave(tracks: list[tuple[str, list[Segment]]]) -> list[Segment]:
    """Merge tracks in time order. `tracks` is (speaker label, segments)."""
    tagged = []
    for speaker, segs in tracks:
        for seg in segs:
            tagged.append(Segment(seg.start, seg.end, seg.text, seg.avg_logprob, seg.no_speech_prob, speaker))
    tagged.sort(key=lambda s: (s.start, s.speaker or ""))
    return tagged


def glossary_terms(text: str) -> list[str]:
    terms = []
    for line in text.splitlines():
        item = re.sub(r"^\s*[-*]\s*", "", line).strip()
        if item and item != line.strip() and not item.startswith("#"):
            terms.append(item)
    return terms


def build_initial_prompt(glossary: str, clients: Iterable[str], budget: int = PROMPT_CHAR_BUDGET) -> str:
    """Vocabulary hint for Whisper, cut to about 200 tokens. Not instructions."""
    parts: list[str] = []
    used = 0
    for term in glossary_terms(glossary):
        piece = (", " if parts else "") + term
        if used + len(piece) > budget:
            break
        parts.append(term)
        used += len(piece)
    body = ", ".join(parts)
    names = [c.strip() for c in clients if c and c.strip()]
    if names and used + len(" Clients: ") < budget:
        extra = ""
        for name in names:
            add = (", " if extra else "") + name
            if used + len(" Clients: ") + len(extra) + len(add) > budget:
                break
            extra += add
        if extra:
            body = f"{body} Clients: {extra}" if body else f"Clients: {extra}"
    return body[:budget]


_DLL_DIR_HANDLES: list = []


def add_nvidia_dll_directories() -> list[Path]:
    """Windows needs the cuBLAS/cuDNN DLL folders on the search path before ctranslate2 loads.

    The nvidia wheels have no importable package, so this looks the folders up on sys.path.
    The handles from add_dll_directory must stay referenced or Python drops the folders.
    """
    if os.name != "nt":
        return []
    import sys

    added = []
    seen = {str(p).lower() for p in added}
    for entry in sys.path:
        root = Path(entry)
        for rel in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
            bin_dir = (root / rel).resolve()
            key = str(bin_dir).lower()
            if key in seen or not bin_dir.is_dir():
                continue
            seen.add(key)
            _DLL_DIR_HANDLES.append(os.add_dll_directory(str(bin_dir)))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            added.append(bin_dir)
    return added


def unload_lm_studio() -> None:
    """Free the GPU for Whisper by unloading only the chat model this tool manages.

    Embedding models stay loaded. With no model manager (and `LOCAL_MANAGE_MODELS` off)
    nothing is unloaded.
    """
    from .gpu import current_manager

    manager = current_manager()
    if manager is None:
        log.info("No model manager is active, so the chat model was left loaded.")
        return
    manager.unload_chat()


def _device_attempts(device: str) -> list[tuple[str, str]]:
    device = (device or "auto").strip().lower()
    if device == "cpu":
        return [("cpu", "int8")]
    if device == "cuda":
        return [("cuda", "float16"), ("cuda", "int8_float16")]
    if device != "auto":
        raise TranscriberUnavailable(f"TRANSCRIBE_DEVICE must be auto, cuda, or cpu (got {device!r}).")
    return [("cuda", "float16"), ("cuda", "int8_float16"), ("cpu", "int8")]


def _compat_pyav() -> None:
    """faster-whisper passes metadata_errors to av.open; current PyAV no longer accepts it."""
    import av

    if getattr(av.open, "_vault_ingest_wrapped", False):
        return
    real = av.open

    def open_compat(*args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return real(*args, **kwargs)

    open_compat._vault_ingest_wrapped = True
    av.open = open_compat


def load_whisper(model_name: str, device: str):
    """Return (model, device, compute_type). CUDA float16, then int8_float16, then CPU int8."""
    try:
        add_nvidia_dll_directories()
        _compat_pyav()
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise TranscriberUnavailable(
            "faster-whisper is not installed. Run: pip install -e \".[transcribe]\"") from e
    except OSError as e:
        raise TranscriberUnavailable(f"CUDA libraries failed to load: {e}") from e

    errors = []
    attempts = _device_attempts(device)
    for dev, compute in attempts:
        log.info("Loading Whisper %s on %s (%s).", model_name, dev, compute)
        for handler in log.handlers:
            handler.flush()
        try:
            model = WhisperModel(model_name, device=dev, compute_type=compute)
        except Exception as e:  # noqa: BLE001  ctranslate2 raises several types for a bad device
            errors.append(f"{dev}/{compute}: {e}")
            log.warning("Whisper could not load on %s/%s: %s", dev, compute, e)
            continue
        if dev == "cpu" and any(a[0] == "cuda" for a in attempts):
            log.warning("Whisper is on the CPU (int8). GPU load failed: %s", "; ".join(errors)[:500])
        else:
            log.info("Whisper %s running on %s (%s).", model_name, dev, compute)
        return model, dev, compute
    detail = "; ".join(errors)[:800]
    if (device or "auto").lower() == "cuda":
        raise TranscriberUnavailable(f"CUDA was requested but Whisper could not use the GPU. {detail}")
    raise TranscriberUnavailable(f"Whisper could not load. {detail}")


def load_whisper_exact(model_name: str, device: str, compute_type: str):
    """Load one device and compute type, with no fallback. Used by `doctor gpu`."""
    try:
        add_nvidia_dll_directories()
        _compat_pyav()
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise TranscriberUnavailable(
            "faster-whisper is not installed. Run: pip install -e \".[transcribe]\"") from e
    except OSError as e:
        raise TranscriberUnavailable(f"CUDA libraries failed to load: {e}") from e
    log.info("Loading Whisper %s on %s (%s).", model_name, device, compute_type)
    for handler in log.handlers:
        handler.flush()
    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    log.info("Whisper %s running on %s (%s).", model_name, device, compute_type)
    return model, device, compute_type


def stderr_tail(text: str, lines: int = STDERR_TAIL_LINES) -> str:
    kept = (text or "").splitlines()
    return "\n".join(kept[-lines:])


def _segment_json(seg: Segment) -> dict:
    return {"start": seg.start, "end": seg.end, "text": seg.text, "avg_logprob": seg.avg_logprob,
            "no_speech_prob": seg.no_speech_prob, "speaker": seg.speaker}


def _parent_should_stop() -> bool:
    from .gpu import current_manager, paused_now

    if _whisper_should_stop():
        return True
    manager = current_manager()
    return bool(manager is not None and paused_now(manager.s))


def spawn_whisper_child(job: dict, should_stop: Callable[[], bool]) -> ChildRun:
    """Run `whisper_worker` in a new process. A native crash stays in the child."""
    import tempfile

    result_fd, result_name = tempfile.mkstemp(prefix="vault-whisper-", suffix=".json")
    os.close(result_fd)
    job_fd, job_name = tempfile.mkstemp(prefix="vault-whisper-job-", suffix=".json")
    os.close(job_fd)
    err_fd, err_name = tempfile.mkstemp(prefix="vault-whisper-err-", suffix=".txt")
    os.close(err_fd)
    payload_job = dict(job)
    payload_job["result"] = result_name
    Path(job_name).write_text(json.dumps(payload_job), encoding="utf-8")
    err_fh = open(err_name, "w", encoding="utf-8", errors="replace")
    cancelled = False
    proc = None
    try:
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "vault_ingest.whisper_worker", job_name],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err_fh,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            err_fh.write(str(e))
            proc = None
        while proc is not None and proc.poll() is None:
            if should_stop():
                cancelled = True
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                break
            time.sleep(0.25)
    finally:
        err_fh.close()
    stderr = ""
    try:
        stderr = Path(err_name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        stderr = ""
    payload = None
    try:
        raw = Path(result_name).read_text(encoding="utf-8")
        if raw.strip():
            payload = json.loads(raw)
    except (OSError, ValueError):
        payload = None
    for name in (result_name, job_name, err_name):
        try:
            os.remove(name)
        except OSError:
            pass
    code = 1 if proc is None or proc.returncode is None else int(proc.returncode)
    return ChildRun(code, stderr, payload if isinstance(payload, dict) else None, cancelled)


def _log_child_failure(child: ChildRun, device: str, compute: str) -> None:
    tail = stderr_tail(child.stderr)
    log.error("Whisper child exited %s on %s/%s. %s", child.returncode, device, compute, tail or "(no stderr)")


def _child_reason(child: ChildRun, device: str, compute: str) -> str:
    parts = [f"exit {child.returncode} on {device}/{compute}"]
    if child.payload and child.payload.get("error"):
        parts.append(str(child.payload["error"]))
    tail = stderr_tail(child.stderr)
    if tail:
        parts.append(tail)
    return "; ".join(parts)[:800]


def _set_transcribing(active: bool) -> None:
    from .gpu import current_manager, mark_transcribing

    manager = current_manager()
    if manager is None:
        return
    mark_transcribing(manager.s, active)


def _transcript_from(payload: dict, model_name: str, language: str, started: float) -> Transcript:
    segments = []
    for row in payload.get("segments") or []:
        if isinstance(row, dict):
            segments.append(Segment(float(row.get("start") or 0), float(row.get("end") or 0),
                                    str(row.get("text") or ""), float(row.get("avg_logprob") or 0),
                                    float(row.get("no_speech_prob") or 0), row.get("speaker")))
    text = format_segments(segments)
    return Transcript(text, str(payload.get("language") or whisper_options(language)["language"]), model_name,
                      str(payload.get("device") or ""), str(payload.get("compute_type") or ""),
                      time.perf_counter() - started)


def _child_ok(child: ChildRun) -> bool:
    return child.returncode == 0 and bool(child.payload and child.payload.get("ok"))


def _one_child(job: dict, spawn: Callable, device: str, compute: str) -> ChildRun:
    child = spawn(job, _parent_should_stop)
    if not child.cancelled and not _child_ok(child):
        _log_child_failure(child, device, compute)
    return child


def _transcribe_isolated(path: Path, *, model_name: str, device: str, language: str, initial_prompt: str,
                         me_name: str, clip_end: float | None, unload: bool,
                         spawn: Callable) -> Transcript:
    """Load and transcribe in a child. A crash retries once on CPU int8."""
    from .gpu import GpuInterrupted

    started = time.perf_counter()
    if unload:
        unload_lm_studio()
    _mark_whisper(True)
    _set_transcribing(True)
    try:
        first = _device_attempts(device)[0]
        job = {"mode": "transcribe", "model": model_name, "device": device, "path": str(path),
               "language": language, "initial_prompt": initial_prompt, "me_name": me_name, "clip_end": clip_end}
        child = _one_child(job, spawn, first[0], first[1])
        if child.cancelled or (child.payload and child.payload.get("interrupted")):
            raise GpuInterrupted("GPU hold")
        if _child_ok(child):
            return _transcript_from(child.payload or {}, model_name, language, started)
        if (device or "auto").strip().lower() == "cpu":
            raise TranscriptionError(_child_reason(child, "cpu", "int8"))
        cpu_job = dict(job, device="cpu")
        cpu = _one_child(cpu_job, spawn, "cpu", "int8")
        if cpu.cancelled or (cpu.payload and cpu.payload.get("interrupted")):
            raise GpuInterrupted("GPU hold")
        if _child_ok(cpu):
            log.warning("Whisper is on the CPU (int8) after the GPU child exited %s.", child.returncode)
            return _transcript_from(cpu.payload or {}, model_name, language, started)
        raise TranscriptionError(_child_reason(cpu, "cpu", "int8"))
    finally:
        _mark_whisper(False)
        _set_transcribing(False)


def doctor_loads(model_name: str, spawn: Callable | None = None) -> list[dict]:
    """Load Whisper on cuda/float16, cuda/int8_float16, then cpu/int8. Each attempt is its own process."""
    run = spawn or spawn_whisper_child
    rows = []
    for device, compute in DOCTOR_LOADS:
        job = {"mode": "load", "model": model_name, "device": device, "compute": compute}
        child = _one_child(job, run, device, compute)
        rows.append({"device": device, "compute": compute, "ok": _child_ok(child),
                     "returncode": child.returncode, "stderr": stderr_tail(child.stderr)})
    return rows


def _as_segment(raw, speaker: str | None = None) -> Segment:
    return Segment(float(raw.start), float(raw.end), raw.text or "",
                   float(getattr(raw, "avg_logprob", 0.0) or 0.0),
                   float(getattr(raw, "no_speech_prob", 0.0) or 0.0), speaker)


def _whisper_should_stop() -> bool:
    from .gpu import current_manager

    manager = current_manager()
    return bool(manager is not None and manager.gate is not None and manager.gate.cancel.is_set())


def transcribe_with_model(model, path: Path, language: str, initial_prompt: str,
                          clip_end: float | None = None, speaker: str | None = None) -> list[Segment]:
    from .gpu import GpuInterrupted

    if _whisper_should_stop():
        raise GpuInterrupted("GPU hold")
    opts = whisper_options(language, initial_prompt, clip_end)
    raw_segments, _info = model.transcribe(str(path), **opts)
    segments = []
    for seg in raw_segments:
        if _whisper_should_stop():
            raise GpuInterrupted("GPU hold")
        segments.append(_as_segment(seg, speaker))
    return segments


def release_model(model) -> None:
    """Drop the Whisper model and the ctranslate2 object inside it so CUDA memory is freed."""
    from .gpu import current_manager

    if model is None:
        return
    inner = getattr(model, "model", None)
    if inner is not None:
        try:
            model.model = None
        except Exception:  # noqa: BLE001
            pass
        del inner
    del model
    gc.collect()
    log.info("Whisper model deleted; CUDA memory released.")
    manager = current_manager()
    if manager is not None:
        manager.whisper_active = False
        manager.log_vram("after whisper release")
        manager.log_gpu_processes("after whisper release")


class Engine:
    """One loaded model, reused for a compare run. Call `close` before any LLM request."""

    def __init__(self, model_name: str, device: str):
        self.model_name = model_name
        self.model, self.device, self.compute_type = load_whisper(model_name, device)

    def transcribe(self, path: Path, language: str, initial_prompt: str,
                   clip_end: float | None = None, speaker: str | None = None) -> list[Segment]:
        return transcribe_with_model(self.model, path, language, initial_prompt, clip_end, speaker)

    def close(self) -> None:
        model = getattr(self, "model", None)
        self.model = None
        if model is not None:
            release_model(model)


def read_glossary(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def audio_tracks(path: Path) -> list[tuple[Path, str]]:
    """`(file, role)` with role `mic`, `other`, or `mixed`. Two roles mean separate tracks."""
    folder = path if path.is_dir() else path.parent
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS] if folder.is_dir() else []
    if path.is_file() and path not in files:
        files = [path]

    def role(name: str) -> str | None:
        stem = name.lower()
        if "mic" in stem or "microphone" in stem:
            return "mic"
        if any(k in stem for k in ("system", "loopback", "speaker", "desktop")):
            return "other"
        return None

    mic = [p for p in files if role(p.stem) == "mic"]
    other = [p for p in files if role(p.stem) == "other"]
    if mic and other:
        return [(mic[0], "mic"), (other[0], "other")]
    target = path if path.is_file() else next((p for p in files if p.name.lower() == "audio.mp4"), None)
    if target is None:
        raise TranscriptionError(f"No audio file in {folder}")
    streams = _audio_stream_count(target)
    if streams >= 2:
        return [(target, "mic"), (target, "other")]
    return [(target, "mixed")]


def _audio_stream_count(path: Path) -> int:
    ffmpeg = shutil.which("ffprobe") or shutil.which("ffmpeg")
    if not ffmpeg:
        bundled = Path(os.environ.get("LOCALAPPDATA", "")) / "meetily" / "ffmpeg.exe"
        ffmpeg = str(bundled) if bundled.is_file() else ""
    if not ffmpeg:
        return 1
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, timeout=30,
                              stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return len(re.findall(r"Stream #\d+:\d+.*Audio:", (proc.stderr or "") + (proc.stdout or "")))


def _extract_stream(path: Path, index: int, dest: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        bundled = Path(os.environ.get("LOCALAPPDATA", "")) / "meetily" / "ffmpeg.exe"
        ffmpeg = str(bundled) if bundled.is_file() else ""
    if not ffmpeg:
        raise TranscriptionError(f"{path.name} has more than one audio stream but ffmpeg is not available to split it")
    proc = subprocess.run([ffmpeg, "-y", "-i", str(path), "-map", f"0:a:{index}", "-vn", str(dest)],
                          capture_output=True, timeout=600, stdin=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not dest.is_file():
        raise TranscriptionError(f"Could not extract audio stream {index} from {path.name}: {(proc.stderr or '')[-300:]}")
    return dest


def _collect_segments(engine: Engine, path: Path, language: str, prompt: str, me_name: str,
                      clip_end: float | None) -> list[Segment]:
    import tempfile

    tracks = audio_tracks(path)
    if len(tracks) == 1 and tracks[0][1] == "mixed":
        return engine.transcribe(tracks[0][0], language, prompt, clip_end)
    labeled = []
    tmp = Path(tempfile.mkdtemp(prefix="vault-audio-"))
    try:
        for i, (file, role) in enumerate(tracks):
            speaker = me_name if role == "mic" else "Other"
            src = file
            if tracks[0][0] == tracks[-1][0] and role in ("mic", "other"):
                src = _extract_stream(file, 0 if role == "mic" else 1, tmp / f"{role}.wav")
            labeled.append((speaker, engine.transcribe(src, language, prompt, clip_end, speaker)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return interleave(labeled)


def _mark_whisper(active: bool) -> None:
    from .gpu import current_manager

    manager = current_manager()
    if manager is not None:
        manager.whisper_active = active


def _transcribe_in_process(path: Path, *, model_name: str, device: str, language: str, initial_prompt: str,
                           me_name: str = "Faris", clip_end: float | None = None,
                           engine: Engine | None = None, unload: bool = True) -> Transcript:
    """Transcribe in this process. Compare runs and tests pass an engine; the companion uses a child."""
    from .gpu import GpuInterrupted

    path = Path(path)
    if not path.is_file():
        raise TranscriptionError(f"Audio file not found: {path}")
    started = time.perf_counter()
    own = engine
    with _ENGINE_LOCK:
        if unload and own is None:
            unload_lm_studio()
        if own is None:
            _mark_whisper(True)
            try:
                own = Engine(model_name, device)
            except BaseException:
                _mark_whisper(False)
                raise
        try:
            segments = _collect_segments(own, path, language, initial_prompt, me_name, clip_end)
            text = format_segments(segments)
        except GpuInterrupted:
            raise
        finally:
            if engine is None and own is not None:
                own.close()
    return Transcript(text, whisper_options(language)["language"], model_name, own.device, own.compute_type,
                      time.perf_counter() - started)


def transcribe_path(path: Path, *, model_name: str, device: str, language: str, initial_prompt: str,
                    me_name: str = "Faris", clip_end: float | None = None,
                    engine: Engine | None = None, unload: bool = True,
                    spawn: Callable | None = None) -> Transcript:
    """Transcribe one file in a child process. An engine that is already loaded stays in this process."""
    path = Path(path)
    if not path.is_file():
        raise TranscriptionError(f"Audio file not found: {path}")
    if engine is not None:
        return _transcribe_in_process(path, model_name=model_name, device=device, language=language,
                                      initial_prompt=initial_prompt, me_name=me_name, clip_end=clip_end,
                                      engine=engine, unload=unload)
    with _ENGINE_LOCK:
        return _transcribe_isolated(path, model_name=model_name, device=device, language=language,
                                    initial_prompt=initial_prompt, me_name=me_name, clip_end=clip_end,
                                    unload=unload, spawn=spawn or spawn_whisper_child)


def compare_languages(path: Path, languages: list[str], *, model_name: str, device: str, initial_prompt: str,
                      me_name: str = "Faris", clip_end: float = 300) -> tuple[dict[str, str], str, str]:
    """First `clip_end` seconds in each language, one model load. Returns (texts, device, compute_type)."""
    texts: dict[str, str] = {}
    with _ENGINE_LOCK:
        unload_lm_studio()
        engine = Engine(model_name, device)
        try:
            for lang in languages:
                segments = _collect_segments(engine, Path(path), lang, initial_prompt, me_name, clip_end)
                texts[whisper_options(lang)["language"]] = format_segments(segments)
        finally:
            engine.close()
    return texts, engine.device, engine.compute_type


def comparison_markdown(texts: dict[str, str], title: str) -> str:
    columns = list(texts)
    width = max((len(v.splitlines()) for v in texts.values()), default=0)
    rows = []
    for i in range(width):
        cells = []
        for lang in columns:
            lines = texts[lang].splitlines()
            cells.append((lines[i] if i < len(lines) else "").replace("|", "\\|"))
        rows.append("| " + " | ".join(cells) + " |")
    header = "| " + " | ".join(columns) + " |"
    rule = "| " + " | ".join("---" for _ in columns) + " |"
    return f"# {title}\n\nFirst 5 minutes, one column per language.\n\n{header}\n{rule}\n" + "\n".join(rows) + "\n"


# ------------------------------------------------------------------ Meetily recording queue
@dataclass
class PendingAudio:
    folder: Path
    size: int
    since: float


class RecordingQueue:
    """Watch Meetily's recordings folder. Copy nothing: transcribe `audio.mp4` in place and write a transcript to the inbox."""

    def __init__(self, folder: Path, inbox: Path, state_path, vault, settings, settle_seconds: float = 30,
                 clock: Callable[[], float] = time.time, clients: Callable[[], list[str]] | None = None,
                 should_skip: Callable[[dt.datetime], bool] | None = None):
        self.folder = Path(folder)
        self.inbox = Path(inbox)
        self.state_path = Path(state_path)
        self.vault = vault
        self.settings = settings
        self.settle = settle_seconds
        self.clock = clock
        self.clients = clients or (lambda: [])
        self.should_skip = should_skip
        self.gpu = None
        self.pending: dict[str, PendingAudio] = {}
        self.inflight: set[str] = set()
        self._jobs: list[Path] = []
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop = False
        self._missing = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._worker, name="whisper-queue", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def _state(self) -> dict:
        if self.state_path.is_file():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                data.setdefault("done", [])
                data.setdefault("sources", {})
                data.setdefault("failed", {})
                return data
            except ValueError:
                log.warning("%s is unreadable; Meetily recordings will not be queued until it is fixed", self.state_path)
                return {"done": [], "sources": {}, "broken": True}
        return {"done": [], "sources": {}, "failed": {}}

    def _save(self, data: dict) -> None:
        self.vault.write(self.state_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def poll(self) -> None:
        if not self.folder.is_dir():
            if not self._missing:
                log.warning("Meetily recordings folder not found: %s", self.folder)
                self._missing = True
            return
        self._missing = False
        state = self._state()
        if state.get("broken"):
            return
        done = set(state["done"])
        failed = set(state.get("failed") or {})
        now = self.clock()
        for folder in self.folder.iterdir():
            if not folder.is_dir():
                continue
            key = str(folder)
            if key in done or key in self.inflight or key in failed:
                continue
            audio = folder / "audio.mp4"
            if not audio.is_file():
                continue
            size = audio.stat().st_size
            seen = self.pending.get(key)
            if seen is None or seen.size != size:
                self.pending[key] = PendingAudio(folder, size, now)
                continue
            if size > 0 and now - seen.since >= self.settle:
                self.inflight.add(key)
                self.pending.pop(key, None)
                self._jobs.append(folder)
                self._wake.set()

    def _worker(self) -> None:
        from .gpu import GpuInterrupted

        while not self._stop:
            self._wake.wait(timeout=2)
            self._wake.clear()
            while self._jobs and not self._stop:
                if self.gpu is not None and (reason := self.gpu.start_blocked()):
                    log.info("Not starting transcription (%s).", reason)
                    break
                folder = self._jobs.pop(0)
                try:
                    self._transcribe(folder)
                except GpuInterrupted:
                    self._jobs.insert(0, folder)
                    log.info("Transcription paused; %s stays at the front of the queue.", folder.name)
                    break
                except Exception:  # noqa: BLE001
                    log.exception("Could not transcribe %s", folder)
                    self.inflight.discard(str(folder))

    def _transcribe(self, folder: Path) -> None:
        from .meetily import export_name
        from .sessions import parse_iso, to_local

        meta = {}
        meta_path = folder / "metadata.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
        created = None
        if meta.get("created_at"):
            try:
                created = parse_iso(meta["created_at"])
            except ValueError:
                created = None
        if created is None:
            try:
                created = to_local(folder.stat().st_mtime)
            except OSError:
                created = None
        if created is not None and self.should_skip is not None and self.should_skip(created):
            log.info("Not transcribing %s (this meeting is marked don't ingest).", folder.name)
            state = self._state()
            state["done"].append(str(folder))
            self._save(state)
            self.inflight.discard(str(folder))
            return
        title = str(meta.get("meeting_name") or folder.name)
        name = export_name(title, created)
        dest = self.inbox / name
        n = 2
        while dest.exists():
            dest = self.inbox / f"{Path(name).stem} ({n}).txt"
            n += 1
        audio = folder / "audio.mp4"
        log.info("Transcribing %s", audio)
        glossary = read_glossary(self.settings.vault_path / self.settings.system_dir / "Glossary.md")
        prompt = build_initial_prompt(glossary, self.clients())
        try:
            result = transcribe_path(
                audio, model_name=self.settings.transcribe_model, device=self.settings.transcribe_device,
                language=self.settings.transcribe_language, initial_prompt=prompt, me_name=self.settings.me_name)
        except TranscriptionError as e:
            self._fail_recording(folder, str(e))
            return
        self.vault.write(dest, result.text)
        state = self._state()
        state["done"].append(str(folder))
        state["sources"][dest.name] = {"audio": str(audio), "transcribed_by": result.transcribed_by}
        self._save(state)
        self.inflight.discard(str(folder))
        log.info("Wrote %s (%s, %.0fs)", dest.name, result.transcribed_by, result.seconds)

    def _fail_recording(self, folder: Path, reason: str) -> None:
        """Keep the folder out of the automatic queue. Removing the failed entry is the manual retry."""
        state = self._state()
        if state.get("broken"):
            self.inflight.discard(str(folder))
            return
        failed = state.setdefault("failed", {})
        failed[str(folder)] = {"reason": reason, "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        self._save(state)
        self.inflight.discard(str(folder))
        log.error("Transcription failed for %s (%s). It stays in the queue for a manual retry; "
                  "remove its entry from failed in %s.", folder.name, reason, self.state_path)


def media_seconds(path: Path) -> float | None:
    """Duration of an audio or video file, in seconds."""
    try:
        import av
    except ImportError:
        return None
    try:
        with av.open(str(path)) as container:
            if container.duration:
                return float(container.duration) / 1_000_000
            for stream in container.streams:
                if stream.duration and stream.time_base:
                    return float(stream.duration * stream.time_base)
    except Exception:  # noqa: BLE001  unreadable media: the caller tries another file
        return None
    return None


def lookup_source(state_path: Path, filename: str) -> dict | None:
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    found = (data.get("sources") or {}).get(filename)
    return found if isinstance(found, dict) else None
