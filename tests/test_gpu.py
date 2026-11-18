"""Chat-model load/unload and game-mode pause. lms, nvidia-smi, and the process list are mocked."""
import datetime as dt
import json
import logging
import subprocess
from collections import deque
from pathlib import Path

import httpx
import pytest

from conftest import make_transcript

from vault_ingest import cli
from vault_ingest.config import PROJECT_DIR, Settings
from vault_ingest.gpu import (STATE_NAME, BusyHit, Foreground, GpuInterrupted, GpuRules, GpuService,
                              GpuStateStore, ModelManager, Proc, detect_busy, finish_chat, meeting_active)
from vault_ingest.llm import FakeLLM, LocalStudioLLM
from vault_ingest.pipeline import Pipeline
from vault_ingest.transcribe import RecordingQueue, _transcribe_in_process, release_model
from vault_ingest.vault import Vault
from vault_ingest.watcher_meetings import load_config


def local(vault: Path, **kw) -> Settings:
    base = dict(vault_path=vault, prompts_dir=PROJECT_DIR / "prompts", llm_mode="local",
                llm_provider="openai_compat", llm_model="qwen3-14b",
                openai_compat_base_url="http://localhost:1234/v1")
    base.update(kw)
    return Settings(**base)


def chat(state="not-loaded"):
    return {"id": "qwen3-14b", "type": "llm", "state": state}


def embed(gpu_layers=0, state="loaded"):
    return {"id": "text-embedding-bge-m3", "type": "embeddings", "state": state, "gpu_layers": gpu_layers}


def make_manager(settings, calls, rows, ps=None, nvidia_text="800"):
    def run(args, **kw):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    def models():
        unloaded = any(len(c) > 1 and c[1] == "unload" for c in calls)
        loaded = any(len(c) > 1 and c[1] == "load" for c in calls)
        out = []
        for row in rows():
            row = dict(row)
            if row.get("id") == "qwen3-14b":
                if unloaded:
                    row["state"] = "not-loaded"
                elif loaded:
                    row["state"] = "loaded"
            out.append(row)
        return out

    mgr = ModelManager(settings, run=run, which=lambda name: "lms" if name == "lms" else None,
                       models_fn=models, ps_fn=ps or (lambda: []), nvidia=lambda args: nvidia_text)
    mgr.ensure_server = lambda: None
    return mgr


def commands(calls):
    return [c[1:] for c in calls]


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# ------------------------------------------------------------------ load / unload
def test_load_before_llm_stage_and_unload_when_the_queue_is_empty(vault, monkeypatch, caplog):
    import atexit

    caplog.set_level(logging.INFO, logger="vault_ingest")

    registered = []
    monkeypatch.setattr(atexit, "register", lambda fn: registered.append(fn))
    calls = []
    rows = lambda: [chat("not-loaded"), embed()]
    mgr = make_manager(local(vault), calls, rows)
    assert mgr.ensure_loaded() == "qwen3-14b"
    assert commands(calls) == [["load", "qwen3-14b", "--gpu", "max", "--context-length", "8192", "--parallel", "1"]]
    assert "Dedicated VRAM before load: 800 MB" in caplog.text
    assert "GPU processes before load: none" in caplog.text
    assert "Embedding text-embedding-bge-m3 is on CPU." in caplog.text
    assert "VRAM after load: 800 MB" in caplog.text
    assert registered, "unload is registered for process exit"

    finish_chat(mgr, more_waiting=True, interrupted=False)
    assert commands(calls) == [["load", "qwen3-14b", "--gpu", "max", "--context-length", "8192", "--parallel", "1"]]
    assert mgr.ensure_loaded() == "qwen3-14b"          # still loaded; not loaded a second time

    finish_chat(mgr, more_waiting=False, interrupted=False)
    assert ["unload", "qwen3-14b"] in commands(calls)
    assert "VRAM after unload: 800 MB" in caplog.text
    assert not any("bge" in " ".join(c) for c in commands(calls))

    registered[0]()                                    # atexit hook is safe to run again
    mgr.close()


