"""Command line: vault-ingest run | watch | reprocess <path> | check | mode [cloud|local] | companion | clients"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

from .config import DEFAULT_ENV_FILE, LLM_MODES, load_settings, set_llm_mode
from .gpu import GpuInterrupted
from .llm import LLMUnavailable
from .pipeline import Pipeline, Result
from .parsers import SUPPORTED_EXTENSIONS

log = logging.getLogger("vault_ingest")
UNAVAILABLE_RETRY_SECONDS = 60
SERVER_RETRY_SECONDS = 300
HEARTBEAT_SECONDS = 120
_INGEST_LOCK = None


def setup_logging(log_file: Path | None, verbose: bool) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    if sys.stdout is not None:           # pythonw (background on Windows) has no console
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        log.addHandler(console)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        # 5 MB per file, 3 old files kept (the logs had grown past 70 MB each)
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3,
                                                  encoding="utf-8", delay=True)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    for noisy in ("httpx", "anthropic", "watchdog", "comtypes", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class IngestLock:
    """One non-blocking lock for `run` and `watch`, held until this process exits."""

    def __init__(self, system: Path):
        self.path = system / "ingest.lock"
        self.pid_path = system / "ingest.pid"
        self._fh = None

    def acquire(self) -> str | None:
        """None when this process holds the lock. Otherwise a message naming the other pid."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if fh.seek(0, os.SEEK_END) == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            pid = _read_pid(self.pid_path)
            who = str(pid) if pid else "unknown"
            return f"vault-ingest is already running (pid {who})."
        self._fh = fh
        self.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        return None

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True


def hold_ingest_lock(system: Path) -> str | None:
    """Take the process-lifetime lock. Returns the message to print when another process holds it."""
    global _INGEST_LOCK
    lock = IngestLock(system)
    message = lock.acquire()
    if message:
        return message
    _INGEST_LOCK = lock
    return None


def release_ingest_lock() -> None:
    global _INGEST_LOCK
    lock = _INGEST_LOCK
    _INGEST_LOCK = None
    if lock is not None:
        lock.release()


def start_heartbeat(p: Pipeline) -> None:
    """Every two minutes, log the file and step while a job is running."""
    working = {"transcribing", "summary", "evidence", "profile"}

    def loop() -> None:
        while True:
            time.sleep(HEARTBEAT_SECONDS)
            name, step = p.current_file, p.step
            if name and step in working:
                log.info("Still working: %s (%s)", name, step)

    threading.Thread(target=loop, name="ingest-heartbeat", daemon=True).start()


def _names(folder: Path) -> list[str]:
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_file() and not p.name.startswith((".", "~$")))


def format_ingest_status(s, *, pid_alive=None, mcp_ok=None, lm_ok=None) -> list[str]:
    """Lines for `vault-ingest status`. Probes default to the live machine."""
    alive = pid_alive or _pid_alive
    watcher = _read_pid(s.system / "ingest.pid")
    companion = _read_pid(s.system / "companion.lock")
    if mcp_ok is None:
        mcp_ok = _probe_mcp()
    if lm_ok is None:
        lm_ok = _probe_lm_studio(s)
    current = _read_job(s.system / "ingest-status.json")
    inbox = ", ".join(_names(s.inbox)) or "(empty)"
    review = ", ".join(_names(s.review)) or "(empty)"
    return [
        f"watcher: {'running (pid ' + str(watcher) + ')' if watcher and alive(watcher) else 'stopped'}",
        f"companion: {'running (pid ' + str(companion) + ')' if companion and alive(companion) else 'stopped'}",
        f"MCP: {'up' if mcp_ok else 'down'}",
        f"LM Studio: {'up' if lm_ok else 'down'}",
        f"inbox: {inbox}",
        f"_needs-review: {review}",
        f"current: {current['file'] or '(none)'} ({current['step'] or 'idle'})",
        f"last error: {current['last_error'] or '(none)'}",
    ]


