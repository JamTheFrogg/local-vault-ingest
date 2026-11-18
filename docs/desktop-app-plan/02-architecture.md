# 02 - Architecture: vault-ingest as a Tauri app with the Python engine inside

Status: plan only, no code written. Code read: `/home/claude/work/vault-ingest` (all modules named in the task;
`pipeline.py` by structure). Web facts checked on 2026-11-14; every source is a link next to the claim.
"unverified" means I could not confirm it from a primary source or by reading code.
"As is" means the existing function is called without editing it.

Design rule used throughout (ponytail, level full): take the smallest design that fully works, reuse what the code
already has, add no option nobody asked for. What was left out on purpose is listed at the end of section 13.

Where this plan differs from `01-product-and-scope.md`: it recommends a small MCP server written in Python instead
of shipping the third-party `obsidian-mcp` binary (section 9). Everything else follows that scope
(Windows `.exe` first, `.msi`, Intel Mac, auto-update and semantic search after 1.0).

---

## 1. One-page overview

### Processes

| Process | What it does | Started by | New or existing |
|---|---|---|---|
| `vault-ingest` (shell, Rust + Tauri 2) | Main window, call popup window, tray icon, global shortcuts, the audio recorder, start at login. Holds no business logic. | The user, or login | New (about 700 lines of Rust, estimate) |
| Engine `serve` (`python -m vault_ingest serve`) | Local HTTP API for the UI, meeting detection loop, recording queue (Whisper), phone monitor, note index. | The shell | New command; the loops inside it exist |
| Engine `watch` (`python -m vault_ingest watch`) | The inbox pipeline, exactly as today. | `serve` (restarts it if it exits) | Existing (`cli.watch`), one flag added |
| `whisper_worker` | One transcription per process, so a native crash stays contained. | `serve` or `watch` | Existing (`transcribe.spawn_whisper_child`) |
| `llama-server` (llama.cpp) | The built-in chat + vision model. One instance; running only while a job needs it. | Whichever engine process needs it; tracked by a pid file | New binary, downloaded on first run |
| `python -m vault_ingest.mcp_server` | Vault tools for Claude, Cursor, Codex over stdio. | Each AI tool, on its own | New module |

The two engine processes are the same two the tool runs today (the "Meeting companion" and "Vault ingest watcher"
scheduled tasks). They keep their lock files (`_System/companion.lock`, `_System/ingest.lock`) and their shared
state file (`_System/gpu-state.json`), so the coordination code in `gpu.py` is not touched.

### Diagram

```
                       +--------------------------------------------------+
                       |  vault-ingest shell (Rust, Tauri 2)              |
                       |  tray | shortcuts | recorder (cpal) | autostart  |
                       |  +---------------+   +---------------------+     |
                       |  | main window   |   | popup window        |     |
                       |  | (React)       |   | (React, same app)   |     |
                       |  +-------+-------+   +----------+----------+     |
                       +----------|----------------------|----------------+
        (2) stdin/stdout lines    |   (1) HTTP + SSE     |
        popup, record, tray,      |   127.0.0.1:<random port>, token
        hotkeys  <->  shell       v                      v
                       +--------------------------------------------------+
                       |  engine "serve" (bundled CPython)                |
                       |  API | Companion.tick | RecordingQueue | Phone   |
                       |  | index (SQLite FTS5)                           |
                       +----+----------------------+----------------------+
                            | starts               | reads the files the watcher already writes:
                            v                      | _System/ingest-status.json, gpu-state.json
                       +-----------------------+   |
                       |  engine "watch"       |---+
                       |  Pipeline (unchanged) |
                       +----+-------------+----+
                            |             |
                            v             v
                    whisper_worker     llama-server  <- one instance, pid file in the app folder
                    (per job)          (OpenAI-compatible, localhost)

   Claude Desktop / Cursor / Codex --stdio--> python -m vault_ingest.mcp_server --> vault folder + index.sqlite
   Recorder writes  <recordings>/<meeting>/mic.wav + system.wav  -->  RecordingQueue  -->  00_Inbox/*.txt  -->  watch
```

### Three channels, and why

1. **UI <-> engine: HTTP + server-sent events on 127.0.0.1.** The UI needs request/response, pushed events and
   images. A webview does all three with `fetch`, `EventSource` and `<img>`; no Rust code per endpoint.
2. **Engine <-> shell: text lines on the engine's stdin/stdout.** The engine stays the one place that decides
   (as today: the companion owns the popup, tray and hotkeys through callbacks). The shell only carries out
   "show popup", "start recording", "paint tray". The code base already uses this exact pattern for its tray and
   hotkey helper (`ui_child.py`: `icon <phase>` in, `click <id>` out).
3. **UI <-> shell: a handful of Tauri commands** (engine address, window show/hide, folder picker, autostart).

The recorder lives in the shell, so a recording keeps running if the engine restarts.

<div style="page-break-before: always"></div>

## 2. Repo layout after the change

The Python package stays where it is. New things are marked `+`.

```
vault-ingest/
  vault_ingest/                 existing package, unchanged location
    cli.py  pipeline.py  gpu.py  llm.py  transcribe.py  ...      (small edits listed in section 3)
  + app.py                      wires Companion, RecordingQueue, PhoneMonitor for "serve" (no tk, no pystray)
  + server.py                   HTTP routes, event stream, shell link
  + llama_server.py             LlamaServerManager (subclass of gpu.ModelManager)
  + models.py  + models.json    catalogue, hardware check, downloads
  + index.py                    SQLite: notes, links, FTS5
  + mcp_server.py               vault tools over MCP (stdio)
  + connect.py                  writes the MCP entry into Claude / Cursor / Codex config files
  + migrate.py                  import an old install, disable old scheduled tasks
  tests/                        existing 233 tests + one test file per new module
  prompts/  configs/  companion.toml  pyproject.toml  windows/  unix/      unchanged (kept for the CLI)
+ app/                          the desktop app
    package.json  vite.config.ts  index.html
    src/                        React + TypeScript UI (main window and popup are routes of one app)
    src-tauri/
      Cargo.toml  tauri.conf.json  capabilities/  icons/  Info.plist
      src/lib.rs                setup, windows, tray, shortcuts
      src/engine.rs             start/stop the engine, the line protocol
      src/recorder.rs           microphone + system audio to WAV (cpal)
      engine/                   build output, git-ignored: Python + packages + vault_ingest + prompts
+ scripts/build_engine.py       stages app/src-tauri/engine/ for the current OS
+ .github/workflows/ci.yml      tests on every push (3 systems)
+ .github/workflows/release.yml installers on a version tag
+ THIRD_PARTY_NOTICES.md        licences of what is bundled
```

Nothing is deleted. `picker_ui.py`, `ui_child.py`, `meetily.py`, `win_toast.py` and the scripts in `windows/` and
`unix/` stay, because `vault-ingest companion` from source still uses them.

<div style="page-break-before: always"></div>

## 3. Engine API

### Recommendation: local HTTP on 127.0.0.1, random port, token

Not stdio JSON-RPC, for three reasons: every call would need Rust glue (the owner does not work in Rust); images
and long event streams are awkward over one pipe; and an HTTP API can be opened in a browser and tested with
Starlette's test client on Linux.

- Server: Starlette + uvicorn. Both arrive with the official `mcp` package, which section 9 needs anyway; they are
  also listed explicitly in `pyproject.toml` (`[project.optional-dependencies] app`). No FastAPI.
- The shell creates a random token, passes it in the environment (`VAULT_INGEST_TOKEN`) and starts
  `python -m vault_ingest serve --parent-pid <pid>`. The engine binds port 0 and prints one line
  `@@ ready {"port": N}`. The UI asks the shell once (`engine_info`) for port and token.
- Every request carries `Authorization: Bearer <token>`; `GET /events` and `GET /file` also accept `?token=`
  (`EventSource` and `<img>` cannot set headers). The engine rejects a `Host` other than `127.0.0.1:<port>`.
- The engine exits when its stdin closes or the parent pid is gone (same idea as `ui_child.run_tray`:
  "stdin closed: the companion is gone").
- Fallback if a platform's webview refuses `http://127.0.0.1` from the app page (unverified on macOS WKWebView):
  swap `fetch` for the one from Tauri's HTTP plugin, which sends the request from Rust. Same API shape.

### Endpoints v1 needs, and what each calls