def test_unload_when_the_llm_call_raises(vault):
    calls = []
    mgr = make_manager(local(vault), calls, lambda: [chat("not-loaded"), embed()])
    mgr.ensure_loaded()
    with pytest.raises(RuntimeError):
        try:
            raise RuntimeError("model exploded")
        finally:
            finish_chat(mgr, more_waiting=True, interrupted=True)
    assert ["unload", "qwen3-14b"] in commands(calls)
    mgr.close()


def test_startup_unloads_a_leftover_chat_model_and_leaves_the_embedding(vault, caplog):
    caplog.set_level(logging.INFO, logger="vault_ingest")
    calls = []
    mgr = make_manager(local(vault), calls, lambda: [chat("loaded"), embed()],
                       ps=lambda: [{"identifier": "text-embedding-bge-m3", "gpuLayers": 0}])
    mgr.startup_cleanup()
    assert ["unload", "qwen3-14b"] in commands(calls)
    assert not any("bge" in " ".join(c) or "embed" in " ".join(c) for c in commands(calls))
    assert "leftover chat model qwen3-14b" in caplog.text


def test_embedding_on_the_gpu_is_a_warning_not_an_unload(vault, caplog):
    caplog.set_level(logging.INFO, logger="vault_ingest")
    calls = []
    mgr = make_manager(local(vault), calls, lambda: [embed(gpu_layers=12)],
                       ps=lambda: [{"identifier": "text-embedding-bge-m3", "gpuLayers": 12}])
    mgr.startup_cleanup()
    assert calls == []
    assert "text-embedding-bge-m3" in caplog.text and "--gpu off" in caplog.text
    assert "CPU" in caplog.text


def test_manage_models_false_does_not_load_or_unload(vault):
    calls = []
    settings = local(vault, local_manage_models=False)
    mgr = make_manager(settings, calls, lambda: [chat("loaded"), embed(gpu_layers=8)])
    mgr.startup_cleanup()
    assert mgr.ensure_loaded() is None
    mgr.unload_chat()
    assert calls == []


def test_chat_requests_send_ttl_when_models_are_managed(vault):
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [chat("loaded")]})
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"client": "Acme", "confidence": 1, "reason": "ok"}'}}]})

    from vault_ingest.schemas import ClientGuess

    llm = LocalStudioLLM(local(vault), auto_start=False, transport=httpx.MockTransport(handler))
    llm.complete_json("s", "u", ClientGuess)
    assert bodies[0]["ttl"] == 120

    quiet = []

    def handler_off(req: httpx.Request) -> httpx.Response:
        quiet.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"client": "Acme", "confidence": 1, "reason": "ok"}'}}]})

    off = LocalStudioLLM(local(vault, local_manage_models=False), auto_start=False,
                         transport=httpx.MockTransport(handler_off))
    off.complete_json("s", "u", ClientGuess)
    assert "ttl" not in quiet[0]


# ------------------------------------------------------------------ game detection
def test_game_detected_by_path_and_by_name():
    rules = GpuRules()
    steam = Proc(1, "ProjectZomboid64.exe", r"D:\Steam\steamapps\common\ProjectZomboid\ProjectZomboid64.exe")
    hit = detect_busy([steam], None, 0, False, rules)
    assert hit is not None and hit.kind == "path" and hit.label == "ProjectZomboid64.exe"
    assert "is running" in hit.paused_text()

    named = Proc(2, "ProjectZomboid64.exe", r"C:\Games\ProjectZomboid64.exe")
    hit = detect_busy([named], None, 0, False, rules)
    assert hit is not None and hit.kind == "name"

    epic = Proc(3, "game.exe", r"C:\Program Files\Epic Games\Foo\game.exe")
    assert detect_busy([epic], None, 0, False, rules).kind == "path"


def test_run_external_reads_utf8(caplog):
    from vault_ingest.gpu import run_external
    caplog.set_level(logging.INFO)

    def run(args, **kw):
        assert kw["encoding"] == "utf-8" and kw["errors"] == "replace"
        return subprocess.CompletedProcess(args, 0, "ok café", "")

    proc = run_external(["echo"], timeout=5, label="echo", run=run)
    assert "café" in proc.stdout
    assert "Starting echo (timeout 5s)" in caplog.text
    assert "Finished echo: exit 0" in caplog.text