def _read_job(path: Path) -> dict:
    if not path.is_file():
        return {"file": "", "step": "", "last_error": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"file": "", "step": "", "last_error": ""}
    if not isinstance(data, dict):
        return {"file": "", "step": "", "last_error": ""}
    return {
        "file": str(data.get("file") or ""),
        "step": str(data.get("step") or ""),
        "last_error": str(data.get("last_error") or ""),
    }


def _probe_mcp() -> bool:
    try:
        import httpx
        response = httpx.get("http://127.0.0.1:37842/health", timeout=2)
        return response.status_code < 500
    except Exception:  # noqa: BLE001
        return False


def _probe_lm_studio(s) -> bool:
    import socket
    from urllib.parse import urlparse
    base = getattr(s, "openai_compat_base_url", None) or "http://localhost:1234/v1"
    parsed = urlparse(base)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 1234
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def status_cmd(args) -> int:
    overrides = {"vault_path": args.vault} if getattr(args, "vault", None) else {}
    s = load_settings(args.env, **overrides)
    for line in format_ingest_status(s):
        print(line)
    return 0


def print_results(results: list[Result]) -> None:
    if not results:
        print("Inbox is empty — nothing to do.")
        return
    for r in results:
        if r.status == "planned":
            print(f"\n[DRY RUN] {r.source.name}")
            for line in r.planned:
                print(f"   {line}")
        elif r.status == "processed":
            print(f"✔ {r.source.name} -> {r.client}: {r.call_note.name} ({len(r.chunk_notes)} parts)")
        elif r.status == "skipped":
            print(f"• {r.source.name}: skipped — {r.message}")
        elif r.status == "waiting":
            print(f"… {r.source.name}: left in the inbox — {r.message}")
        else:
            print(f"⚠ {r.source.name}: needs review — {r.message}")


def make_pipeline(args) -> Pipeline:
    overrides = {"vault_path": args.vault} if getattr(args, "vault", None) else {}
    s = load_settings(args.env, **overrides)
    setup_logging(None if args.dry_run else s.system / "ingest.log", args.verbose)
    llm = None
    if not args.dry_run:
        from .llm import make_llm
        llm = make_llm(s)
    concurrency = args.concurrency or s.llm_concurrency
    return Pipeline(s, llm, dry_run=args.dry_run, concurrency=concurrency)


# ------------------------------------------------------------------ watch
def _bind_gpu(p: Pipeline, service) -> None:
    p.models = service.manager
    p.gpu = service
    llm = p.llm
    if llm is not None and hasattr(llm, "gate"):
        llm.gate = service.gate
        http = getattr(llm, "http", None)
        if http is not None:
            service.gate.bind(http)


def begin_gpu(p: Pipeline, *, tray: bool):
    """Start the shared GPU guard. Dry runs do not load or unload anything."""
    if p.dry_run:
        return None
    from .gpu import open_service
    from .watcher_meetings import start_hotkeys

    service = open_service(p.s, p.vault, is_companion=False)
    service.start(tray=tray)
    _bind_gpu(p, service)
    if tray:
        service.attach_hotkey(start_hotkeys)
    return service


def _requeue(front: deque, job: Path, rest: list[Path]) -> None:
    """`job` first, then whatever else in this batch had not started."""
    for path in reversed(rest):
        front.appendleft(path)
    front.appendleft(job)