| Endpoint | Purpose | Existing code it calls (as is unless noted) |
|---|---|---|
| `GET /status` | One snapshot for the status bar | `cli._names(s.inbox)`, `cli._names(s.review)`, `cli._read_job(ingest-status.json)`, `gpu.GpuStateStore.load()`, `gpu.format_status()` |
| `GET /events` | Event stream (below) | new; file polling + callbacks |
| `GET /settings`, `PATCH /settings` | Read and change settings | `config.load_settings()`, `watcher_meetings.load_config()`; writes with `config.set_llm_mode()` generalised to any key (edit) |
| `POST /mode` | Switch cloud / local, then test | `config.set_llm_mode()`, the test inside `cli.check()` (its 6 lines moved into a function that returns instead of printing) |
| `GET /meetings` | List: calls, detected sessions, waiting items | index (`type: call` notes), `sessions.SessionStore.load()`, `vault.Registry` |
| `GET /sessions/current`, `GET /sessions/{id}` | The live meeting; one meeting with screenshots | `Companion.latest_session()`, `SessionStore.find()`, `Session.to_json()` |
| `PATCH /sessions/{id}` | Client, title, "don't ingest" | `picker.apply_picker_result()`; a later change is moved by `Pipeline.sync_meeting_selections()`, which `watch` already runs every loop |
| `POST /picker/open`, `POST /picker/event` | Drive the client picker | `picker.PickerState` (`set_query`, `move`, `confirm`, `back`, `submit_field`, `answer_duplicate`, `cancel`), `picker_ui.headline()`, `picker_ui.person_guess()`, `clients.load_clients()`, `clients.MeetingMap` |
| `GET /clients?q=`, `POST /clients` | Client list / search / create | `clients.load_clients()`, `rank_clients()`, `near_duplicate()`, `create_client()` |
| `POST /sessions/{id}/record`, `POST /recording/start`, `POST /recording/stop` | Record for a meeting; record without a meeting; stop | `Companion.record_clicked()`, `meeting_signals.capture_desktop()`, `ScreenshotWatcher.add_capture()`, `SessionStore.start()`; then a line to the shell |
| `POST /sessions/{id}/screenshot` | Full-desktop shot, or open the snip overlay | `capture_desktop()` + `add_capture()`; `watcher_meetings.open_snip_overlay()` |
| `GET /queue` | Inbox, needs-review, pending steps, recordings waiting | `Pipeline.inbox_files()`, `cli._names()`, `Pipeline._load_pending()`, `RecordingQueue` state |
| `POST /inbox/import` | Add audio or transcript files | `phone.publish_inbox()` (copies as `.part`, then renames) |
| `POST /queue/client` | Give a waiting phone memo its client | `phone.inbox_filename()`, `Vault.move()` |
| `POST /reprocess` | Redo one call | `Pipeline.reprocess(path, from_audio)` |
| `POST /pending/retry`, `POST /review/retry` | Retry a failed step; put a file back in the inbox | `Pipeline.reset_pending()`, `Vault.move()` |
| `GET /gpu`, `POST /gpu/pause`, `POST /gpu/resume` | Pause state and controls | `GpuService.status_lines()`, `.free(hours=)`, `.resume()` |
| `GET /processes` | Running apps, for the "has priority" picker | `gpu.list_processes()` |
| `GET /models`, `POST /models/{id}/download`, `POST /models/test` | Catalogue, download, speed test | new `models.py`; test = body of `cli.bench_cmd()`; `transcribe.doctor_loads()` |
| `GET /notes/tree`, `GET /note`, `GET /file`, `GET /search`, `GET /graph` | Notes view | new `index.py`; `vault.get_frontmatter()`, `vault.split_frontmatter()` |
| `POST /open` | Open in Obsidian / default app | `desktop.open_path()` |
| `GET /phone`, `GET /phone/recordings`, `POST /phone/import`, `POST /phone/skip` | Phone screen | `phone_mtp.ShellPhone`, `phone.ImportLedger`, `PhoneMonitor.on_action()` (section 8) |
| `GET /connect`, `POST /connect/{tool}`, `DELETE /connect/{tool}` | "Connect my AI tools" | new `connect.py` (section 9) |
| `GET /migration`, `POST /migration/import`, `POST /migration/disable-tasks` | Move an old install over | new `migrate.py` (section 10) |

### Events pushed to the UI (`GET /events`)

| Event | Payload | Where it comes from |
|---|---|---|
| `meeting.started` / `meeting.updated` / `meeting.ended` | `Session.to_json()` | `Companion.on_started`, `Companion.on_retitle` callbacks; `Ended` returned by `Companion.tick()` |
| `recording.state` | `idle` / `recording` / `stopped` / `error`, session id, seconds, level per track | reported by the shell recorder over stdin, re-published |
| `pipeline.progress` | file, step (`transcribing`, `summary`, `evidence`, `profile`, `client`, `failed`), last error, queue counts | `serve` re-reads `_System/ingest-status.json` once a second. `Pipeline.set_step()` already writes it when `track_status` is on; `watch` turns that on |
| `gpu.state` | paused, reason, manual, until, phase, inbox, recordings | `_System/gpu-state.json` once a second (`GpuStateStore.load()`) |
| `phone.state`, `phone.new_recording` | connected / charging / absent; the newest recording not yet imported | `PhoneMonitor` (`ask` and `toast` callbacks) |
| `models.download` | id, bytes done, bytes total, error | `models.py` |
| `index.updated` | count | `index.py` after a refresh |

Progress inside one step (a percentage for Whisper) does not exist today and is not added.

### Shell link (engine stdout -> shell, shell -> engine stdin)

Lines that start with `@@ ` are commands; everything else on stdout is log text and is ignored.

| Engine -> shell | Shell -> engine |
|---|---|
| `@@ ready {"port": N}` | `tray click <id>` |
| `@@ popup show {"session": id, "activate": bool}` / `@@ popup hide` | `hotkey <combo>` |
| `@@ record start {"folder": path}` / `@@ record stop` | `recorder <state> {...}` |
| `@@ tray {"phase": "idle|processing|paused", "title": "...", "items": [...]}` | `quit` |
| `@@ hotkeys {"combos": [...]}` | |

The tray menu spec is the one `GpuService._start_child_tray()` already builds (ids, labels, default item), and
`GpuService._paint()` already sends `icon <phase>` to anything with a `send` method. `start_hotkeys(combos)` gets
an app-mode twin with the same interface (an object with `stop()`), so `GpuService.attach_hotkey()` works as is.

### Edits to existing modules (the complete list)

| File | Edit |
|---|---|
| `config.py` | `home()`: `VAULT_INGEST_HOME` if set, else `PROJECT_DIR` (so the CLI from source behaves as today). `.env` and prompts resolved through it. `set_llm_mode` body reused as `set_env_key(path, key, value)`. New keys: `LOCAL_ENGINE`, `AUTO_RECORD`. Defaults `ME_NAME="Faris"` and `TRANSCRIBE_LANGUAGE="hr"` stop being defaults; onboarding asks for both. |
| `watcher_meetings.py` | `DEFAULT_CONFIG` and the `state/` folder through `config.home()`. `load_config()` lets a few env keys override TOML values (section 10). `_lock_owner_is_companion()` also accepts `serve`. `run_companion()` itself is not changed. |
| `cli.py` | New `serve` sub-command. `watch` gets `--no-tray` (passes `tray=False` to `begin_gpu`). `check` and `bench_cmd` cores return values instead of printing. |
| `gpu.py` | `GpuService` builds its manager through a small factory (LM Studio or built-in). `GpuRules` gets `priority_apps` and a `fullscreen` switch (section 5). Public `attach_ui(icon)` instead of poking `_icon`. |
| `transcribe.py` | `RecordingQueue.poll()` and `_transcribe()` accept a folder with `mic.wav` + `system.wav` as well as `audio.mp4`. On Linux, add the downloaded CUDA folders to `LD_LIBRARY_PATH` before the worker starts (Windows is already handled by `add_nvidia_dll_directories()`). |
| `llm.py` | `ttl` is sent only for LM Studio; `auto_start` (the `lms server start` attempt) only for LM Studio; error texts say "the local model server" instead of "LM Studio". |
| `pipeline.py` | `_recordings_folder()` reads the config through `config.home()`. No other change: the app sets the existing `phone_client` hook so `_picker_choice()` (tkinter) is never reached. |
| `desktop.py` | `default_recordings_folder()` returns `~/Music/vault-ingest` for new installs. |
| Not touched | `picker.py`, `sessions.py`, `screenshots.py`, `clients.py`, `phone.py`, `phone_mtp.py`, `vault.py`, `meeting_signals.py`, `chunker.py`, `parsers.py`, `schemas.py`, `whisper_worker.py`, `notify.py` |

Toasts stay as they are in v1: `gpu.show_toast()` starts `python -m vault_ingest.notify`, which works from both
engine processes.

<div style="page-break-before: always"></div>

## 4. Recorder design per OS

### What was compared