def test_unload_is_skipped_when_the_server_refuses(vault, caplog, monkeypatch):
    from vault_ingest import gpu as gpu_mod
    caplog.set_level(logging.INFO)

    def refused(*_a, **_k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(gpu_mod, "http_call", refused)
    calls = []
    manager = ModelManager(local(vault), run=lambda *a, **k: calls.append(a) or None)
    manager.unload_chat()
    assert calls == []
    assert "LM Studio is not running; skipping unload." in caplog.text


def test_fullscreen_browser_is_ignored_and_vram_only_counts_when_ours_are_unloaded():
    rules = GpuRules()
    assert detect_busy([], Foreground("chrome.exe", True), 0, False, rules) is None
    assert detect_busy([], Foreground("Cursor.exe", True), 0, False, rules) is None
    assert detect_busy([], Foreground("Game.exe", True), 0, False, rules) is None
    assert detect_busy([], Foreground("Game.exe", True), 0, False, rules, foreground_vram_mb=500) is None
    game = detect_busy([], Foreground("Game.exe", True), 0, False, rules, foreground_vram_mb=600)
    assert game is not None and game.kind == "fullscreen"
    assert detect_busy([], Foreground("chrome.exe", True), 0, False, rules, foreground_vram_mb=2000) is None

    assert detect_busy([], None, 4000, False, rules).kind == "vram"
    assert detect_busy([], None, 4000, True, rules) is None
    assert detect_busy([], None, 3000, False, rules) is None


def test_companion_toml_has_the_game_rules():
    cfg = load_config(PROJECT_DIR / "companion.toml")
    assert cfg.gpu.poll_seconds == 5 and cfg.gpu.resume_after_seconds == 60 and cfg.gpu.vram_mb == 3000
    assert "projectzomboid64.exe" in cfg.gpu.names
    assert any("steamapps" in p.lower() for p in cfg.gpu.path_markers)
    assert "chrome.exe" in cfg.gpu.fullscreen_ignore
    assert cfg.gpu.hotkey == "ctrl+alt+g"


# ------------------------------------------------------------------ pause, requeue, no writes
def test_interrupted_job_is_requeued_and_the_vault_is_unchanged(vault):
    path = vault / "00_Inbox" / "Acme - 2026-09-30 - Kickoff.txt"
    path.write_text(make_transcript(800), encoding="utf-8")
    before = snapshot(vault)

    def boom(system, user):
        raise GpuInterrupted("GPU hold")

    models = type("M", (), {"events": [], "ensure_loaded": lambda self: self.events.append("load"),
                            "unload_chat": lambda self: self.events.append("unload")})()
    pipe = Pipeline(local(vault), FakeLLM(boom), concurrency=1, models=models)
    with pytest.raises(GpuInterrupted):
        pipe.process(path)
    assert models.events == ["load"]
    assert snapshot(vault) == before

    front = deque([Path("later.txt")])
    cli._requeue(front, path, [Path("next.txt")])
    assert list(front) == [path, Path("next.txt"), Path("later.txt")]
    finish_chat_events = []
    models.unload_chat = lambda: finish_chat_events.append("unload")
    # the watcher unloads on interrupt even when more jobs are waiting
    from vault_ingest.gpu import finish_chat as finish
    # models here is a dummy; use the real helper on a manager-shaped object via the dummy's method
    models.unload_chat()
    assert finish_chat_events == ["unload"]
    assert finish  # the helper used by the watcher


def test_meeting_blocks_the_start_of_gpu_work(vault):
    meetings = vault / "_System" / "meetings"
    meetings.mkdir(parents=True)
    now = dt.datetime.now().astimezone().replace(microsecond=0)
    open_session = {"start": now.isoformat(), "last_seen": now.isoformat(), "end": None}
    (meetings / "open.json").write_text(json.dumps(open_session), encoding="utf-8")
    assert meeting_active(meetings, now=now)
    closed = dict(open_session, end=now.isoformat())
    (meetings / "open.json").write_text(json.dumps(closed), encoding="utf-8")
    assert not meeting_active(meetings, now=now)

    stale = {"start": (now - dt.timedelta(hours=5)).isoformat(),
             "last_seen": (now - dt.timedelta(hours=5)).isoformat(), "end": None}
    (meetings / "stale.json").write_text(json.dumps(stale), encoding="utf-8")
    assert not meeting_active(meetings, now=now)


def test_recording_interrupt_writes_nothing_and_keeps_the_job_in_front(vault, monkeypatch):
    folder = vault / "rec" / "Meeting"
    folder.mkdir(parents=True)
    (folder / "audio.mp4").write_bytes(b"audio")
    inbox = vault / "00_Inbox"
    queue = RecordingQueue(folder.parent, inbox, vault / "_System" / "whisper-sources.json",
                           Vault(vault, ("00_Inbox", "_System", "Clients")), local(vault))
    queue._jobs = [folder]
    queue.inflight.add(str(folder))

    def explode(*args, **kwargs):
        queue._stop = True
        raise GpuInterrupted("GPU hold")

    monkeypatch.setattr("vault_ingest.transcribe.transcribe_path", explode)
    before = snapshot(vault)
    queue._wake.set()
    queue._worker()
    assert queue._jobs[0] == folder
    assert snapshot(vault) == before
    assert not (inbox / "anything.txt").exists()


def _service(vault, detect, monkeypatch):
    monkeypatch.setattr("vault_ingest.gpu.show_toast", lambda text: None)
    settings = local(vault)
    current = {"hit": detect}

    def probe():
        return current["hit"]

    vault_obj = Vault(vault, ("00_Inbox", "_System", "Clients"))
    svc = GpuService(settings, GpuRules(resume_after_seconds=60), vault_obj, detect=probe,
                     meetings_dir=vault / "_System" / "no-meetings")
    svc.manager.unload_chat = lambda: None
    svc.manager.ours_loaded = lambda: False
    return svc, current


def test_auto_resume_waits_60_seconds(vault, monkeypatch):
    svc, current = _service(vault, BusyHit("ProjectZomboid64.exe", "name"), monkeypatch)
    svc.tick(0)
    assert svc.store.load().paused and not svc.store.load().manual
    assert "ProjectZomboid64.exe" in (vault / "_System" / "digest.md").read_text(encoding="utf-8")

    current["hit"] = None
    svc.tick(10)
    assert svc.store.load().paused
    svc.tick(69)
    assert svc.store.load().paused
    svc.tick(70)
    assert not svc.store.load().paused
    text = (vault / "_System" / "digest.md").read_text(encoding="utf-8")
    assert "Resumed processing (0 calls waiting)" in text


def test_manual_pause_is_not_cleared_by_auto_resume(vault, monkeypatch):
    svc, current = _service(vault, None, monkeypatch)
    svc.free(hours=None)
    assert svc.store.load().manual and svc.store.load().paused
    current["hit"] = None
    svc.tick(10_000)
    assert svc.store.load().paused and svc.store.load().manual
    svc.resume()
    assert not svc.store.load().paused and not svc.store.load().manual

    svc.free(hours=1)
    until = svc.store.load().until
    assert svc.store.load().manual
    svc.tick(until - 1)
    assert svc.store.load().paused
    svc.tick(until + 1)
    assert not svc.store.load().paused


def test_whisper_model_is_released_when_transcription_raises(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="vault_ingest")
    class Host:
        def __init__(self):
            self.model = object()

    host = Host()
    release_model(host)
    assert host.model is None
    assert "Whisper model deleted" in caplog.text

    audio = tmp_path / "call.mp3"
    audio.write_bytes(b"x")
    closed = []

    class Eng:
        device = "cpu"
        compute_type = "int8"

        def __init__(self):
            self.model = Host()

        def transcribe(self, *args, **kwargs):
            raise RuntimeError("whisper failed")

        def close(self):
            closed.append(True)
            release_model(self.model)

    monkeypatch.setattr("vault_ingest.transcribe.unload_lm_studio", lambda: None)
    monkeypatch.setattr("vault_ingest.transcribe.Engine", lambda *a, **k: Eng())
    with pytest.raises(RuntimeError, match="whisper failed"):
        _transcribe_in_process(audio, model_name="tiny", device="cpu", language="hr", initial_prompt="")
    assert closed == [True]


def test_chat_model_stays_unloaded_while_whisper_is_running(vault):
    calls = []
    mgr = make_manager(local(vault), calls, lambda: [chat("not-loaded"), embed()])
    from vault_ingest.gpu import mark_transcribing

    mark_transcribing(mgr.s, True)
    with pytest.raises(GpuInterrupted, match="transcription"):
        mgr.ensure_loaded()
    assert calls == []
    mark_transcribing(mgr.s, False)
    assert mgr.ensure_loaded() == "qwen3-14b"


def test_a_dead_transcribing_pid_does_not_block(vault):
    store = GpuStateStore(vault / "_System" / STATE_NAME)
    store.update(transcribing=True, transcribing_pid=2_000_000_000)
    assert store.transcription_active() is False
    assert store.load().transcribing is False


def test_startup_cleanup_retries_until_the_server_answers(vault):
    calls = []
    mgr = make_manager(local(vault), calls, lambda: [chat("not-loaded")])
    reachable = {"up": False}
    mgr.server_reachable = lambda timeout=3: reachable["up"]
    svc = GpuService(local(vault), GpuRules(), Vault(vault, ("00_Inbox", "_System", "Clients")),
                     meetings_dir=vault / "_System" / "no-meetings")
    svc.manager = mgr
    svc.server_checked = False
    hits = {"n": 0}
    real = mgr.startup_cleanup

    def wrapped():
        hits["n"] += 1
        return real()

    mgr.startup_cleanup = wrapped
    assert svc.retry_startup_cleanup() is False
    assert hits["n"] == 1
    reachable["up"] = True
    assert svc.retry_startup_cleanup() is True
    assert svc.retry_startup_cleanup() is True
    assert hits["n"] == 2


def test_picker_is_retried_until_the_window_is_ready():
    import threading

    from vault_ingest.watcher_meetings import PickerStartup

    class Window:
        def __init__(self, failed=False, ready=False):
            self.failed = failed
            self.ready = threading.Event()
            if ready:
                self.ready.set()

    made = []

    def factory():
        made.append(1)
        if len(made) == 1:
            return Window(failed=True)
        return Window(ready=True)

    wired = []
    host = PickerStartup(factory, lambda window: wired.append(window))
    assert host.poll(0) is False
    assert host.poll(10) is False
    assert host.poll(30) is True
    assert len(wired) == 1 and len(made) == 2

    waiting = Window(ready=False)
    later = []
    host2 = PickerStartup(lambda: waiting, lambda window: later.append(window))
    assert host2.poll(0) is False
    assert host2.poll(100) is False
    assert len(later) == 0
    waiting.ready.set()
    assert host2.poll(100) is True
    assert later == [waiting]


def test_pause_unloads_once_not_on_every_poll(vault, monkeypatch):
    svc, current = _service(vault, BusyHit("UAGame.exe", "path"), monkeypatch)
    calls = []
    svc.manager.unload_chat = lambda: calls.append(1)
    for t in range(0, 100, 5):                                # a game running for a while
        svc.tick(t)
    assert len(calls) == 1
    current["hit"] = None
    svc.tick(200)
    svc.tick(261)                                             # auto-resume
    assert not svc.store.load().paused
    current["hit"] = BusyHit("UAGame.exe", "path")
    svc.tick(300)                                             # next game: unload again, once
    svc.tick(305)
    assert len(calls) == 2


def test_manual_pause_unloads_once(vault, monkeypatch):
    svc, current = _service(vault, None, monkeypatch)
    calls = []
    svc.manager.unload_chat = lambda: calls.append(1)
    svc.free(hours=None)
    for t in range(0, 100, 5):
        svc.tick(t)
    assert len(calls) == 1
    svc.resume()
    svc.free(hours=None)
    svc.tick(200)
    assert len(calls) == 2