def watch(p: Pipeline) -> None:
    from .gpu import finish_chat

    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    inbox = p.s.inbox
    inbox.mkdir(parents=True, exist_ok=True)
    p.set_step(None, None)
    pending: dict[Path, float] = {}
    front: deque[Path] = deque()
    lock = threading.Lock()
    service = begin_gpu(p, tray=True)

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            if event.is_directory:
                return
            for attr in ("src_path", "dest_path"):
                raw = getattr(event, attr, None)
                if not raw:
                    continue
                path = Path(raw)
                if path.parent.resolve() == inbox.resolve() and path.suffix.lower() in SUPPORTED_EXTENSIONS \
                        and not path.name.startswith((".", "~$")):
                    with lock:
                        pending[path] = time.time()

    # anything already sitting in the inbox
    for f in p.inbox_files():
        pending[f] = 0.0

    observer = Observer()
    observer.schedule(Handler(), str(inbox), recursive=False)
    observer.start()
    log.info("Watching %s (debounce %.0fs). Ctrl+C to stop.", inbox, p.s.watch_debounce_seconds)
    sizes: dict[Path, int] = {}
    unavailable = False
    try:
        while True:
            time.sleep(2)
            try:
                p.resume_pending()
            except Exception:  # noqa: BLE001
                log.exception("Could not retry a pending step")
            try:
                p.sync_meeting_selections()
            except Exception:  # noqa: BLE001
                log.exception("Could not apply a client change")
            now = time.time()
            with lock:
                queued = len(front) + len(pending)
            if service is not None:
                service.set_counts(inbox=queued)
            if service is not None and (reason := service.start_blocked()):
                log.debug("GPU work is waiting (%s).", reason)
                continue
            with lock:
                ready = [f for f, t in pending.items() if now - t >= p.s.watch_debounce_seconds]
            jobs: list[Path] = []
            while front:
                jobs.append(front.popleft())
            for f in ready:
                if not f.exists():
                    with lock:
                        pending.pop(f, None)
                    continue
                size = f.stat().st_size
                if sizes.get(f) != size:          # still being written -> wait another round
                    sizes[f] = size
                    with lock:
                        pending[f] = now
                    continue
                with lock:
                    pending.pop(f, None)
                sizes.pop(f, None)
                jobs.append(f)
            for n, f in enumerate(jobs):
                if service is not None and service.start_blocked():
                    _requeue(front, f, jobs[n + 1:])
                    break
                if service is not None:
                    service.set_counts(inbox=len(front) + len(pending) + (len(jobs) - n), phase="processing")
                more_after = bool(front) or bool(pending) or n + 1 < len(jobs)
                try:
                    print_results([p.process(f)])
                    if unavailable:
                        log.info("LLM server is reachable again.")
                        unavailable = False
                    held = service is not None and service.store.load().paused
                    if p.models is not None:
                        finish_chat(p.models, more_waiting=more_after and not held, interrupted=held)
                except GpuInterrupted as e:
                    log.info("Paused; %s stays at the front of the queue. %s", f.name, e)
                    _requeue(front, f, jobs[n + 1:])
                    if p.models is not None:
                        finish_chat(p.models, more_waiting=True, interrupted=True)
                    break
                except LLMUnavailable as e:
                    if not unavailable:
                        log.warning("%s", e)
                        unavailable = True
                        p._note_unavailable(e)
                    else:
                        log.debug("Still unavailable: %s", e)
                    # this file and the rest of this round stay in the inbox
                    from .gpu import SERVER_START_FAILED
                    delay = SERVER_RETRY_SECONDS if SERVER_START_FAILED in str(e) else UNAVAILABLE_RETRY_SECONDS
                    retry_at = now + delay - p.s.watch_debounce_seconds
                    with lock:
                        for g in [f, *jobs[n + 1:]]:
                            if g.exists():
                                pending[g] = max(pending.get(g, 0.0), retry_at)
                                sizes[g] = g.stat().st_size
                    if p.models is not None:
                        finish_chat(p.models, more_waiting=False, interrupted=True)
                    break
                except Exception as e:  # noqa: BLE001
                    if service is not None and service.store.load().paused:
                        log.info("Paused while %s was running; it goes back to the queue.", f.name)
                        _requeue(front, f, jobs[n + 1:])
                        if p.models is not None:
                            finish_chat(p.models, more_waiting=True, interrupted=True)
                        break
                    log.exception("Failed on %s", f.name)
                    if p.models is not None:
                        finish_chat(p.models, more_waiting=more_after, interrupted=True)
                    if f.exists():
                        p.send_to_review(f, f"Processing failed: {e}")
            if service is not None:
                with lock:
                    left = len(front) + len(pending)
                service.set_counts(inbox=left, phase="idle" if not left else "processing")
    except KeyboardInterrupt:
        pass
    finally:
        if service is not None:
            service.stop()
        observer.stop()
        observer.join()