| Option | Finding | Verdict |
|---|---|---|
| Reuse Meetily's Rust audio code | Read at commit `2166bc6` (https://github.com/Zackriya-Solutions/meetily, MIT). `frontend/src-tauri/src/audio/` is about 26,500 lines: capture is mixed with its own VAD, pipeline, checkpoints and an ffmpeg sidecar. Per OS it uses cpal 0.15 on Windows (an output device opened as input = WASAPI loopback), a hand-written Core Audio tap through the `cidre` crate on macOS (`capture/core_audio.rs`), and `libpulse-binding` monitor sources on Linux (`capture/pulse_linux.rs`). | Do not copy. Too much code for two WAV files. |
| `cpal` crate, current version | cpal 0.18 does all three natively. Windows: an input stream on a render device sets `AUDCLNT_STREAMFLAGS_LOOPBACK` (`src/host/wasapi/device.rs`). macOS: an input stream on an output-only device builds a loopback aggregate device with a tap; added in 0.17.0, "loopback recording requires 14.6+". Linux: new PipeWire and PulseAudio hosts in 0.18.0; PipeWire sink nodes are "treated as input-capable" and the stream is opened with `STREAM_CAPTURE_SINK`. Apache-2.0. Sources: https://github.com/RustAudio/cpal (CHANGELOG.md, README.md, source at commit `40ff3ad`). | **Recommended.** One code path. |
| Capture from Python with `soundcard` | BSD-3, a hobby project by its own README, Windows loopback since 2018, known WASAPI quirks, nothing documented for macOS system audio (https://github.com/bastibe/SoundCard). macOS would need a second package (`catap`, beta, https://pypi.org/project/catap/0.4.3/). | Not chosen: two libraries, and audio would stop whenever the engine restarts. |
| macOS ScreenCaptureKit | Not needed: the Core Audio tap gives audio without the Screen Recording permission for audio. | Not used for audio. |

### The design

One Rust file, `recorder.rs`, about 250 lines (estimate), built on cpal 0.18 with the `pipewire` and `pulseaudio`
features, plus `hound` (WAV writer) and `rubato` (resampler; Meetily uses it too).

- Two streams: the default input device (microphone) and the default output device opened as input (system audio).
- Each stream's callback only pushes samples into a channel. A writer thread per track downmixes to mono,
  resamples to 16 kHz, and writes 16-bit PCM. A fixed file format means a device change mid-call (headset
  plugged in) cannot break the file; cpal 0.18 reroutes default WASAPI streams on its own (CHANGELOG, 0.18.0).
- **Silence padding by the clock.** Both writers share one start time. If a track has fewer samples than the
  elapsed time, zeros are written. This is needed: loopback capture delivers nothing while nothing is playing
  (unverified in Microsoft's page, well known in practice), and the transcript merges the two tracks by timestamp.
- `flush()` every few seconds. hound documents that after a flush "the file can still be read by a compliant
  decoder up to the last flush" (https://docs.rs/hound/3.5.0/hound/struct.WavWriter.html), so a crash loses
  seconds, not the meeting.

### What is written, and how the pipeline takes it

```
<recordings>/<YYYY-MM-DD HH-MM-SS> <App>/
    mic.wav         you            16 kHz mono 16-bit
    system.wav      everyone else  16 kHz mono 16-bit
    metadata.json   {"meeting_name": "...", "created_at": "...", "session": "<id>"}
```

Two tracks, not mixed. The existing code is built for this: `transcribe.audio_tracks()` finds one file with "mic"
and one with "system" in the name, `_collect_segments()` transcribes each and labels the speaker `ME_NAME` or
"Other", `interleave()` merges by time. Meetily's mixed `audio.mp4` gave no speaker labels; this does, with no new
transcription code. `RecordingQueue` already watches a recordings folder, waits until the file size is stable,
reads `metadata.json` (`meeting_name`, `created_at`), honours "don't ingest", and writes
`00_Inbox/<name> - YYYY-MM-DD.txt`. The only edit is accepting `mic.wav` where it looks for `audio.mp4`.
The pipeline then matches the transcript to the meeting session by time, as today.

Size: about 115 MB per track per hour, 230 MB per meeting hour. Compression is an open question (section 14).

Known limit: with loudspeakers instead of headphones the microphone also hears the other side, so some sentences
appear in both tracks. Echo removal is not in v1.

### Per OS: API and what the user sees

| OS | System audio | Microphone | Prompts and settings the user meets |
|---|---|---|---|
| Windows 10 1703+ / 11 | WASAPI loopback on the default render device. Shared mode only; DRM-protected audio is not captured (https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording). | WASAPI default capture device | No prompt for loopback (the Microsoft page names none). Microphone: Settings > Privacy & security > Microphone > "Let desktop apps access your microphone" must be on (unverified wording). |
| macOS 14.6+ | Core Audio process tap, through cpal | Core Audio default input | Two prompts on the first recording: microphone (`NSMicrophoneUsageDescription`) and system audio (`NSAudioCaptureUsageDescription`). Apple: the system asks "the first time it starts recording from an aggregate device that contains a Core Audio tap"; if refused the app receives silence and has no official way to find out (https://developer.apple.com/forums/thread/771864, https://github.com/insidegui/AudioCap). So the UI must warn when the system track is flat while the meeting plays sound, and point to Privacy & Security > Screen & System Audio Recording. Screen Recording is still needed for window titles and the participant screenshot (already in the README). Both keys go into `src-tauri/Info.plist`, which Tauri merges (https://v2.tauri.app/distribute/macos-application-bundle/). |
| Linux | PipeWire: capture from the default sink. PulseAudio host as fallback (capturing a monitor source through cpal's PulseAudio host: unverified). ALSA only: microphone only. | PipeWire / PulseAudio default source | No prompt. Build needs `libpipewire-0.3-dev`, `libpulse-dev`, `libasound2-dev` (cpal README). |

macOS and Linux recording cannot be tested by the owner or by CI (hosted runners have no audio devices); see
section 12.

<div style="page-break-before: always"></div>

## 5. Models

### Chat model engine: llama-server as a second process, not llama-cpp-python

| | llama.cpp `llama-server` (recommended) | `llama-cpp-python` |
|---|---|---|
| Binaries | Prebuilt per OS and backend on every release. Release `b12380` (14 Nov 2026) lists, among others: `llama-b12380-bin-win-cuda-12.4-x64.zip` + `cudart-llama-bin-win-cuda-12.4-x64.zip`, `...-win-vulkan-x64.zip`, `...-win-cpu-x64.zip`, `...-macos-arm64.tar.gz`, `...-ubuntu-x64.tar.gz`, `...-ubuntu-vulkan-x64.tar.gz`, `...-ubuntu-cuda-12.8-x64.tar.gz` (https://github.com/ggml-org/llama.cpp/releases). Sizes: unverified (the page does not show them). | PyPI has only a 76.6 MB source archive for 0.3.36, so it compiles on install; prebuilt CUDA / Metal / Vulkan wheels sit on a separate index (https://pypi.org/project/llama-cpp-python, https://github.com/abetlen/llama-cpp-python). |
| Licence | MIT (https://github.com/ggml-org/llama.cpp, LICENSE) | MIT |
| Fit with the code | `llm.LocalStudioLLM` already talks to an OpenAI-compatible server | New provider class and in-process GPU memory to manage |
| Freeing the GPU | Stop the process | Unload inside the engine process; a crash takes the engine down |

What `llm.py` sends and what llama-server documents (https://github.com/ggml-org/llama.cpp, `tools/server/README.md`):

| `LocalStudioLLM` sends | llama-server |
|---|---|
| `POST /v1/chat/completions`, streamed | Supported |
| `response_format: {"type": "json_schema", "json_schema": {...}}` | "The OpenAI wrapper form ... is also accepted." If rejected, `_raise_http()` already falls back to plain JSON prompts. |
| `reasoning_effort: "none"` | "If `none`, reasoning/thinking is disabled." If rejected, `_DropReasoning` already retries without it. |
| Image as `image_url` with a `data:` URL | Supported when started with `--mmproj` (`docs/multimodal.md`) |
| `GET /v1/models` (after the LM Studio-only `/api/v0/models` fails) | Supported; `resolve_model()` then picks the loaded model |
| `ttl` (LM Studio only) | Unverified whether unknown fields are ignored; the plan stops sending it |

So `llm.py` needs no new provider. Metal is on by default in the macOS build (`docs/build.md`).

### What replaces the `lms` CLI

`gpu.ModelManager` calls `lms` in four places: `_lms("load", ...)`, `_lms("unload", ...)`, `lms ps`,
`lms server start`. A subclass `LlamaServerManager` (about 60 lines) overrides only those seams:

| `ModelManager` method | Built-in behaviour |
|---|---|
| `_lms("load", id, ...)` | Start `llama-server -m <gguf> --mmproj <file> -c <LOCAL_CONTEXT> -np 1 --host 127.0.0.1 --port <port> --api-key <key> --alias <id>`, write `{pid, port}` to `<home>/state/llama-server.json`, wait for `GET /health` |
| `_lms("unload", id)` | Terminate that pid, remove the file |
| `list_models()` | One row: the configured model, `state: loaded` when the pid is alive and `/health` answers |
| `ps()`, `ensure_server()`, `server_reachable()` | `[]`, nothing, `True` |

`ensure_loaded()`, `unload_chat()`, `startup_cleanup()` (kills a server left by a crash), `finish_chat()`, the VRAM
logging and all pause logic are inherited unchanged. The pid file makes the server one shared instance for both
engine processes, which is the role LM Studio plays today. The port is fixed in settings
(`LOCAL_BASE_URL=http://127.0.0.1:<port>/v1`), so `LocalStudioLLM` needs no change. One job slot (`-np 1`), as
the current `lms load --parallel 1`.

`LOCAL_ENGINE` in `.env`: `builtin` (default for new installs), `lmstudio` (today's behaviour, unchanged class),
`server` (any OpenAI-compatible address, nothing started or stopped: Ollama at `http://localhost:11434/v1`, vLLM,
a remote box). The Claude API stays `LLM_MODE=cloud`.

### Default models

| Role | For | Repo and file | Size | Licence |
|---|---|---|---|---|
| Chat + vision, standard | NVIDIA 8 GB and up; Apple Silicon with 16 GB RAM and up (threshold unverified) | `unsloth/gemma-4-12B-it-qat-GGUF`: `gemma-4-12B-it-qat-UD-Q4_K_XL.gguf` + `mmproj-F16.gguf` | 6.72 GB + 175 MB | apache-2.0 (https://huggingface.co/unsloth/gemma-4-12B-it-qat-GGUF/tree/main; base model https://huggingface.co/google/gemma-4-12B-it: text + image input, 256K context, not gated) |
| Chat + vision, light | CPU only, smaller GPUs | `unsloth/gemma-4-E4B-it-GGUF`: `gemma-4-E4B-it-Q4_K_M.gguf` + `mmproj-F16.gguf` | 4.98 GB + 990 MB | apache-2.0 (https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/tree/main) |
| Speech, GPU | NVIDIA | `Systran/faster-whisper-large-v3` (`model.bin`) | 3.09 GB | MIT (https://huggingface.co/Systran/faster-whisper-large-v3/tree/main) |
| Speech, CPU | No NVIDIA GPU, Macs | `large-v3-turbo`, which faster-whisper maps to `mobiuslabsgmbh/faster-whisper-large-v3-turbo` (`faster_whisper/utils.py`) | unverified (about 1.6 GB) | unverified |
| Embeddings (after 1.0) | Semantic search | `lm-kit/bge-m3-gguf`, Q8_0 | 635 MB | MIT (https://huggingface.co/lm-kit/bge-m3-gguf); exact file name unverified |

Notes:

- One model does text and images, so the participant screenshot and the snips need no second model. A text-only
  model still works: `VisionUnsupported` is already handled (screenshots attached without descriptions).
- The owner's 8 GB card: 6.72 GB of weights plus the projector and an 8,192-token context is tight. Whether it
  fits fully is unverified; `vault-ingest bench` (`fits_in_dedicated`) answers it in one minute, and
  llama-server's default `-ngl auto` offloads only what fits. If it does not fit well, use the light model.
- Gemma 4 12B is not in llama.cpp's own list of tested multimodal models (E2B, E4B, 26B, 31B are;
  `docs/multimodal.md`), so image input with the 12B is unverified. Fallback: Gemma 3 12B
  (`gemma-3-12b-it-Q4_K_M.gguf` 7.3 GB + `mmproj` 854 MB, https://huggingface.co/unsloth/gemma-3-12b-it-GGUF/tree/main),
  whose "gemma" licence carries use terms.
- These are third-party conversions. The catalogue (`models.json`) pins repo, file, size and SHA-256, so a model can
  be swapped without a code change. "Use a GGUF file I already have" is a file picker that sets the same path.
- The README's own warning still holds: 7-8B models write weaker notes. On a machine without a GPU the honest
  default in onboarding is the Claude API, with the light model as the private option.

### Whisper, CUDA libraries, CPU and Apple Silicon

- faster-whisper needs "cuBLAS for CUDA 12 and cuDNN 9" on the GPU; PyAV bundles FFmpeg, so no FFmpeg install
  (https://github.com/SYSTRAN/faster-whisper). The wheels are large: `nvidia-cudnn-cu12` 9.28.1.4 is 696 MB for
  Windows, `nvidia-cublas-cu12` 12.9.2.10 is 553 MB (https://pypi.org/project/nvidia-cudnn-cu12/,
  https://pypi.org/project/nvidia-cublas-cu12/). The newest cuBLAS release (12.9.3.13) has no Windows wheel, so
  versions are pinned per OS.
- **Not bundled. Downloaded on first run, only when an NVIDIA GPU is found**, with the bundled interpreter:
  `python -m pip install --target <home>/pylibs --require-hashes -r gpu-requirements-<os>.txt`. The shell starts the
  engine with `PYTHONPATH=<home>/pylibs`; `transcribe.add_nvidia_dll_directories()` already searches `sys.path` for
  `nvidia/cublas/bin` and `nvidia/cudnn/bin`, so Windows needs no code change.
- llama.cpp's `cudart` archive also contains CUDA 12 cuBLAS DLLs. Whether CTranslate2 can use those, saving the
  553 MB download, is unverified; test it in phase 4.
- **CPU only:** `TRANSCRIBE_DEVICE=cpu` is written at onboarding when no NVIDIA GPU is found, so the two failing
  CUDA attempts in `_device_attempts("auto")` are skipped. Model: `large-v3-turbo`, `int8`. Speed is unverified.
- **Apple Silicon:** CTranslate2 lists Apple Accelerate as a CPU backend and no Apple GPU backend
  (https://github.com/OpenNMT/CTranslate2/). So on a Mac, Whisper runs on the CPU and the chat model on the GPU
  through Metal. A Metal Whisper engine (whisper.cpp) would be a second transcription path; not in v1.
- AMD and Intel GPUs: chat model through the Vulkan build of llama-server; Whisper on the CPU.

### Download, verify, storage

| What | Tool | Where | Check |
|---|---|---|---|
| llama.cpp binaries (one archive per OS and backend, tag pinned) | `httpx` stream, `zipfile` / `tarfile` | `<home>/bin/llama/<tag>-<backend>/` | SHA-256 from `models.json` |
| GGUF files | `huggingface_hub.hf_hub_download` (resumes; installed with faster-whisper) | `<home>/models/` | SHA-256 with `vault.sha256_file()` |
| Whisper model | `faster_whisper.download_model()` | The standard Hugging Face cache, so a model already downloaded by today's install is reused | by the library |
| CUDA libraries | `pip --require-hashes` | `<home>/pylibs/` | pip |

Backend choice: `nvidia-smi` present -> CUDA build; else Vulkan build (Windows, Linux) or Metal (macOS); if
`llama-server --list-devices` shows no device, the CPU build. `<home>` is the per-user app folder (section 10), not
the install folder, so an update does not download anything again.

### GPU priority and pause, generalised from games

Today `gpu.detect_busy()` pauses on: a process under a game-store path (`path_markers`), a listed process name
(`names`), a fullscreen app using the GPU, or other apps holding more than `vram_mb`. Settings exposes exactly
these, plus one new list:

| Setting in the UI | Field | Change |
|---|---|---|
| "These apps have priority" (picked from running apps) | new `GpuRules.priority_apps` | checked in `detect_busy()` beside `names` |
| "Games" on / off | `path_markers`, `names` | empty when off |
| "Any fullscreen app that uses the GPU" on / off | new `GpuRules.fullscreen` | one condition |
| "Other apps use more than N MB of GPU memory" | `vram_mb` | 0 = off |
| Pause now / for 1 hour / Resume | `GpuService.free()`, `.resume()` | none |

The user's apps must not go into `names`: `GameProbe` reuses those rules to decide "this is game voice chat, not a
meeting", so a priority app would silence call detection. A separate field avoids that.

Everything else stays: `GpuStateStore`, the 60-second auto-resume, "a meeting in progress holds the queue"
(`meeting_active()`), the `WorkGate` that cancels a running request.

**No GPU:** `nvidia-smi` is missing, so the memory rule is inert (the functions already return `None`); the app
list and the manual pause work unchanged, and the UI calls it "Pause processing".

<div style="page-break-before: always"></div>

## 6. Call popup

### How detection reaches a Tauri window

Detection is not changed. `serve` builds the same objects `run_companion()` builds, without tkinter:

```
Companion(s, cfg, vault, store,
          audio=AudioProbe().playing, mic=MicWatcher(cfg.mic), gaming=GameProbe(...).running,
          on_started=..., on_retitle=...)
loop every cfg.poll_seconds:  companion.tick(now, lister.list());  companion.shots.poll();  recordings.poll()
```

| Today (tkinter) | In the app |
|---|---|
| `on_started` -> `PickerWindow.show(session, activate=False)` + toast | `on_started` -> event `meeting.started` + line `@@ popup show` |
| `on_retitle` -> `window.refresh_session` | event `meeting.updated` |
| Tray click / `Ctrl+Alt+C` -> `window.show(companion.latest_session(), activate=True)` | same call path, `activate: true` |
| `record()` closure: participant screenshot, `record_clicked`, open Meetily, follow-up shot after 120 s | same steps in `app.py`; "open Meetily" becomes `@@ record start` |
| Meeting ends | if that session is being recorded: `@@ record stop` |

Auto-record: with `AUTO_RECORD=true`, `on_started` runs the record steps at once. The popup still opens for the
client choice.

### The window

A second Tauri window, created hidden at startup: 440 x 440, bottom-right of the work area, `alwaysOnTop`,
`decorations: false`, `skipTaskbar`, `resizable: false`, `visible: false` (fields in
https://v2.tauri.app/reference/config/). It loads the `/popup` route of the same React app.

It must appear without taking the keyboard from the meeting. Tauri offers `focus: false` and `focusable`; whether
showing a hidden window later keeps focus where it was is unverified on Windows. The fallback is known to work:
`picker_ui.place_window()` does it with `SetWindowPos(..., SWP_NOACTIVATE | SWP_SHOWWINDOW)` on `HWND_TOPMOST`;
those five lines move to Rust using the window's HWND.

### `picker.py` and `picker_ui.py`

- **`picker.py` stays the logic, in Python, untouched.** `PickerState` and `apply_picker_result()` are tested
  (`tests/test_picker.py`). The popup is a plain view: it shows a snapshot (`rows`, `selected`, `mode`, `prompt`,
  `hint`, `field_value`, headline, title) and sends events (`query`, `move`, `confirm`, `back`, `cancel`,
  `submit_field`, `answer_duplicate`, `title`). One local request per keystroke costs a few milliseconds.
  Porting `PickerState` to TypeScript would duplicate tested logic; not done.
- A finished pick goes through the same routing as today: a meeting -> `apply_picker_result(action, session, store,
  s, meeting_map)`; a phone recording -> `PhoneMonitor.on_action(action)`.
- **`picker_ui.py` is not used by the app and is not deleted.** Its two pure helpers, `headline()` and
  `person_guess()`, are imported from it (the module imports tkinter only inside `_main()`).
- The pipeline's own fallback picker (`Pipeline._picker_choice`, tkinter) is switched off by setting the existing
  hook `pipeline.phone_client`; an unnamed phone memo then waits in the inbox as "needs a client" and the UI offers
  the choice (`POST /queue/client`).

Popup buttons: Record / Stop, Screenshot (full desktop, or snip), the client list, title field. Screenshot uses
`capture_desktop()` + `ScreenshotWatcher.add_capture()` or `open_snip_overlay()`; the existing
`ScreenshotWatcher.poll()` attaches snips to the meeting.

<div style="page-break-before: always"></div>

## 7. Notes, graph, search

### Where the index is built

`vault.py` has the parts but no index: `get_frontmatter()`, `split_frontmatter()`, `get_section()`, and a wikilink
pattern inside `normalize()`. The pipeline writes links with the full vault path (`Vault.link()`), so they resolve
exactly. New module `index.py` (about 150 lines) builds one SQLite file with the standard library:

| Table | Columns | Filled from |
|---|---|---|
| `notes` | path, title, type, client, date, mtime, size | walk of `*.md`; frontmatter `type` is one of `client`, `call`, `transcript-chunk`, `client-archive` (set by the pipeline) or empty for hand-written notes |
| `links` | source path, target path | `[[target]]`, `[[target|alias]]`, `![[embed]]`; a short hand-written link resolves by unique file name |
| `notes_fts` (FTS5) | title, body | `tokenize='unicode61 remove_diacritics 2'`, so `c` finds `č` and `ć` |

- Location: `<home>/index.sqlite`, not inside the vault (a synced vault should not carry a database).
- Skipped folders: `.obsidian`, `_System/backups`, `00_Inbox` (the same exclusions the MCP server has today).
- Refresh: by mtime and size; at start, after each processed call, and from a `watchdog` observer on the vault
  (already a dependency).
- FTS5 in the bundled Python's SQLite is unverified; `build_engine.py` asserts it at build time.

### Search

`GET /search?q=` runs an FTS5 `MATCH` with `bm25()` ranking and `snippet()`. The same function serves the MCP tool
`search_text`. Semantic search is after 1.0 (section 9).

### Graph library: react-force-graph-2d

| Library | Latest (npm, 2026-11-14) | Note |
|---|---|---|
| **react-force-graph-2d** | 1.29.2, 2026-09-29, MIT | One React component on canvas, d3-force layout, zoom, hover and click built in. Least code. Recommended. |
| sigma + @react-sigma/core + graphology | 4.0.6 / 6.0.0 / 0.26.0, MIT | WebGL, scales further, but three packages and a layout worker to wire. Move here only if the graph gets slow. |
| cytoscape | 3.35.0, MIT | General graph toolkit; more than needed. |
| d3-force alone | 3.0.1, ISC | Rendering would be hand-written. |

Source: https://registry.npmjs.org (package metadata). Performance of react-force-graph-2d at "a few thousand
nodes" on the owner's vault is unverified; the measure to keep it fast is in the data, not the library:
`GET /graph` leaves out `transcript-chunk` notes by default (they are most of the nodes and each links only to its
call) and can be limited to one client.

### Markdown renderer: react-markdown + remark-gfm

- `react-markdown` 10.1.0 and `remark-gfm` 4.0.1 (MIT).
- Frontmatter never reaches the renderer: `GET /note` returns it as JSON (`vault.get_frontmatter()`) and the body
  separately (`vault.split_frontmatter()`). The UI shows it as a small properties table.
- Wikilinks and embeds: `@flowershow/remark-wiki-link` 4.0.1 (2026-10-25, MIT) is the maintained plugin;
  `remark-wiki-link` was last published in 2023. Whether the plugin covers `![[image.png]]` embeds and
  `[[path|alias]]` exactly as the pipeline writes them is unverified. If not, a 15-line transform before rendering
  does it (`[[a|b]]` -> link, `![[x.png]]` -> image from `GET /file`), with one unit test.
- Callouts (`> [!note]`, used for the collapsed screenshot text): `rehype-callouts` 2.2.0 (MIT).
- "Open in Obsidian": `desktop.open_path("obsidian://open?path=<file>")` (URI form unverified here), falling back
  to the system's default app for `.md`.

Editing stays outside the app (decision 4).

<div style="page-break-before: always"></div>

## 8. Phone import

### What exists

`phone_mtp.ShellPhone` reads an Android phone through the Windows Shell and, if that copy fails, through Windows
Portable Devices. Both are Windows COM APIs. `phone.PhoneMonitor` checks the newest recording every 5 seconds and
keeps a SHA-256 ledger (`ImportLedger`).

### The screen, from existing parts

| Need (decision 8) | Code |
|---|---|
| "Is the phone there? What must I turn on?" | `ShellPhone.usb_present()` (seen over USB, even charging-only) and `storage_readable()` (File transfer chosen, phone unlocked). The existing "charging" state drives the instructions. |
| List of recordings on the phone | `ShellPhone.list_recordings()` (name, size, modified time, duration) |
| Imported / not yet | `ImportLedger.matches_fast(rec)`: same path, size and time -> `ingested`, `queued` or `skipped`, without copying the file. No match -> "new". |
| Mass import | Per file: `copy_recording()` -> `sha256_file()` -> known hash? mark it : `publish_inbox(tmp, inbox, inbox_filename(client, recording_day(...), title, stem, ext))` -> `ledger.remember(..., outcome="queued")`. One client for the batch, changeable per row. |
| Newest-recording check stays; popup leads to this screen | `PhoneMonitor` unchanged. Its `ask` callback publishes `phone.new_recording` and a toast instead of opening the tkinter picker; the choice comes back through `on_action()`. |

Detail: all phone calls run on the monitor's own thread (it calls `CoInitialize` there), so the API hands list and
import requests to that thread.

The device name, USB id and folder are per phone (`[phone]` in `companion.toml`, today the owner's own). Onboarding
for this screen lists the portable devices Windows shows and lets the user pick the device and the recordings
folder.

### Other systems, honestly

| System | v1 |
|---|---|
| Windows + Android | Works (existing code). |
| Windows + iPhone | Not supported; no code exists. |
| macOS | USB import not available. macOS has no built-in MTP support (unverified from a primary source). |
| Linux | USB import not available in v1. GNOME mounts MTP phones as a folder under `/run/user/<uid>/gvfs/` (unverified); a folder-based reader with the same four methods as `ShellPhone` would be about 40 lines, but nobody can test it now. |

On macOS and Linux the Phone screen says so and offers what does work everywhere: "Import audio files"
(`POST /inbox/import`), which copies chosen files into `00_Inbox` where the watcher already transcribes them.

<div style="page-break-before: always"></div>

## 9. MCP and "Connect my AI tools"

### The third-party server

`lstpsche/obsidian-mcp` is MIT (https://docs.rs/crate/obsidian-mcp/2.2.1, repository
https://github.com/lstpsche/obsidian-mcp), so it may be redistributed inside the installer with its licence file.
The licence of the separate `obsidian-semanticd` release asset is unverified.

### Recommendation: a small server in the engine, written with the official `mcp` Python SDK

| | Ship `obsidian-mcp` | Own server in Python (recommended) |
|---|---|---|
| Legal | Allowed (MIT) | Own code; `mcp` SDK is MIT (https://pypi.org/project/mcp/) |
| Size of the work | Fetch and pin a binary per OS in CI; keep its settings in step | About 150 lines on top of `index.py`, which the UI needs anyway |
| Stability | 2.2.1 to 5.0.0 on crates.io within months (5.0.0 on 2026-11-07); a one-author project | Changes only when this repo changes |
| Search | Its own Tantivy index beside ours | The same FTS5 index as the UI |
| Embeddings | Needs an OpenAI-compatible embeddings address running; the owner's start script notes that its in-process mode could not load multilingual models in 3.1.0 | Later, through the same llama-server |
| Write safety | Its own rules | `vault.Vault` (atomic writes, path guard) and the house rule "never delete" |
| Tests | None of ours | pytest |

Any other MCP server can still be pointed at the same folder; the vault is plain Markdown.

Tools in v1, named like the ones the owner's instructions already use (`configs/vault-CLAUDE.md`):

| Tool | Does | Code |
|---|---|---|
| `vault_list(folder)` | List notes and folders | `index.py` |
| `note_read(path)` | Frontmatter + body | `vault.get_frontmatter()`, `split_frontmatter()` |
| `search_text(query, folder?, limit?)` | Keyword search with snippets | FTS5 |
| `note_create(path, content)` | New note; refuses to overwrite | `Vault.write()` |
| `note_patch(path, heading, text)` | Append under a heading | `vault.append_to_section()`, `Vault.write()` |

Writes are allowed anywhere in the vault except `.obsidian`, `_System/backups` and `00_Inbox`. No delete and no
move tool. `search_semantic` comes with embeddings after 1.0: bge-m3 served by `llama-server --embedding` on the
CPU, one vector per note stored in the same SQLite file, compared with numpy; when the app is not running the tool
falls back to keyword search and says so.

Transport: stdio only (`MCPServer(...).run()`; stdio is the default,
https://py.sdk.modelcontextprotocol.io/run/). Every listed client supports a stdio command, and the server then
works while the app is closed. The server imports only `mcp`, `sqlite3`, `yaml` and `vault.py`, so it starts fast.
It refreshes the index by mtime before a search, at most once every few seconds.

### What "Connect my AI tools" writes

The command is the bundled interpreter, the same for all three:

```
command:  <install folder>/engine/python(.exe)
args:     ["-m", "vault_ingest.mcp_server"]
env:      VAULT_INGEST_HOME = <home>
```

| Tool | File | What is written |
|---|---|---|
| Claude Desktop | Windows `%APPDATA%\Claude\claude_desktop_config.json`; macOS `~/Library/Application Support/Claude/claude_desktop_config.json` (https://modelcontextprotocol.io/docs/develop/connect-local-servers). Linux path unverified. | Key `vault` under `mcpServers` with `command`, `args`, `env`. Other entries are kept. Claude Desktop must be fully quit and reopened (same page). |
| Cursor | `~/.cursor/mcp.json` (https://cursor.com/docs/mcp) | Same JSON shape under `mcpServers`. |
| Codex | `~/.codex/config.toml`; shared by the Codex CLI, the IDE extension and the ChatGPT desktop app (https://developers.openai.com/codex/mcp) | `[mcp_servers.vault]` with `command`, `args`, and `[mcp_servers.vault.env]`. |
| Claude Code (not in the brief; shown as a command to copy) | stored by the tool itself in `~/.claude.json` (https://code.claude.com/docs/en/mcp) | `claude mcp add --scope user --env VAULT_INGEST_HOME=<home> vault -- <python> -m vault_ingest.mcp_server` |

Rules for every write:

1. Read the file; if it is missing, create it with only our entry.
2. Copy it to `<name>.bak-<timestamp>` first.
3. JSON: parse, set one key, write back. TOML: replace or append only the `[mcp_servers.vault]` block as text
   (the standard library reads TOML but does not write it), then parse the result with `tomllib`; if that fails,
   restore the backup.
4. Never remove another server. If an old `obsidian` entry pointing at `obsidian-mcp` is found, show it and offer
   to remove it; do nothing without a yes.
5. The screen shows per tool: not installed / not connected / connected / path changed (reconnect).
6. It also shows the standing instruction text to paste (`configs/standing-instruction.txt`, with the server name
   updated); the tools' instruction fields cannot be written from outside.

Known limits: the AppImage runs from a different mount path on every start, so "Connect" is offered in the `.deb`
build only. If Claude Desktop was installed from the Microsoft Store its config may live in a different folder
(unverified).

<div style="page-break-before: always"></div>

## 10. Settings, onboarding, migration

### Where settings live

Today: `.env` and `companion.toml` next to the code, `state/` next to the code, and everything about the vault
inside the vault (`_System/`). An installed app cannot write next to its program files, so one per-user folder
replaces "next to the code":

| | Path (`<home>`) |
|---|---|
| Windows | `%LOCALAPPDATA%\<bundle identifier>\` |
| macOS | `~/Library/Application Support/<bundle identifier>/` |
| Linux | `~/.local/share/<bundle identifier>/` |

The shell gets it from Tauri's path API and passes it as `VAULT_INGEST_HOME`.

| In `<home>` | Same format as today | Written by |
|---|---|---|
| `.env` | Yes, same keys | Settings UI, one line at a time (`set_env_key`, the atomic rewrite `set_llm_mode` already does) |
| `companion.toml` | Yes; copied from the bundled default on first run | Only by hand. The UI never rewrites it, so comments and custom rules survive. |
| `state/` (`phone_imports.json`, `llama-server.json`, captures) | Yes | engine |
| `models/`, `bin/llama/`, `pylibs/`, `index.sqlite`, `logs/` | new | engine |

Values the UI must change that live in `companion.toml` today (hotkeys, screenshot folder, participant screenshot,
phone device and folder, recordings folder, GPU priority list and switches, auto-record) become `.env` keys that
override the TOML value in `load_config()`. Two files, one rule: **the UI writes `.env`; `companion.toml` is the
advanced file.** Because `<home>` is per machine, auto-record is per device, as decision 2 requires.

Vault state (`_System/ingest-log.json`, `meetings/`, `meeting-map.json`, `whisper-sources.json`,
`screenshots/index.json`, `gpu-state.json`) does not move.

A changed setting takes effect by restarting the affected engine process when no job and no meeting is active
(2-3 seconds). The recorder is in the shell and is not interrupted.

The API key stays in `.env` in plain text, as today.

### First-run onboarding

| Step | What happens | Code |
|---|---|---|
| 1. Vault | Choose an existing folder or create one (`00_Inbox`, `Clients`, `_System`). | writes `VAULT_PATH`; `Pipeline.ensure_client()` already falls back to the built-in template |
| 2. You | Your name (the microphone label) and the language of your calls. | `ME_NAME`, `TRANSCRIBE_LANGUAGE` (auto-detect is refused by `whisper_options()`, so a language must be chosen) |
| 3. AI | Hardware check, then: built-in model (shows download size) or Claude API key. | `ModelManager.vram_used_total()`, `models.py`; `LLM_MODE`, `LOCAL_ENGINE` |
| 4. Downloads | llama.cpp, model, Whisper, CUDA libraries if NVIDIA. Runs in the background; the app is usable meanwhile. | section 5 |
| 5. Test | "Test my setup": one JSON answer from the model, Whisper load on the chosen device. | cores of `cli.check()`, `cli.bench_cmd()`, `transcribe.doctor_loads()` |
| 6. Permissions | Record five seconds, show both level meters. On macOS this raises the two prompts. | recorder |
| 7. Optional | Start at login; connect AI tools; phone. | autostart plugin (https://v2.tauri.app/plugin/autostart/), section 9, section 8 |

### Migration of an existing install (the owner's)

Each step copies or disables; nothing is deleted.

| Old piece | What the app does | When it can go |
|---|---|---|
| `.env`, `companion.toml`, `state/phone_imports.json` in the tool folder | "Import from an existing vault-ingest folder": copies the three into `<home>`. `VAULT_PATH` comes with it, and with the vault all history. | Phase 2 |
| Scheduled tasks "Vault ingest watcher" and "Meeting companion" | The lock files already stop two copies from running. The app detects the old ones (`schtasks /Query`), explains, and after a yes runs `schtasks /End` and `schtasks /Change /DISABLE`. | Phase 3 |
| Meetily and `Music\meetily-recordings` | The imported `companion.toml` keeps `recordings` pointing there, so the app records into the same folder. Old `audio.mp4` folders are already listed as done in `_System/whisper-sources.json`; `reprocess --from-audio` keeps finding old audio. Meetily can then be uninstalled by the owner. | Phase 3 |
| LM Studio chat model | Imported settings keep `LOCAL_ENGINE=lmstudio`, so nothing changes until the owner switches to "built-in" in Settings. | Phase 4 |
| Scheduled task "Obsidian MCP server" (`obsidian-mcp.exe --http` on port 37842) | Stays until "Connect my AI tools" has replaced it; then disabled after a yes. | Phase 6 |
| Scheduled task "LM Studio server" + bge-m3 | Needed only for the old server's semantic search. Disabled with the task above. | Phase 6 (semantic search returns after 1.0) |

<div style="page-break-before: always"></div>

## 11. Packaging and release

### How the Python engine is shipped

| | PyInstaller one-folder | python-build-standalone + packages (recommended) | uv |
|---|---|---|---|
| What it is | Frozen executable + folder | A real, relocatable CPython with packages installed into its own `site-packages`; no venv (a venv holds absolute paths) | Installer and runner |
| Fit with the code | Breaks three call sites that run `sys.executable -m vault_ingest.<module>` (`transcribe.py:339` whisper worker, `gpu.py:1054` toasts, `ui_child.py:123`); `pip` is not available for the GPU libraries; the MCP command would need a frozen entry point | Everything works unchanged, including running the 233 tests with the shipped interpreter | Not needed at run time; `pip` inside the bundled Python does the one first-run install. Could speed up CI later. |
| Antivirus | PyInstaller's own tracker calls false positives "a recurring problem" (https://github.com/pyinstaller/pyinstaller/issues/5579) | Plain `python.exe`; risk unverified but these are the builds `uv` installs | - |
| Start time | One-folder: normal; one-file: unpacks on every start | Normal interpreter start | - |
| Size | Similar; decided by the wheels | Similar | - |

python-build-standalone: use the `install_only_stripped` archive; Linux needs glibc 2.17+
(https://docs.astral.sh/python-build-standalone/running/). Python 3.12: wheels for ctranslate2, numpy, onnxruntime
and av exist for `cp312` on all three systems (PyPI, 2026-11-14).

`scripts/build_engine.py` (one script, same on every OS):

1. Download the pinned python-build-standalone archive for the host, check its SHA-256, unpack to
   `app/src-tauri/engine/`.
2. `engine/python -m pip install --require-hashes -r requirements-app.txt` (the project, `faster-whisper`, `mcp`,
   `starlette`, `uvicorn`; not the NVIDIA wheels).
3. Copy `vault_ingest/`, `prompts/`, `configs/`, `companion.toml`.
4. Self-check: import `faster_whisper`, `mcp`; create an FTS5 table; run `python -m vault_ingest --help`.
5. macOS: ad-hoc sign every Mach-O file in `engine/` (`codesign -s -`).

Tauri bundles the folder as a resource: a directory in `bundle.resources` is copied "preserving the original
structures" (https://v2.tauri.app/develop/resources/). It is not declared as a sidecar: `externalBin` expects a
single binary named with the target triple (https://v2.tauri.app/develop/sidecar/), and Python needs its folder.
The shell starts it with `std::process::Command` from the resolved resource path. In development the shell uses
the repo's `.venv` (`VAULT_INGEST_ENGINE` environment variable).

### Workflows

`ci.yml` (every push and pull request):

```
matrix: windows-latest, macos-latest (Apple Silicon), ubuntu-22.04
steps:  checkout
        python scripts/build_engine.py
        engine/python -m pytest -q                      # the existing tests, run with the shipped interpreter
        npm ci && npm run build && npm test             # in app/
        cargo test                                      # in app/src-tauri (Linux: apt packages below)
        tauri build --debug -> upload installer as a workflow artifact (so every phase can be installed)
```

`release.yml` (on a tag `v*`), following Tauri's own example (https://v2.tauri.app/distribute/pipelines/github/):

```
permissions: contents: write
matrix:
  - windows-latest                     bundles: nsis
  - macos-latest, --target aarch64-apple-darwin      bundles: app, dmg
  - ubuntu-22.04                       bundles: appimage, deb
steps:  checkout; setup-node; dtolnay/rust-toolchain@stable; swatinem/rust-cache
        (ubuntu) apt: libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf xdg-utils
                      libpipewire-0.3-dev libpulse-dev libasound2-dev
        python scripts/build_engine.py;  engine/python -m pytest -q
        npm ci (app/)
        tauri-apps/tauri-action@v1  with tagName: v__VERSION__, releaseName, releaseDraft: true, args: per matrix
        env (all optional; empty = unsigned build):
            APPLE_CERTIFICATE, APPLE_CERTIFICATE_PASSWORD, APPLE_SIGNING_IDENTITY,
            APPLE_API_ISSUER, APPLE_API_KEY, APPLE_API_KEY_PATH          # macOS signing + notarisation
            WINDOWS_CERTIFICATE, WINDOWS_CERTIFICATE_PASSWORD            # or AZURE_CLIENT_ID / _TENANT_ID / _CLIENT_SECRET
            TAURI_SIGNING_PRIVATE_KEY, TAURI_SIGNING_PRIVATE_KEY_PASSWORD   # updater, when turned on
```

The release is created as a draft; the owner checks the files and publishes. tauri-action names bundle types
`nsis`, `msi`, `dmg`, `app`, `appimage`, `deb`, `rpm` (https://github.com/tauri-apps/tauri-action).

### Unsigned now, signing later

| | Now | Later: where the secret plugs in |
|---|---|---|
| Windows | Unsigned NSIS `-setup.exe`. Installs for the current user into `%LOCALAPPDATA%` without admin rights (https://v2.tauri.app/distribute/windows-installer/). The browser download shows a SmartScreen warning. | `bundle.windows.certificateThumbprint` or `signCommand`; secrets as above. Since 2024 a new certificate, EV or OV, can still show the warning until reputation builds (https://v2.tauri.app/distribute/sign/windows/). |
| macOS | Ad-hoc signature, `"signingIdentity": "-"`, required on Apple Silicon; the user must still allow the app in Privacy & Security (https://v2.tauri.app/distribute/sign/macos/). | The `APPLE_*` variables. Notarisation with a bundled Python needs every nested binary signed and probably hardened-runtime entitlements for the interpreter (unverified). |
| Linux | Nothing to sign. | - |
| `.msi` | Not built in 1.0 (as in the scope plan). It is one more value in `bundle.targets`; WiX runs only on Windows. | - |

### Expected sizes (estimates from PyPI wheel sizes; unverified until the first CI build)

| Package | Estimate | Main contents |
|---|---|---|
| Windows `-setup.exe` | 150-200 MB | CPython, ctranslate2 (20 MB), PyAV (28 MB), onnxruntime (17 MB), numpy (13 MB), the rest, the Tauri app |
| macOS `.dmg` (Apple Silicon) | 100-140 MB | smaller wheels on arm64 |
| Linux AppImage / `.deb` | 220-280 MB / about 200 MB | larger Linux wheels; the AppImage also carries WebKitGTK |

First-run downloads on top: chat model 5.2-6.9 GB, Whisper 3.09 GB (or the smaller turbo model), CUDA libraries
about 1.25 GB on NVIDIA machines, llama.cpp binaries (size unverified).

### Auto-update

Off in 1.0, as in the scope plan. The design is ready and small:

- `tauri-plugin-updater`, endpoint `https://github.com/JamTheFrogg/vault-ingest/releases/latest/download/latest.json`;
  tauri-action uploads `latest.json` by default (`uploadUpdaterJson`).
- The updater's own signature is mandatory: "This cannot be disabled" (https://v2.tauri.app/plugin/updater/). It
  is a free key pair made with `tauri signer generate`, not an OS certificate. Losing the private key means
  installed copies can never be updated.
- Updatable formats: NSIS on Windows, `.app.tar.gz` on macOS, AppImage on Linux. A `.deb` is updated by hand.
- Each update downloads the whole installer; models and GPU libraries are in `<home>` and are kept.
- Windows detail to solve when it is turned on: AI tools keep `engine\python.exe` running as the MCP server, and
  Windows cannot replace files in use. Before installing, the app must stop every process started from its install
  folder (`psutil` is already a dependency).

<div style="page-break-before: always"></div>

## 12. Build order

Each phase ends with something the owner installs or runs on Windows. "CI" means GitHub's hosted runners; they
have no sound devices and no GPU.

### Phase 0: three one-day checks before building (size S)

| Check | How | Answers |
|---|---|---|
| Built-in model on the 8 GB card | No code. Download the llama.cpp CUDA archive and the Gemma 4 12B GGUF by hand, start `llama-server`, set `LOCAL_BASE_URL` to it and `LOCAL_MANAGE_MODELS=false` in today's `.env`, run `vault-ingest check`, `bench`, and `reprocess` on one call with screenshots. | Does `llm.py` work against llama-server unchanged? Does the model fit? Is the note quality acceptable? Do images work? |
| Recorder | A 100-line Rust program (cpal + hound) built by CI as an `.exe` artifact: record 3 minutes of a real call. | Both tracks present and equal in length? Silence padding right? Headset change survived? Then drop the folder into the recordings folder and see the labelled transcript. |
| Engine bundle | `build_engine.py` on CI; download the folder; run `engine\python -m vault_ingest transcribe <file>` on the owner's PC. | Does Whisper run from the shipped interpreter, on the GPU, with the downloaded CUDA libraries? |

Also in this phase: `pip freeze` from the owner's working `.venv`, to pin the versions that work on his card.

### Phases

| # | Built | Reused | Verified by | Size | Linux CI can test | Only the owner's Windows PC / a Mac |
|---|---|---|---|---|---|---|
| 1 | `serve`: `<home>` folder, API read endpoints, event stream, companion loop without tkinter, `watch` as child | `Companion`, `RecordingQueue`, `SessionStore`, `cli.watch`, `GpuService`, status helpers in `cli.py` | Owner stops the two old tasks, runs `vault-ingest serve` from his venv, opens `/status` and `/meetings` in a browser, drops a transcript and watches the events | M | All of it (Starlette test client, fake window lister as in `tests/test_companion.py`) | Detection of a real call still works inside `serve` |
| 2 | Tauri shell: main window, tray, single instance, starts the bundled engine. UI: meetings list and detail, status bar, activity. `build_engine.py`, `ci.yml`. Settings import from the old folder. | Tray menu spec from `GpuService`; all of phase 1 | Owner installs the `.exe` from the CI artifact; sees his meetings; a dropped file is processed in his current mode; tray shows idle / processing / paused | L | UI build and unit tests, `cargo test`, the tests with the shipped interpreter on 3 systems, AppImage and `.deb` build | Installer, SmartScreen, tray, engine start and stop |
| 3 | Recorder, popup window, picker over HTTP, Record / Stop, auto-record setting, screenshot buttons, global shortcuts | `PickerState`, `apply_picker_result`, `record_clicked`, `capture_desktop`, `ScreenshotWatcher`, `audio_tracks`, `RecordingQueue` | A real client call: popup appears without taking focus, Record records, the call ends, a transcript with speaker labels is processed. Owner disables the old tasks and stops using Meetily. | L | Picker endpoints (existing picker tests plus API tests); `RecordingQueue` with two WAV fixtures; recorder compiles | Everything audible and visible: loopback, focus, shortcuts. macOS prompts: a Mac only |
| 4 | Built-in AI: `LlamaServerManager`, `models.py`, downloads, hardware check, AI step of onboarding, "Test my setup" | `ModelManager` (subclassed), `LocalStudioLLM`, `bench_cmd`, `doctor_loads` | Owner switches Settings to "built-in", processes a call with LM Studio closed, pauses and resumes; GPU memory is free afterwards | M-L | Manager against a stub HTTP server; download code against a local file server; CPU llama-server with a tiny model (optional) | CUDA on the 8 GB card; Metal on a Mac |
| 5 | Notes: `index.py`, note reader, search, graph, "Open in Obsidian" | `vault.py` frontmatter functions, `watchdog` | Owner browses his vault, searches a client name with and without diacritics, opens the graph for one client | M | Index and search on a fixture vault; UI unit tests | Speed on the real vault |
| 6 | `mcp_server.py`, `connect.py`, the Connect screen | `index.py`, `Vault`, `append_to_section` | Owner clicks Connect for Claude Desktop, Cursor and Codex, asks "Where are we with <client>?" in each, then disables the old MCP task | M | Tools through the SDK's in-memory client; config writers on temp files (JSON and TOML, with backups) | The three real apps reading the config |
| 7 | Screens: Phone import, GPU priority settings, Import files | `ShellPhone`, `ImportLedger`, `PhoneMonitor`, `GpuRules`, `list_processes` | Owner plugs in the phone, sees the list with imported marks, imports five at once; adds an app to the priority list and sees the pause | M | Phone logic with a fake storage (as `tests/test_phone.py`); rules in `detect_busy` | MTP with the real phone |
| 8 | Onboarding, start at login, permissions step, README, `release.yml`, first tagged release | autostart plugin, everything above | A clean Windows user account: install, onboard, record, get a note. Draft release checked and published. | M | Release workflow on a test tag | Clean-machine run; a Mac and a Linux tester if available |
| After 1.0 | Auto-update and signing; semantic search; `.msi`; Intel Mac; audio compression | - | - | S-M each | - | - |

Rough size of new code (estimate): Python 1,800-2,300 lines plus tests, Rust about 700, TypeScript decided by the
UI plan.

<div style="page-break-before: always"></div>

## 13. Risks, and things I could not verify

### Risks, most serious first

| # | Risk | What limits it |
|---|---|---|
| 1 | The built-in model on 8 GB: fit, speed, note quality, image input with Gemma 4 12B. | Phase 0 check with no code. Fallbacks: light model, Gemma 3 12B, LM Studio stays selectable, Claude API. |
| 2 | Recorder details on real hardware: loopback silence, track alignment, device changes, echo with loudspeakers. | Phase 0 recorder check; clock-based padding; fixed 16 kHz output. |
| 3 | Popup must not take focus. | Known-working Win32 call from `picker_ui.place_window()` as fallback. |
| 4 | macOS and Linux builds are untested: Core Audio tap permission, which process macOS names in the Screen Recording prompt when the Python child asks, Wayland (no window positioning, shortcuts), tray click events ("Unsupported" on Linux, https://v2.tauri.app/learn/system-tray/, so the tray menu must carry every action). | Released as experimental; a checklist for a tester. |
| 5 | Unsigned installers scare people; an unsigned `python.exe` inside may be flagged. | README instructions; signing later through the prepared variables. |
| 6 | Model files are third-party conversions that can be moved or removed. | Pinned catalogue; one-line swap; "use my own file". |
| 7 | Two processes started from the install folder by other apps (MCP) block in-place updates on Windows. | Only matters once auto-update is on; stop them first. |
| 8 | cpal 0.18 is five months old (June 2026) and its PipeWire and PulseAudio hosts are new. | Affects Linux only; Windows WASAPI code is old and stable. |

### Unverified, collected

- Webview `fetch` to `http://127.0.0.1` from the app page on macOS (WKWebView).
- Tauri: showing a hidden window without activating it on Windows; global shortcuts under Wayland.
- llama-server: reaction to the unknown `ttl` field; image input with Gemma 4 12B; whether its `cudart` DLLs
  satisfy CTranslate2; archive sizes.
- Whether the standard model fits in 8 GB with the projector and an 8k context.
- Size and licence of `mobiuslabsgmbh/faster-whisper-large-v3-turbo`; exact bge-m3 GGUF file name.
- CPU-only speeds for Whisper and the light chat model.
- cpal PulseAudio host capturing a monitor source; cpal's stated minimum macOS (its README lists 14.2 for the
  backend and 14.6 for loopback; AudioCap's README says the tap API arrived in 14.4).
- FTS5 in python-build-standalone's SQLite; antivirus behaviour toward its `python.exe`.
- `@flowershow/remark-wiki-link` handling of embeds; react-force-graph-2d speed on the real vault.
- Claude Desktop config path on Linux and for the Microsoft Store install; the `obsidian://` URI form;
  licence of `obsidian-semanticd`.
- macOS notarisation requirements for a bundled interpreter; whether macOS re-asks for permissions after each
  ad-hoc-signed update.
- Installer sizes (computed from wheel sizes, nothing was built).
- MTP on macOS and the GVFS mount path on Linux (the Arch wiki page refused the request).
- Line counts for new code are estimates.

### Skipped on purpose

| Skipped | Why |
|---|---|
| Copying Meetily's audio code | cpal now covers all three systems; 26,500 lines for two WAV files. If a fragment is copied later, add its MIT notice to `THIRD_PARTY_NOTICES.md`. |
| Merging the two engine processes into one | They work today with locks and a shared state file; merging means sharing one `GpuService` and editing `cli.watch`. |
| A TypeScript copy of `PickerState` | Tested Python logic exists. |
| MCP over HTTP; delete and move tools | stdio covers every listed tool; fewer ways to lose data. |
| A third settings file; rewriting `companion.toml` from the UI | `.env` already has a safe one-line writer. |
| Tauri notification plugin | `notify.py` already shows toasts from both processes. |
| Live transcription, speaker diarisation, echo removal, pause during a recording | Not asked. |
| Whisper on Apple or AMD GPUs | Would be a second transcription engine. |
| Model delete button, OS keychain for the API key, live settings reload | Not asked; restart-when-idle is enough. |
| Intel Mac, ARM Windows and ARM Linux builds; `.msi`; auto-update; semantic search | After 1.0, as in the scope plan. |
| sigma.js, cytoscape, FastAPI, PyInstaller, uv, llama-cpp-python | A smaller option does the job (reasons in sections 5, 7, 11). |
| Deleting `picker_ui.py`, `meetily.py`, `ui_child.py`, old scripts | Nothing is deleted without asking; the CLI still uses them. |

<div style="page-break-before: always"></div>

## 14. Open questions for the owner

| # | Question | Recommended answer |
|---|---|---|
| 1 | MCP: write a small server in Python (five tools, same names as today: `vault_list`, `note_read`, `search_text`, `note_create`, `note_patch`) instead of shipping `obsidian-mcp`? | Yes. Call the server `vault`; offer to remove the old `obsidian` entry. |
| 2 | Default local model: Gemma 4 12B QAT (Apache-2.0, 6.7 GB) if the phase 0 check passes on your 8 GB card, otherwise Gemma 4 E4B? | Yes. |
| 3 | Recordings are kept as two 16 kHz WAV files, about 230 MB per meeting hour. Enough for 1.0, with compression (which removes the WAV after checking the smaller file) after 1.0? | Yes. |
| 4 | Bundle identifier, permanent because it names the settings folder: `io.github.jamthefrogg.vault-ingest`? | Yes. |
| 5 | Your install keeps recording into `Music\meetily-recordings` so old audio stays reachable; new installs use `Music\vault-ingest`. OK? | Yes. |
| 6 | May the app disable (not delete) the old scheduled tasks during migration, after asking once per task? | Yes. |
| 7 | The shell is about 700 lines of Rust (recorder, tray, windows) with no logic in it. Will you install the Rust toolchain on your PC for `tauri dev`, or should every test build come from CI? | Install it; popup and recorder work needs fast tries. |
| 8 | Experimental macOS build requires macOS 14.6 (system audio does not work below it). OK? | Yes. |

---

## Sources

Tauri: https://v2.tauri.app/develop/sidecar/ · https://v2.tauri.app/develop/resources/ ·
https://v2.tauri.app/learn/system-tray/ · https://v2.tauri.app/plugin/global-shortcut/ ·
https://v2.tauri.app/plugin/autostart/ · https://v2.tauri.app/plugin/updater/ ·
https://v2.tauri.app/distribute/pipelines/github/ · https://github.com/tauri-apps/tauri-action ·
https://v2.tauri.app/distribute/windows-installer/ · https://v2.tauri.app/distribute/sign/windows/ ·
https://v2.tauri.app/distribute/sign/macos/ · https://v2.tauri.app/distribute/macos-application-bundle/ ·
https://v2.tauri.app/reference/config/

Audio: https://github.com/Zackriya-Solutions/meetily (commit 2166bc6) · https://github.com/RustAudio/cpal
(commit 40ff3ad) · https://docs.rs/hound/3.5.0/hound/struct.WavWriter.html ·
https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording ·
https://developer.apple.com/forums/thread/771864 · https://github.com/insidegui/AudioCap ·
https://github.com/bastibe/SoundCard · https://pypi.org/project/catap/0.4.3/

Engine and models: https://github.com/SYSTRAN/faster-whisper · https://github.com/OpenNMT/CTranslate2/ ·
https://docs.astral.sh/python-build-standalone/running/ · https://github.com/pyinstaller/pyinstaller/issues/5579 ·
https://pypi.org/project/nvidia-cudnn-cu12/ · https://pypi.org/project/nvidia-cublas-cu12/ ·
https://pypi.org/project/ctranslate2/ · https://pypi.org/project/av/ · https://github.com/ggml-org/llama.cpp
(LICENSE, tools/server/README.md, docs/multimodal.md, docs/build.md) ·
https://github.com/ggml-org/llama.cpp/releases · https://github.com/abetlen/llama-cpp-python ·
https://pypi.org/project/llama-cpp-python · https://huggingface.co/unsloth/gemma-4-12B-it-qat-GGUF/tree/main ·
https://huggingface.co/google/gemma-4-12B-it · https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/tree/main ·
https://huggingface.co/unsloth/gemma-3-12b-it-GGUF/tree/main ·
https://huggingface.co/Systran/faster-whisper-large-v3/tree/main · https://huggingface.co/lm-kit/bge-m3-gguf

MCP: https://docs.rs/crate/obsidian-mcp/2.2.1 · https://pypi.org/project/mcp/ ·
https://py.sdk.modelcontextprotocol.io/run/ · https://modelcontextprotocol.io/docs/develop/connect-local-servers ·
https://cursor.com/docs/mcp · https://developers.openai.com/codex/mcp · https://code.claude.com/docs/en/mcp

UI libraries: https://registry.npmjs.org (react-force-graph-2d, sigma, @react-sigma/core, graphology, cytoscape,
d3-force, react-markdown, remark-gfm, remark-wiki-link, @flowershow/remark-wiki-link, rehype-callouts)