# ------------------------------------------------------------------ check
def check(args) -> int:
    """Verify config, vault layout and that the LLM answers."""
    overrides = {"vault_path": args.vault} if getattr(args, "vault", None) else {}
    s = load_settings(args.env, **overrides)
    ok = True
    print(f"Vault:    {s.vault_path}")
    for name, path in (("Inbox", s.inbox), ("Clients", s.clients), ("_System", s.system),
                       ("Template", s.vault_path / s.template_path)):
        exists = path.exists()
        ok &= exists or name == "Template"
        print(f"  {'✔' if exists else '✘'} {name}: {path}")
    print(f"Prompts:  {s.prompts_dir} ({'found' if s.prompts_dir.exists() else 'MISSING'})")
    print(f"LLM:      {s.effective_mode} ({s.llm_provider}) / {s.llm_model}")
    if args.no_llm:
        return 0 if ok else 1
    try:
        from .llm import make_llm
        from .schemas import ClientGuess

        llm = make_llm(s)
        g = llm.complete_json("You answer with JSON only.",
                              'Return client "Test", confidence 1, reason "ok".', ClientGuess)
        print(f"  ✔ LLM answered: {g.client} ({g.reason}) — model {getattr(llm, 'model', s.llm_model)}")
    except BaseException as e:  # noqa: BLE001
        ok = False
        print(f"  ✘ LLM call failed: {e}")
    return 0 if ok else 1


# ------------------------------------------------------------------ mode
def show_mode(args) -> int:
    overrides = {"vault_path": args.vault} if getattr(args, "vault", None) else {}
    s = load_settings(args.env, **overrides)
    note = "" if s.llm_mode else f"  (LLM_MODE not set; from LLM_PROVIDER={s.llm_provider})"
    print(f"Mode:     {s.effective_mode}{note}")
    model = s.llm_model
    if s.llm_mode == "local" and model.lower() == "auto":
        from .llm import LocalStudioLLM, NoModelLoaded

        try:
            model = f"auto -> {LocalStudioLLM(s, auto_start=False).resolve_model()}"
        except NoModelLoaded:
            model = "auto (no model loaded in LM Studio)"
        except LLMUnavailable:
            model = "auto (LM Studio not reachable)"
        except Exception as e:  # noqa: BLE001
            model = f"auto ({e})"
    print(f"Model:    {model}")
    return 0


def switch_mode(args) -> int:
    env_file = Path(args.env) if args.env else DEFAULT_ENV_FILE
    previous_env = os.environ.get("LLM_MODE")
    set_llm_mode(env_file, args.target)
    print(f"Switched to {args.target} mode (updated LLM_MODE in {env_file}).")
    if previous_env and previous_env.strip().lower() != args.target:
        print(f"Note: LLM_MODE={previous_env} is also set as an environment variable in this session; "
              "it overrides .env for commands run from here.")
    os.environ["LLM_MODE"] = args.target
    print("Testing the connection ...")
    args.no_llm = False
    return check(args)


# ------------------------------------------------------------------ reprocess --all-local
def reprocess_all_local(p: Pipeline) -> list[Result]:
    records = p.local_records()
    print(f"{len(records)} call(s) in the ingest log were generated in local mode.")
    if not records:
        return []
    model = p.s.llm_model
    if hasattr(p.llm, "resolve_model"):
        try:
            model = p.llm.resolve_model()
        except Exception:  # noqa: BLE001  shown as configured; the real error surfaces on the first call
            pass
    print(f"They will be redone with the current mode: {p.s.effective_mode} / {model}")
    if p.s.effective_mode == "local":
        print("Note: you are in local mode, so they would be redone locally again. "
              "Run `vault-ingest mode cloud` first to redo them with Claude.")
    if not p.dry_run:
        try:
            answer = input(f"Reprocess {len(records)} local call(s) now? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled. Nothing was changed.")
            return []
    results: list[Result] = []
    for rec in records:
        raw = p.s.vault_path / rec.get("raw", "")
        if not rec.get("raw") or not raw.is_file():
            log.warning("Skipping %s: raw transcript not found at %s", rec.get("call_note"), rec.get("raw"))
            continue
        try:
            results.append(p.reprocess(raw))
        except LLMUnavailable as e:
            log.warning("%s", e)
            print(f"Stopped: {e}")
            break
        except Exception as e:  # noqa: BLE001  keep going with the others
            log.exception("Failed to reprocess %s", raw.name)
            results.append(Result(raw, "review", message=f"reprocess failed: {e}"))
    return results


def _client_names(s) -> list[str]:
    if not s.clients.is_dir():
        return []
    return sorted(p.name for p in s.clients.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))


def clients_cmd(args) -> int:
    """List, add, or fuzzy-find clients. Shares the picker's search and create path."""
    from .clients import create_client, fold, load_clients, near_duplicate, rank_clients
    from .vault import sanitize_name

    overrides = {"vault_path": args.vault} if getattr(args, "vault", None) else {}
    s = load_settings(args.env, **overrides)
    setup_logging(s.system / "ingest.log", args.verbose)
    records = load_clients(s.clients)
    action = args.clients_cmd or "list"
    if action == "list":
        for info in records:
            print(info.name)
        return 0
    if action == "find":
        hits = rank_clients(records, args.query)
        if not hits:
            print("No matches.")
            return 0
        for hit in hits:
            extra = f"  ({hit.via}: {hit.matched})" if hit.via != "name" else ""
            print(f"{hit.score:5.0f}  {hit.name}{extra}")
        return 0

    dup = near_duplicate(args.name, records)
    name = args.name
    if dup:
        try:
            answer = input(f"Did you mean {dup}? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() in ("y", "yes"):
            name = dup
        elif fold(sanitize_name(args.name)) == fold(dup):
            print(f"There is already a client named {dup}. Nothing was created.")
            return 0
    try:
        folder = create_client(s, name, aliases=list(args.alias or []), contact=args.contact or None,
                               company=args.company or None, note=args.note or None)
    except ValueError as e:
        print(e)
        return 1
    print(folder)
    return 0


def transcribe_cmd(args) -> int:
    """Transcribe one file, or compare languages on the first 5 minutes. Does not touch the inbox."""
    from .transcribe import build_initial_prompt, read_glossary

    overrides = {"vault_path": args.vault} if getattr(args, "vault", None) else {}
    s = load_settings(args.env, **overrides)
    setup_logging(s.system / "transcribe.log", args.verbose)
    audio = Path(args.path)
    if not audio.is_file():
        print(f"File not found: {audio}")
        return 1
    glossary = read_glossary(s.system / "Glossary.md")
    prompt = build_initial_prompt(glossary, _client_names(s))
    from .gpu import ModelManager, activate

    manager = ModelManager(s)
    activate(manager)
    try:
        return _transcribe_loaded(args, s, audio, prompt, manager)
    finally:
        manager.close()
        activate(None)


def _transcribe_loaded(args, s, audio: Path, prompt: str, manager) -> int:
    from .transcribe import compare_languages, comparison_markdown, transcribe_path

    if args.compare:
        languages = [part.strip() for part in args.compare.split(",") if part.strip()]
        if len(languages) < 2:
            print("--compare needs at least two language codes, for example hr,bs")
            return 1
        log.info("Comparing %s on the first 5 minutes of %s", ", ".join(languages), audio.name)
        texts, device, compute = compare_languages(
            audio, languages, model_name=s.transcribe_model, device=s.transcribe_device,
            initial_prompt=prompt, me_name=s.me_name, clip_end=300)
        body = comparison_markdown(texts, f"{audio.name}: {' vs '.join(texts)}")
        out = Path(args.out) if args.out else s.system / f"{audio.stem} - compare.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
        print(f"Whisper {s.transcribe_model} on {device}/{compute}. First 5 minutes written to {out}")
        for lang, text in texts.items():
            preview = "\n".join(text.splitlines()[:30])
            print(f"\n----- {lang} (first 30 lines) -----\n{preview}")
        return 0

    language = (args.language or s.transcribe_language).strip().lower()
    result = transcribe_path(
        audio, model_name=s.transcribe_model, device=s.transcribe_device, language=language,
        initial_prompt=prompt, me_name=s.me_name)
    if args.out:
        Path(args.out).write_text(result.text, encoding="utf-8")
        print(f"Wrote {args.out}")
    print(f"{result.seconds:.0f}s — {result.transcribed_by} on {result.device}/{result.compute_type}")
    print("\n".join(result.text.splitlines()[:30]))
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="vault-ingest",
                                 description="Turn client call transcripts into searchable Obsidian notes.")
    ap.add_argument("--env", help="path to a .env file (default: the one next to this tool)")
    ap.add_argument("--vault", help="override VAULT_PATH")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, help_ in (("run", "process everything in the inbox once"),
                        ("watch", "keep running and process new inbox files")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--dry-run", action="store_true", help="show what would happen; no LLM calls, no writes")
        sp.add_argument("--concurrency", type=int, help="parallel LLM calls (default 4; use 1 for local models)")

    rp = sub.add_parser("reprocess", help="redo one transcript (pass its file in Transcripts/raw/)")
    rp.add_argument("path", nargs="?")
    rp.add_argument("--all-local", action="store_true",
                    help="redo every call that was generated in local mode (asks first; uses the current mode)")
    rp.add_argument("--from-audio", action="store_true",
                    help="transcribe the call's audio again so new timestamps apply, then redo the notes")
    rp.add_argument("--dry-run", action="store_true")
    rp.add_argument("--concurrency", type=int)

    cp = sub.add_parser("check", help="verify settings, vault folders and the LLM connection")
    cp.add_argument("--no-llm", action="store_true")

    mp = sub.add_parser("mode", help="show the current mode, or switch: mode cloud | mode local")
    mp.add_argument("target", nargs="?", choices=LLM_MODES)

    cmp = sub.add_parser("companion", help="detect meetings, collect screenshots, export Meetily transcripts")
    cmp.add_argument("--config", help="path to companion.toml (default: the one next to this tool)")
    cmp.add_argument("--no-hotkey", action="store_true", help="don't register the snip hotkey")

    cl = sub.add_parser("clients", help="list, add, or fuzzy-find client folders")
    cl_sub = cl.add_subparsers(dest="clients_cmd")
    cl_sub.add_parser("list", help="list client folder names")
    addp = cl_sub.add_parser("add", help="create a client folder from the template")
    addp.add_argument("name")
    addp.add_argument("--alias", action="append", default=[], help="extra alias; can be repeated")
    addp.add_argument("--contact", help="contact name, written to Snapshot and aliases")
    addp.add_argument("--company", help="company, written to Snapshot and aliases")
    addp.add_argument("--note", help="one line appended to Snapshot")
    findp = cl_sub.add_parser("find", help="fuzzy-find clients")
    findp.add_argument("query")

    tp = sub.add_parser("transcribe", help="transcribe an audio file locally; nothing is ingested")
    tp.add_argument("path")
    tp.add_argument("--language", help="language code (default TRANSCRIBE_LANGUAGE, usually hr)")
    tp.add_argument("--out", help="where to write the transcript (or the comparison markdown)")
    tp.add_argument("--compare", help="comma-separated language codes, first 5 minutes only, e.g. hr,bs")

    sub.add_parser("bench", help="time one chunk digest and report whether the model fits in dedicated VRAM")
    sub.add_parser("status", help="watcher, companion, MCP, LM Studio, inbox, and the current step")

    doctor = sub.add_parser("doctor", help="check a local dependency without ingesting anything")
    doctor_sub = doctor.add_subparsers(dest="doctor_cmd", required=True)
    doctor_sub.add_parser("gpu", help="load Whisper on cuda/float16, cuda/int8_float16, then cpu/int8")

    pending = sub.add_parser("pending", help="pending profile and evidence steps")
    pending_sub = pending.add_subparsers(dest="pending_cmd", required=True)
    retry = pending_sub.add_parser("retry", help="clear needs-review and the failure count for one pending job")
    retry.add_argument("key", help="pending-jobs.json key, or a unique prefix of it")

    gpu = sub.add_parser("gpu", help="free the GPU, resume processing, or show what is loaded")
    gpu_sub = gpu.add_subparsers(dest="gpu_cmd", required=True)
    free = gpu_sub.add_parser("free", help="unload the chat model and pause (until resume, or --for 1h)")
    free.add_argument("--for", dest="hold", choices=["1h"], help="pause for one hour instead of until resume")
    gpu_sub.add_parser("resume", help="clear a pause and let processing continue")
    gpu_sub.add_parser("status", help="loaded model, VRAM, queue length, and whether processing is paused")

    args = ap.parse_args(argv)
    if args.cmd == "transcribe":
        return transcribe_cmd(args)
    if args.cmd == "bench":
        return bench_cmd(args)
    if args.cmd == "gpu":
        return gpu_cmd(args)
    if args.cmd == "doctor":
        return doctor_cmd(args)
    if args.cmd == "pending":
        return pending_cmd(args)
    if args.cmd == "clients":
        return clients_cmd(args)
    if args.cmd == "companion":
        from .watcher_meetings import main_companion

        return main_companion(args)
    if args.cmd == "status":
        return status_cmd(args)
    if args.cmd == "check":
        return check(args)
    if args.cmd == "mode":
        return switch_mode(args) if args.target else show_mode(args)
    if args.cmd == "reprocess" and bool(args.path) == bool(args.all_local):
        ap.error("reprocess needs either a file path or --all-local")

    p = make_pipeline(args)
    results: list[Result] = []
    locked = args.cmd in ("run", "watch")
    if locked:
        held = hold_ingest_lock(p.s.system)
        if held:
            print(held)
            return 1
        if not args.dry_run:
            p.announce = True
            p.track_status = True
        start_heartbeat(p)
    try:
        if args.cmd == "watch":
            if args.dry_run:
                print_results(p.run_inbox())
            else:
                watch(p)
            return 0
        service = begin_gpu(p, tray=False)
        try:
            if args.cmd == "run":
                results = p.run_inbox()
                print_results(results)
            elif args.cmd == "reprocess":
                if args.all_local:
                    results = reprocess_all_local(p)
                    if results:
                        print_results(results)
                else:
                    try:
                        print_results([p.reprocess(Path(args.path), from_audio=args.from_audio)])
                    except LLMUnavailable as e:
                        print(f"Nothing changed: {e}")
                        return 1
                    except GpuInterrupted as e:
                        print(f"Paused; nothing was written. {e}")
                        return 0
        finally:
            if service is not None:
                service.stop()
    finally:
        if locked:
            release_ingest_lock()
    return 1 if any(r.status == "waiting" for r in results) else 0


def bench_cmd(args) -> int:
    """One chunk digest on a built-in sample. Prints tokens/sec and dedicated-VRAM headroom."""
    from .gpu import ModelManager, fits_in_dedicated
    from .llm import BENCH_SYSTEM, BENCH_USER, LLMError, LocalStudioLLM, NoModelLoaded
    from .schemas import ChunkDigest

    overrides = {"vault_path": args.vault} if getattr(args, "vault", None) else {}
    s = load_settings(args.env, **overrides)
    setup_logging(s.system / "ingest.log", args.verbose)
    if s.effective_mode != "local":
        print("bench runs in local mode. Switch with: vault-ingest mode local")
        return 1
    manager = ModelManager(s)
    model = None
    st = None
    used = total = None
    try:
        manager.startup_cleanup()
        model = manager.ensure_loaded()
        llm = LocalStudioLLM(s)
        llm.complete_json(BENCH_SYSTEM, BENCH_USER, ChunkDigest)
        st = llm.last_stats
        used, total = manager.vram_used_total()
    except (LLMUnavailable, NoModelLoaded, LLMError) as e:
        print(f"bench failed: {e}")
        return 1
    finally:
        try:
            manager.unload_chat()
        except Exception:  # noqa: BLE001
            log.warning("Could not unload the chat model after bench", exc_info=True)
    fit = fits_in_dedicated(used, total)
    ttft = "n/a" if st.ttft_seconds is None else f"{st.ttft_seconds:.2f} s"
    print(f"model: {model or llm.model}")
    print(f"ttft: {ttft}")
    print(f"output tokens: {st.output_tokens}")
    print(f"reasoning tokens: {st.reasoning_tokens}")
    print(f"tokens/sec: {st.tokens_per_sec:.1f}")
    if used is None or total is None:
        print("dedicated VRAM: nvidia-smi unavailable")
    else:
        print(f"dedicated VRAM: {used} / {total} MB ({'fits' if fit else 'spills'})")
    return 0


def doctor_cmd(args) -> int:
    """Load Whisper in a child for each device. Prints the exit code and stderr. Does not transcribe."""
    from .transcribe import doctor_loads

    overrides = {"vault_path": args.vault} if getattr(args, "vault", None) else {}
    s = load_settings(args.env, **overrides)
    setup_logging(s.system / "ingest.log", args.verbose)
    rows = doctor_loads(s.transcribe_model)
    for row in rows:
        state = "worked" if row["ok"] else "failed"
        print(f"{row['device']}/{row['compute']}: {state} (exit {row['returncode']})")
        print(row["stderr"] or "(no stderr)")
    return 0 if any(row["ok"] for row in rows) else 1


def pending_cmd(args) -> int:
    """Reset one pending job so the next watch or run tries it again."""
    from .pipeline import Pipeline

    overrides = {"vault_path": args.vault} if getattr(args, "vault", None) else {}
    s = load_settings(args.env, **overrides)
    setup_logging(s.system / "ingest.log", args.verbose)
    pipe = Pipeline(s, None)
    data = pipe._load_pending()
    matches = [key for key in data if key == args.key or key.startswith(args.key)]
    if len(matches) != 1:
        print(f"No unique pending job matches {args.key!r} ({len(matches)} matches).")
        return 1
    pipe.reset_pending(key=matches[0])
    print(f"Pending job {matches[0]} will be tried again.")
    return 0


def gpu_cmd(args) -> int:
    """Unload the managed chat model and pause, or show what the GPU is doing. Logged to ingest.log."""
    from .gpu import (STATE_NAME, GpuStateStore, ModelManager, append_digest, format_status, show_toast)
    from .vault import Vault

    overrides = {"vault_path": args.vault} if getattr(args, "vault", None) else {}
    s = load_settings(args.env, **overrides)
    setup_logging(s.system / "ingest.log", args.verbose)
    vault = Vault(s.vault_path, (s.inbox_dir, s.system_dir))
    manager = ModelManager(s)
    store = GpuStateStore(s.system / STATE_NAME)
    if args.gpu_cmd == "status":
        lines = format_status(manager, store.load())
        print("\n".join(lines))
        for line in lines:
            log.info("%s", line)
        return 0
    if args.gpu_cmd == "resume":
        waiting = store.load().waiting
        if store.force_resume():
            text = f"Resumed processing ({waiting} calls waiting)"
            log.info("%s", text)
            show_toast(text)
            append_digest(vault, s.system, text)
            print(text)
        else:
            print("Not paused.")
        return 0
    hours = 1 if getattr(args, "hold", None) == "1h" else None
    until = None if hours is None else time.time() + 3600
    reason = "until you resume" if hours is None else "for 1 hour"
    text = "Paused: GPU held until you resume" if hours is None else "Paused: GPU held for 1 hour"
    store.claim_pause(reason=reason, manual=True, until=until)
    try:
        manager.unload_chat()
    except Exception as e:  # noqa: BLE001
        log.warning("Could not unload the chat model: %s", e)
    log.info("%s", text)
    show_toast(text)
    try:
        append_digest(vault, s.system, text)
    except Exception:  # noqa: BLE001
        log.exception("Could not append the GPU event to the digest")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
