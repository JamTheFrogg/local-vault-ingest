# 01 - Product and scope: vault-ingest as a standalone desktop app

Planning document. No code. The decisions in BRIEF.md (Tauri + React shell, Python engine kept, built-in recorder,
built-in llama.cpp + Whisper, read-only notes view, same repo and name, unsigned per-OS releases, dark look, the four
"own screen" items) are treated as fixed. Everything below works inside them.

Conventions: "unverified" = I could not confirm it from a source or from the code. Confidence is given as
high / moderate / low. Scores fed to the andreessen scorer are my judgment, not measurements.
One input comes from the owner's saved memory, not from the brief: his stated goal (2026-11-13) of landing a job
within two weeks using his portfolio. It matters for framing (a) and is marked where used.

---

## 1. Verdict

### Framing (a): portfolio / open-source project that helps the owner get hired and collects stars

**Strongest counterargument first.** The build is on a longer clock than the goal it is supposed to serve. A Tauri
shell, a real recorder, a bundled Python engine with Whisper and llama.cpp, a notes view with graph, and packages for
three operating systems will not be finished by one person in two weeks, least of all by someone who does not list
Rust (confidence: moderate; there is no measured velocity for this stack). A hiring manager spends minutes on a repo:
README, one picture or video, commit history, tests, a green CI badge. Almost none of them will install an unsigned
desktop app. So for hiring, most of the value sits in the first tenth of the work (a clear promise, a 90-second demo,
an architecture picture, a tagged release), and very little in the last nine tenths. For stars the problem is
different: the pitch "your meetings become Markdown memory that AI tools read over MCP" is already taken. Minutes
(MIT, Rust + Tauri, Markdown output, an MCP server, 1.6k stars) says it almost word for word, and Meetily holds the
"open-source local meeting notes" slot with 32.3k stars (sources in section 3). A late entrant with the same sentence
gets few stars.

**Position: MARKET-FIRST-DERISK, leaning build. Confidence: moderate.**
Scorer run (`market_first_evaluator.py`, inputs assumed: size 6, growth 7, timing 7, pull 3, team 6, product 6):
market cluster 5.75, composite 5.86/10, verdict MARKET-FIRST-DERISK, with the note "pull signal is weak".

Why it still holds as a portfolio piece:
- It shows, in one repo, exactly what the owner sells: a TypeScript desktop UI, system architecture across a Rust
  shell and a Python engine, a per-OS release pipeline in GitHub Actions, local model serving, MCP integration.
  That maps onto "forward deployed engineer" better than a web app does (confidence: moderate).
- The engineering signal is already real and visible in the code: 233 tests, a pipeline that writes nothing until
  every AI step has passed, an evidence check that verifies claims against transcript quotes, backups before every
  profile update, a hash ledger against double processing.
- A portfolio piece does not need users. It needs to be legible in a video and to install without help once.

What "de-risk" means here: ship the cheap, visible things first (README promise, demo video of the tool as it works
today, then a Windows package that a stranger can install), and build the app in slices that can each be shown.
Do not wait for the finished app before showing anything.

### Framing (b): a product people other than the owner would install

**Strongest counterargument first** (this time against a "no"). The category has proven demand. Meetily has 32.3k
stars. Granola sells meeting notes with an MCP connector at $14 per user per month on its Business plan. And the
leaders leave a gap: Meetily ships no Linux binary and its repo page mentions no MCP server, Anarlog keeps notes in
SQLite and its Windows and Linux builds are beta, Minutes bundles no models and is macOS-first. A Windows-first app
with everything in one installer and a plain Markdown vault as the store would be a real variant.

**Position: MARKET-FIRST-DERISK, close to the kill line as a product bet. Confidence: moderate to high that today's
evidence does not support this framing.**
Scorer run (inputs assumed: size 6, growth 7, timing 5, pull 2, team 4, product 5): market cluster 5.0, composite
4.75/10, verdict MARKET-FIRST-DERISK with the text "Market is marginal. Lean toward NO unless you have a specific,
testable reason the demand is bigger than it looks."

Reasons:
- There is no pull. I know of no user other than the author, no request for an installer, no issue from a stranger
  (unverified: I did not look at the public repo's star or issue counts).
- The product is shaped around one person: a `Clients/` vault layout, Bosnian/Croatian defaults
  (`TRANSCRIBE_LANGUAGE=hr`, title regex with `poziv|sastanak`), one Samsung phone in `companion.toml`, a pause rule
  written for games. Those are personal conveniences. Nobody has shown they are market demands.
- The competition has teams, and one of them (Granola) has revenue. A solo maintainer with three operating systems,
  unsigned installers and a job search cannot out-support them (confidence: high).
- The features that are actually different (section 3) are a narrow wedge: client memory for people who do client
  calls. That is a small, specific audience, which is fine for a portfolio piece and thin for a product.

### Which framing the evidence supports

**(a).** Build it as a portfolio and open-source piece that is honest about being one person's tool made installable.
Consequences for scope, applied in section 4:
- Optimise for one path that works on a clean Windows machine and looks good in a 90-second video: install, record a
  call, get the note, see the graph, ask Claude about the call.
- Do not pay for breadth that only hypothetical users need: parity on three operating systems, a tested matrix of
  AI providers, rule editors, in-app editing.
- Revisit (b) only if strangers start asking for things. That is the only signal that counts.

### The six forcing questions (answers assumed; the owner should confirm)

| # | Question | Recommended answer (assumed) | Confidence | Question for the owner |
|---|---|---|---|---|
| 1 | What is the market, and is it pulling or are you pushing? | Pushing. The nameable market is people who run client calls and already work in Claude, Cursor or Codex every day (freelance developers, consultants, small agencies) and do not want a cloud notetaker or a bot in the call. Paid products next door (Granola) show people pay for meeting notes with MCP; nobody has asked for this one. | moderate | Has anyone other than you asked to use it or asked for an installer? |
| 2 | Why now? | Three outside shifts: MCP became the normal way AI tools take context (Granola shipped its connector in February 2026; Minutes, Anarlog, Screenpipe and Basic Memory all expose MCP); local models that fit a consumer GPU can write a usable summary (the project's own README is cautious here: "14B+ is the practical minimum"); bundling a Python engine as a Tauri sidecar is a documented pattern. The same "why now" applies to every competitor, and they are already there: this is a late variant, not an early bet. | moderate | What changed for you: is the trigger the job search, or do you want this to outlive it? |
| 3 | Before or after product/market fit, and what single signal proves it? | Before, with no ambiguity. The only user is the author. The signal that would count: strangers installing it without help and opening issues that ask for things. | high | How many people other than you have run it end to end? |
| 4 | What are you willing to change: product, segment or team? | The decisions fix the shell, the repo and the name. What can still move is the segment (narrow it to client calls on Windows) and the scope (section 4). | moderate | If people ask for a non-client use (team meetings, a personal journal), do you follow them or stay on client calls? |
| 5 | Where is the software leverage? | (i) The vault: every call adds to a store that every AI tool reads, at no extra work per call. (ii) The prompts, the evidence check and the tests serve every user unchanged. (iii) One version tag produces every package. What does not compound: per-OS detection code, per-app window-title rules, per-phone MTP quirks, help with unsigned installs. Those cost time per platform and per app, so v1 keeps them small. | moderate | How many hours a week can you maintain this once you have a job? |
| 6 | What must be true for a 100x outcome, and what is the cheapest test this week? | For framing (a), 100x means this repo is the reason he gets interviews, plus a few thousand stars. Three things must be true: the promise is understood in one sentence, the demo is legible in 90 seconds, a stranger can install it. Cheapest tests: a demo video of today's tool on top of the README (one day), and a clean-machine packaging spike (one to two days). Both are in section 5. | moderate | Which date matters: the job-search date or a launch date for the app? |

---

## 2. Who it is for, and the README promise

**Primary user (one person, described concretely).** A freelancer, consultant or small-agency developer who has
client calls several times a week, works on Windows, already uses Claude, Cursor or Codex daily, and is tired of
re-explaining each client to each AI tool. He does not want a bot joining calls or his transcripts on someone else's
server. He is comfortable installing an unsigned open-source app and reading a README.

**Second audience (framing a).** Engineers and hiring managers who read the repo and watch the demo. They never
install it. The README, the demo and the release page are the product for them.

**Not for (say so in the README):** teams that need a shared workspace, people who want a bot that joins calls,
phone-only users, people who want to edit notes inside the app (editing stays in Obsidian or any editor).

**One-sentence promise for the README:**

> vault-ingest records your calls and turns them into client notes in a plain Markdown vault that Claude, Cursor and
> Codex read over MCP: one install, everything runs on your own computer.

Short form for the repo description: "Your client calls become memory for every AI tool you use. Local. Plain Markdown."

Two honesty lines that belong directly under the promise:
- "Local" is true with the built-in engine. If the user picks the Claude API in settings, transcript text is sent to
  Anthropic (the current README already says this).
- Windows is the supported platform in v1; macOS and Linux packages are experimental (see section 4, Releases).

---

## 3. What already exists, and what vault-ingest does that those do not

### Comparable products (checked 2026-11-14)

| Product | What it is | Where notes live | MCP | Platforms | Licence / price | Source |
|---|---|---|---|---|---|---|
| Meetily | Local meeting recorder and summariser. Whisper or Parakeet for speech; summaries through Ollama, Claude, Groq, OpenRouter, OpenAI or any OpenAI-compatible endpoint. 32.3k stars. | "Stored locally"; the format is not stated on the repo page. | Not mentioned on the repo page. | Windows `.exe` and macOS `.dmg` installers; Linux is build-from-source. | MIT. A separate paid "PRO" edition on another codebase lists auto-detect and join, custom templates, PDF/DOCX/Markdown export. | https://github.com/Zackriya-Solutions/meetily , https://github.com/Zackriya-Solutions/meetily/releases |
| Anarlog (was Hyprnote, then Char; renamed May 2026) | "Open-source alternative to Granola": records device audio, transcribes, summarises, chat. | Local SQLite, with Markdown export. | Yes: a local CLI and MCP server. | macOS mature; Windows and Linux in beta (August 2026 update). | MIT (relicensed from GPL); enterprise parts are commercial. | https://anarlog.so/blog/char-is-now-anarlog , https://raw.githubusercontent.com/fastrepl/anarlog/main/README.md |
| Granola | Commercial meeting notes. | Granola's servers. | Yes, first-party connector at a remote URL, since February 2026. | Mac, iOS, Windows. No Linux mentioned. | Basic (free): last 30 days over MCP, no transcripts. Business $14/user/month, Enterprise from $35/user/month. | https://www.granola.ai/blog/granola-mcp , https://www.granola.ai/blog/granola-mcp-claude-chatgpt-cursor |
| Minutes (silverstein/minutes) | "Open-source, local-first Granola/Otter alternative": records meetings, calls, voice memos; local transcription; output searchable by Claude Code, Codex, Cursor. 1.6k stars. | Markdown with YAML frontmatter in `~/meetings/`; can write into an Obsidian vault. | Yes, 34 tools, started with `npx`. | Desktop app for macOS and Windows; CLI also on Linux. | MIT. Nothing bundled: the user runs a setup command to download a Whisper model; local summaries need Ollama or another server. Has person profiles and "commitments". Phone memos arrive through a synced folder. | https://github.com/silverstein/minutes , https://raw.githubusercontent.com/silverstein/minutes/main/README.md |
| Screenpipe | Records screen and audio continuously, searchable by AI; has a meeting-summary "pipe". | Local database. | Yes. | Windows, macOS, Linux (AppImage). | Source-available, not open source; commercial use needs a licence; paid plans. | https://cdn.jsdelivr.net/gh/screenpipe/screenpipe@main/README.md |
| Basic Memory | Persistent memory for AI tools. No recording, no transcription. | Markdown files plus a local SQLite index; Obsidian can open the same folder. | Yes (read, write, search, graph tools). | Cross-platform (Python). | AGPL-3.0; hosted version $15/month. | https://cdn.jsdelivr.net/gh/basicmachines-co/basic-memory@main/README.md |
| tapeback | Linux CLI recorder: system audio + mic, Whisper, Markdown into an Obsidian vault, optional cloud summary. 19 stars. | Markdown, optionally inside an Obsidian vault. | Not mentioned. | Linux only; Windows is a roadmap item. | Apache-2.0. | https://github.com/yastcher/tapeback |
| Obsidian plugins | Scuttlebutt records the mic (system audio only with workarounds, because it uses browser APIs) and sends audio and text to endpoints the user runs himself; 225 downloads. Meetily Sync copies Meetily summaries into a vault; 214 downloads. Others exist on the community list and were not reviewed one by one (unverified). | The vault. | No. | Wherever Obsidian runs (desktop). | Free plugins; need Obsidian. | https://community.obsidian.md/plugins/scuttlebutt , https://community.obsidian.md/plugins/meetily-sync |
| lstpsche/obsidian-mcp | The vault MCP server vault-ingest already uses: a single Rust binary that reads the vault from disk, works with Obsidian closed, embeddings in-process or from any OpenAI-compatible endpoint. 33 stars. | n/a | It is the MCP server. | Per-platform release archives. | MIT, so it can be shipped inside the app with attribution. | https://github.com/lstpsche/obsidian-mcp |

### What is not a differentiator (be blunt in the README)

- Recording plus local Whisper plus an LLM summary: everyone has it.
- "Plain Markdown you own": Minutes, tapeback and Basic Memory have it.
- "Your AI tools read it over MCP": Minutes, Anarlog, Granola, Screenpipe and Basic Memory have it.
- So the sentence "shared memory for every AI tool via a Markdown vault + MCP" does not set vault-ingest apart on
  its own. Minutes is the closest overlap and is ahead on stars and on macOS.

### What vault-ingest does that the others do not, or do not do together

Each line names the module that already implements it. "Not seen elsewhere" means not found in the competitors'
README or repo page; it is not proof of absence (unverified beyond those pages).

1. **Client memory, not meeting notes.** After every call the client's profile is updated: Current Status rewritten,
   open items ticked off or added, decisions and work log appended, profile backed up first, old entries archived
   past a size limit (`pipeline.plan_profile_update`, `apply_profile_update`, `archive_if_needed`, `vault.merge_open_items`).
   Minutes has person profiles and commitments, which is the nearest thing; the rest stop at one note per meeting.
2. **Evidence check.** Decisions, action items and numbers in the call note are checked against quotes from the
   transcript and flagged when no quote supports them (`pipeline.check_evidence`, `apply_evidence`, `prompts/evidence.md`).
   Not seen elsewhere.
3. **One install that contains the recorder, speech-to-text, the language model, the vault MCP server and the
   one-click wiring to Claude, Cursor and Codex.** True only after v1; today it is the opposite (four separate
   installs). Meetily has no MCP, Minutes bundles no models and needs Node for its MCP server, Anarlog stores in SQLite.
4. **The vault is the memory, in both directions.** AI tools read it and also write back what changed
   (`configs/standing-instruction.txt`, the vault's `_System/AI-Instructions.md`), and it stays a valid Obsidian
   vault. Basic Memory does this without any recording; the recorders do not do it.
5. **Call detection through "which app holds the microphone"**, so WhatsApp, Viber, Discord and browser calls are
   caught without a rule per app, plus a picker that remembers recurring meetings (`meeting_signals.py`,
   `watcher_meetings.MeetingTracker`, `clients.MeetingMap`). Meetily lists auto-detect under its paid edition;
   Minutes offers opt-in auto-recording for Teams and Zoom on macOS.
6. **Screenshots tied to the moment in the call**, captioned by a vision model, placed in the call note and in the
   transcript part for that time; participant names read from a screenshot (`screenshots.py`, `pipeline.describe_screenshots`).
7. **Courtesy on a single-GPU machine**: work is cancelled and models unloaded when something else needs the GPU,
   and resumed later (`gpu.detect_busy`, `GpuService`). Not seen elsewhere.
8. **Phone recordings over USB with no cloud folder** (`phone.py`, `phone_mtp.py`). Windows and Android only.
9. **Bosnian/Croatian/Serbian handled on purpose**: a forced Whisper language and a glossary prompt built from client
   names (`transcribe.build_initial_prompt`). The project README says Meetily's language list lacks these
   languages (unverified by me).

**Positioning that follows:** lead with "client memory" (points 1 and 2) and "zero setup on Windows" (point 3).
Use "Markdown + MCP" as the mechanism, not as the headline.

---

## 4. v1 scope

Ponytail level "ultra" applied: v1 is the smallest app that fully does what the owner asked for. Anything he did not
ask for and the core path does not need waits or is dropped. Existing Python code is called, not rewritten.

**v1 is done when this works on a clean Windows 11 machine** (no Python, Obsidian, LM Studio or Meetily installed):
install the `.exe` -> onboarding (vault, models, name and language) -> a call is detected -> the popup's Record
button records -> the call ends -> the note appears in the meetings list and in the graph -> Claude Desktop, wired
with one click, answers "where are we with this client?" from the vault.

**Push-back inside the fixed decisions (the "ultra" part):**
- Three operating systems at the same quality is not justified by the need and cannot be tested by one person with
  one Windows machine. v1 builds all packages (decision 6 kept) and supports one: Windows. macOS and Linux are
  labelled experimental until someone has run the core path on them.
- The graph gets nobody's work done. It stays in v1 because the owner asked for it and because it is the picture
  that sells the README, and it is cut to one global view.
- The optional AI providers already exist in `llm.py`. They ship as they are. Only the built-in engine and the
  Claude API are tested paths in v1.
- Do not write an MCP server. Ship the MIT-licensed binary the tool already uses.
- Do not add a database. The meetings list reads the files the engine already writes.

### Scope table

| Feature | v1 / v1.1 / dropped | Why | Existing module it reuses |
|---|---|---|---|
| **Shell and engine** | | | |
| Tauri window, tray icon, single instance | v1 | Decision 1. The tray replaces the Python tray. | Replaces `gpu.GpuService` tray code, `ui_child.py` |
| Python engine started and stopped by the shell (watcher + companion, as two processes like today) | v1 | Decision 1. Keep the process layout the code already has, including its lock files and shared state file. | `cli.watch`, `watcher_meetings.run_companion`, `cli.IngestLock`, `gpu.GpuStateStore` |
| Thin local API between shell and engine (status, meetings, actions) | v1 | The UI needs data; wrap existing commands, add no new logic. | `cli.status_cmd`, `cli.format_ingest_status`, `cli.pending_cmd`, `cli.gpu_cmd`, `cli.clients_cmd` |
| Settings and state moved from the tool folder to the per-user app folder | v1 | An installed app cannot write next to its program files. Also makes settings per device, which decision 2 requires for auto-record. | `config.load_settings`, `watcher_meetings.load_config`, `desktop.app_data_dir` |
| Start at login (toggle) | v1 | Replaces the Task Scheduler and LaunchAgent scripts. | Replaces `windows/register-startup.ps1`, `unix/register-startup.sh` |
| Command line for end users | dropped | Not asked for. The CLI stays for development from source. | `cli.py` unchanged |
| **Recorder** | | | |
| Built-in recording of microphone + system audio, written as two files (`mic`, `system`) per meeting | v1 | Decision 2; the headline fix. Two files give "me / the other side" labels with no new transcription code. | `transcribe.audio_tracks`, `interleave`, `RecordingQueue` (already watches a recordings folder), `ME_NAME` |
| Record / Stop in the main window, the tray and the popup; visible recording indicator and timer | v1 | Asked for. The indicator is not optional: the user must always know a recording is running. | `sessions.Session` (already stores "record clicked") |
| Recording stops when the detected call ends | v1 | Otherwise a forgotten recording runs for hours. | `watcher_meetings.MeetingTracker` (`Ended`, `end_grace_seconds`) |
| Auto-record every detected call, per-device setting, default off | v1 | Decision 2. Off by default so a new user is never recorded by surprise. | `watcher_meetings.Companion` (`Started` event) |
| Microphone choice in settings | v1 | Wrong default device is the most common recorder failure. | none (new, small) |
| Pause and resume a recording | v1.1 | Not asked for. | - |
| Live transcript during the call | dropped | Not asked for; large; Meetily's territory. | - |
| Speaker diarization beyond me / others | v1.1 | Not asked for; the two-file split covers the main need. | - |
| Delete audio after N days | v1.1 | Today audio is kept in `_System/audio/`. Disk use grows; a retention setting can wait. | `pipeline` audio handling |
| **Built-in AI engine** | | | |
| Whisper inside the app, model downloaded on first run, two choices (fast / accurate) | v1 | Decision 3. | `transcribe.py`, `whisper_worker.py`, `transcribe.doctor_loads` for the hardware check |
| Built-in chat model through llama.cpp's server, one default model per hardware tier, downloaded on first run | v1 | Decision 3. llama.cpp's server speaks the OpenAI chat API with schema-constrained JSON (https://raw.githubusercontent.com/ggml-org/llama.cpp/master/tools/server/README.md), which is what the local client already expects. | `llm.OpenAICompatLLM` / `LocalStudioLLM` (streaming, JSON schema, stall retry), `gpu.ModelManager` adapted to start and stop a process instead of calling `lms` |
| Model download with progress, resume and checksum; size shown before download | v1 | First-run downloads of several GB fail often; a broken half-file must not be loaded. | none (new) |
| Screenshot captions and participant names with the built-in model | v1 only if the default model supports images; otherwise screenshots are attached without captions | The code already degrades this way. llama.cpp marks multimodal support as experimental (same source). | `llm.VisionUnsupported`, `pipeline.describe_screenshots` |
| Optional providers in settings: Claude API, LM Studio, Ollama | v1, as they are | Decision 3. All three exist. One dropdown plus URL or key. Only built-in and Claude are tested for v1. | `llm.AnthropicLLM`, `LocalStudioLLM`, `OllamaLLM`, `config.set_llm_mode` |
| Claude API key stored in the OS credential store, not in a plain file | v1 | Security is never cut. Which Tauri plugin does it: unverified. | Replaces `ANTHROPIC_API_KEY` in `.env` |
| Built-in embeddings for semantic search | v1.1 | Not asked for by name. v1 ships keyword search; users who run LM Studio or Ollama can point the MCP server at their embeddings endpoint, as today. llama.cpp can serve embeddings, so v1.1 is a second small model, not new machinery. | `windows/start-obsidian-mcp.cmd` settings (API provider) |
| Choose any GGUF file; model browser | v1.1 / dropped | Not needed for the core path. | - |
| **Onboarding (first run)** | | | |
| Step 1: pick an existing vault folder or create a starter vault | v1 | Nothing works without a vault. The starter vault (inbox, Clients, client template, AI instructions) is a new small asset. | `cli.check`, `vault.Vault`, `pipeline.ensure_client`, `configs/vault-CLAUDE.md` |
| Step 2: hardware check, then download speech and chat models | v1 | Decision 3. | `transcribe.doctor_loads`, `cli.bench_cmd` |
| Step 3: your name and the call language | v1 | Today these default to "Faris" and Croatian. A stranger must be asked. | `ME_NAME`, `TRANSCRIBE_LANGUAGE`, `NOTES_LANGUAGE` |
| Step 4: connect AI tools (can be skipped) | v1 | Decision 8. | see "Connect my AI tools" |
| Permission prompts (microphone; screen recording on macOS) only where the OS requires them | v1 | Needed for the recorder to work at all. | `desktop.py` notes on macOS permissions |
| "Try it with a file": import an existing recording to see a first note | v1 | The fastest way to a first result when no call is running. It is a copy into the inbox. | `00_Inbox` watcher, `parsers.SUPPORTED_EXTENSIONS` |
| Guided tour, sample data, account | dropped | Not needed. | - |
| **Meetings list (home screen)** | | | |
| List of meetings: date, client, title, length, state (recording, transcribing, summarising, done, needs review, skipped) with a Record button on top | v1 | Asked for ("sees everything"). No new database: read what the engine writes. | `sessions.SessionStore` (`_System/meetings/*.json`), `vault.Registry` (`ingest-log.json`), `pipeline.set_step`, `cli.format_ingest_status` |
| Meeting detail: the call note rendered, screenshots, participants, links to transcript parts, "Open in Obsidian", "Reprocess" | v1 | Decision 4 and decision 8 ("shown on the meeting"). | `pipeline.render_call_note`, `pipeline.reprocess` |
| Needs-review queue with the reason and a Retry button | v1 | A failed call must be visible; today it is a file in a folder. | `pipeline.send_to_review` (`.why.md`), `pipeline.reset_pending`, `cli.pending_cmd` |
| Queue and pause state in the header (N waiting, paused because ...) | v1 | The user must see why nothing is happening. | `gpu.format_status` |
| Change the client of a finished call from the detail view | v1.1 | Exists in code; not asked for as a screen. | `pipeline.reassign_session` |
| Audio player in the detail view | v1.1 | Not asked for. | - |
| Separate Clients screen, dashboard, statistics, calendar integration | dropped | Clients are folders in the notes view; the rest was not asked for. | - |
| **Notes: read, graph, search (decision 4)** | | | |
| File tree and rendered Markdown: frontmatter, wikilinks you can click, embedded images, callouts | v1 | Decision 4. Callouts and image embeds are required because call notes use them. | The vault itself; `vault.get_frontmatter` |
| Graph: one global graph of wikilinks, coloured by note type, filter by client, transcript parts hidden by default | v1 | Asked for. Transcript parts would bury the graph (one call produces many parts). | `type:` frontmatter already written by the pipeline |
| Local graph per note, graph settings panel | v1.1 | Not needed for the core path. | - |
| Search: keyword search over the vault, filter by client and type | v1 | Decision 4. Cheapest option: ask the bundled MCP server, so the app shows exactly what the AI tools find (unverified how convenient its HTTP endpoint is to call from the app). | `obsidian-mcp` text search |
| Semantic search in the app | v1.1 | Follows built-in embeddings. | - |
| "Open in Obsidian" and "Show in folder" | v1 | Decision 4. Obsidian's `obsidian://open?path=<absolute path>` opens the note in the right vault (https://obsidian.md/help/Extending+Obsidian/Obsidian+URI). If Obsidian is not installed, open with the default editor. | `desktop.open_path` |
| Refresh when files change on disk | v1 | Notes are written by the engine and by AI tools while the app is open. | `watchdog` is already a dependency |
| Editing notes, Dataview, canvas, plugins | dropped | Decision 4 leaves editing to Obsidian. | - |
| **Call popup** | | | |
| Small always-on-top window that does not take the keyboard from the call | v1 | Replaces the tkinter window. The decision logic has no window code and is already test-driven. | `picker.PickerState`, `picker.apply_picker_result`; replaces `picker_ui.py` |
| Record that really records, Stop, timer | v1 | The owner's main complaint. | Recorder above |
| Screenshot quick action in the popup | v1 | Decision 8. | `desktop.open_snip_tool`, `screenshots.ScreenshotWatcher` |
| Client list with fuzzy search, new client, personal call, unsorted, "don't ingest" | v1 | Exists; costs only the new window. | `clients.rank_clients`, `create_client`, `near_duplicate`, `picker.py` |
| Recurring meetings pre-selected | v1 | Exists; free. | `clients.MeetingMap`, `meeting_key` |
| Participant screenshot on Record and once more after two minutes, with a toggle | v1 | Exists; free. It captures the whole desktop, so the toggle must be in settings. | `meeting_signals.capture_desktop`, `[popup] participant_screenshot` |
| Reopen the popup from the tray or a shortcut | v1 | Exists. | `watcher_meetings.start_hotkeys` |
| **Phone import (decision 8)** | | | |
| Own screen with setup instructions: unlock the phone, choose "File transfer", where voice recordings live | v1, Windows + Android only | Decision 8. The USB reading uses Windows APIs only. | `phone_mtp.ShellPhone` (`usb_present`, `storage_readable`), `phone.ConnectTracker` |
| Any connected phone and an editable recordings folder (no more hard-coded "Faris's A33") | v1 | A stranger has a different phone. Device picked from what is connected; folder as an editable path with the Samsung default. | `phone_mtp._present_device_ids`, `[phone]` in `companion.toml` |
| List of recordings on the phone, each marked imported / not imported / skipped | v1 | Decision 8. The list call and the ledger exist. | `ShellPhone.list_recordings`, `phone.ImportLedger` (queued, ingested, skipped), `phone.seed_ledger` |
| Mass import of selected recordings: file all under one client, or let the AI pick the client per recording | v1 | Decision 8. Both paths exist in the pipeline. | `ShellPhone.copy_recording`, `phone.publish_inbox`, `pipeline.identify` |
| "Newest recording" check on connect; if it is not imported, a popup that opens this screen | v1 | Decision 8, kept as it is with a new target. | `phone.PhoneMonitor`, `phone.newest_recording` |
| Folder browser on the phone; a different client per file inside one batch | v1.1 | The editable path and the two batch modes cover v1. | - |
| Phone import on macOS and Linux; iPhone | dropped for now | No code exists. On those systems the screen says: sync the phone's recordings folder into the vault inbox, which is already watched. | `00_Inbox` watcher |
| **Screenshots (decision 8)** | | | |
| Shortcut that can be rebound in settings | v1 | Decision 8. The setting exists; it needs a field. | `[screenshots] hotkey`, `watcher_meetings.open_snip_overlay` |
| Quick action in the popup | v1 | Decision 8. | as above |
| Shown on the meeting, at their time in the call, with caption | v1 | Decision 8. Already in the call note. | `pipeline.render_screenshots`, `turns_with_shots` |
| Own region-capture overlay | dropped | The operating system's snip tool already does it. | `desktop.screenshot_tools` |
| **GPU pause (decision 8)** | | | |
| "Priority apps" in settings: the user lists what goes before vault-ingest, by picking from running programs or adding a program name or folder; games folders stay as a default entry | v1 | Decision 8: not only games. The rule engine is already general; only the wording and the hard-coded game entry are personal. | `gpu.GpuRules` (`names`, `paths`, `helpers`), `gpu.detect_busy`, `gpu.list_processes` |
| Other triggers as plain settings: "another program uses more than N MB of GPU memory", "a full-screen program is in front", resume delay | v1 | Exist. | `GpuRules.vram_mb`, `fullscreen_ignore`, `resume_after_seconds` |
| Manual pause (one hour / until I resume) and resume, in the window, the tray and by shortcut | v1 | Decision 8. Exists. | `gpu.GpuStateStore`, `cli.gpu_cmd`, `[GPU_BUSY_RULES] hotkey` |
| Notification "Paused: <app> is running" and "Resumed (N waiting)" | v1 | Exists. | `gpu.show_toast`, `gpu.append_digest` |
| Schedules ("only process at night") | dropped | Not asked for. | - |
| **Connect my AI tools (decision 8)** | | | |
| Vault MCP server shipped inside the app, started and stopped with it, with a health light | v1 | Decision 8, "necessary". The binary is MIT (https://github.com/lstpsche/obsidian-mcp), reads the vault without Obsidian, and replaces the manual download and the startup script. Pin the version. | `cli._probe_mcp`, settings from `windows/start-obsidian-mcp.cmd` |
| One click per tool for Claude Desktop, Cursor and Codex: add the server entry to that tool's config file, after a backup, merging and never replacing; "Copy snippet" always available | v1 | Decision 8. Writing into another program's config can destroy it, so backup and merge are part of the feature. | `configs/claude_desktop_config.json`, `cursor-mcp.json`, `codex-config.toml` as templates |
| Claude Code: show the one-line command with a Copy button | v1 | It is a command, not a file edit. | `SETUP.md` step 5 |
| "Standing instruction" text with a Copy button per tool | v1 | Without it the tools do not look in the vault on their own. | `configs/standing-instruction.txt`, `cowork-instructions.txt` |
| Writing the standing instruction into each tool's rules file automatically | v1.1 | More files of other programs to edit; copy and paste is enough. | - |
| Read-only mode for AI tools; access tokens | v1.1 | Check first what the server supports and whether it listens on localhost only (unverified). | - |
| Own MCP server | dropped | The shipped one does the job. | - |
| **Settings** | | | |
| Screens: General (vault, name, languages, start at login), AI (provider, models), Recording (auto-record, microphone, participant screenshot), Calls (detection on/off, popup on/off), Shortcuts, Priority apps, Phone, Connect AI tools | v1 | Only what a normal user must touch. | `config.Settings`, `watcher_meetings.CompanionConfig` |
| "Open config file", "Open prompts folder", "Open logs" buttons | v1 | Everything else (chunk sizes, timeouts, detection rules per app, prompts) stays in the existing text files. | `.env`, `companion.toml`, `prompts/`, `_System/*.log` |
| Status line: engine, MCP server, model, queue | v1 | An unsigned app on other people's machines needs a way to see what is wrong. | `cli.format_ingest_status`, `cli.doctor_cmd` |
| Editor for detection rules, prompt editor, light theme, translated interface | dropped | Not asked for; decision 7 fixes the dark look. | - |
| **Releases (decision 6)** | | | |
| GitHub Actions on a version tag: Windows `.exe` installer, macOS `.dmg` (Apple Silicon), Linux AppImage and `.deb` | v1 | Decision 6. | none (new); the 233 tests run on all three systems before the build |
| Windows `.msi`; macOS Intel build | v1.1 | Decision 6 writes ".exe/.msi"; one Windows installer does the job until someone asks for the other. | - |
| Unsigned, with signing prepared (secrets read if present) | v1 | Decision 6. One correction: on Apple Silicon a completely unsigned app does not start; the macOS build needs an ad-hoc signature, which is free and needs no Apple account, and the user still has to allow the app in Privacy & Security (https://v2.tauri.app/distribute/sign/macos/). Notarisation needs the paid Apple Developer account. | - |
| Checksums file in every release; README section "why your system warns you and how to open the app" | v1 | Unsigned downloads need a way to be verified and explained. | - |
| Support level stated in the README: Windows supported, macOS and Linux experimental | v1 | Honest about what was tested. | `README.md` platform table |
| GPU libraries downloaded on first run when an NVIDIA card is found, not packed into the installer | v1 (confirm in the architecture plan) | Keeps the installer small; the CUDA libraries are large (size unverified). | `transcribe.add_nvidia_dll_directories`, `_device_attempts` |
| "Check for updates" menu item that opens the releases page | v1 | No silent network calls, no update keys to manage. | - |
| Automatic updates | v1.1 | Tauri's updater needs its own signing key pair and that cannot be turned off (https://v2.tauri.app/plugin/updater/). Add it together with code signing. | - |
| winget, Homebrew, AUR, Flatpak | dropped for now | Only worth it once people ask. | - |
| **Kept unchanged under the hood** | | | |
| Ingest pipeline: parse, chunk, digest, call note, evidence check, profile update, backups, archive, dedupe by hash | v1 | Decision 1: kept, not rewritten. | `parsers.py`, `chunker.py`, `pipeline.py`, `schemas.py`, `vault.py`, `prompts/` |
| Call detection (window titles + microphone) | v1 | Feeds the popup and auto-record. | `watcher_meetings.py`, `meeting_signals.py`, `desktop.py` |
| Owner-specific defaults removed from shipped config (name, language, phone, one game, `C:\Users\Faris` paths) | v1 | A public app must not ship one person's settings. | `companion.toml`, `.env.example`, `configs/`, `SETUP.md` |

---

## 5. Riskiest assumptions, in order, each with the cheapest test

Ordered by "how badly the plan breaks if it is false". Tests 1 to 3 are technical and can run in the first week.
Test 4 costs no engineering and should run in parallel.

| # | Assumption | Why it is risky | Cheapest test | Time |
|---|---|---|---|---|
| 1 | The Python engine with faster-whisper, its native libraries and llama.cpp can be packed as Tauri sidecars and run on a clean Windows machine. | This is what "standalone" means, and nothing in the repo proves it. The code already documents one native-library clash (Windows toast code crashes inside a process that loaded Whisper's CUDA libraries, hence `win_toast.py`). Tauri documents Python sidecars built with PyInstaller (https://v2.tauri.app/develop/sidecar/), but not with this stack. | Freeze the current engine as it is (no UI), copy it to a clean Windows Sandbox or VM, run `vault-ingest transcribe <file>` and `vault-ingest run` on one audio file. Record: does it start, installer size, start-up time, CPU and GPU both. | 1-2 days |
| 2 | A model small enough to download and run on ordinary hardware writes notes that are good enough. | The project's own README says 14B+ is the practical minimum and that 7-8B models miss details and break the JSON format. An 8 GB card fits less than that comfortably, and many laptops have no usable GPU. If this fails, "standalone" really means "needs a good GPU or a Claude key". | Start llama.cpp's server by hand, point the existing local profile at it (`LOCAL_BASE_URL`, `LOCAL_MANAGE_MODELS=false`), run `vault-ingest bench` and reprocess three real calls with two or three candidate models. Compare with the Claude notes side by side. Repeat once with the GPU disabled and note the time per call. | 1 day |
| 3 | Recording microphone plus system audio is reliable on Windows across the call apps the owner uses. | The whole reason for the project is that Record must really record. Bluetooth headsets, sample-rate differences and echo are the classic failures. Meetily's capture code is MIT and reusable, but it is Rust, which the owner does not list. | A throwaway recorder (Meetily's capture code or the simplest loopback capture) that writes `mic` and `system` files. Record three real calls: Meet in Chrome, WhatsApp desktop, Zoom; one of them on a Bluetooth headset. Feed the folder to the existing transcription and compare with a Meetily recording of the same call. | 1-2 days |
| 4 | Somebody other than the owner wants this. | Decides whether any work beyond the portfolio path is worth doing. No evidence of pull exists. | Put the promise from section 2 and a 90-second video of today's tool (call -> popup -> note -> Claude answers from the vault) on top of the README. Post it once where the audience is (Obsidian forum, a local-LLM community, Show HN) and send it to the hiring contacts. Watch for strangers asking "is there an installer?". The owner sets the threshold beforehand. | 1 day to make, 3-5 days to read |
| 5 | One person without Rust can build and keep alive a Tauri shell with a recorder, a tray, two sidecars and three packages. | Decision 1 is fixed, so the risk is managed, not removed. If it bites, everything slips. | Walking skeleton before any real screen: Tauri window + tray + start and stop the engine + one round trip ("status") + one GitHub Actions build that produces a Windows installer. If the recorder in Rust feels too heavy after test 3, capture inside the Python engine on Windows first. | 1-2 days |
| 6 | People get past the "unknown publisher" warnings. | Windows SmartScreen and macOS Gatekeeper both stop unsigned downloads; on macOS an ad-hoc-signed app must be allowed by hand in Privacy & Security (Tauri macOS signing page). Whether macOS re-asks for microphone and screen permission after every unsigned update is unverified. | Give the first Windows installer and the README instructions to three people who are not developers and watch, without helping. | half a day, once an installer exists |
| 7 | The vault MCP server can be shipped and relied on. | It is a 33-star project by one author. The owner's own start script notes that version 3.1.0 could not load multilingual embedding models in-process. | Start the shipped binary with embeddings off on a clean user profile, connect Claude Desktop and Cursor through the generated config, ask one question. Keep the config format so another vault MCP server can replace it. | half a day |
| 8 | The macOS and Linux packages work. | The detection code for both exists and has tests, but system-audio capture, permissions and packaging on them are untested by the owner. | After CI produces the packages: one person per system installs and records a two-minute call. Until that has happened the README says "experimental". | half a day each, needs a tester |

---

## 6. What to delete from the current project once the app works (proposals only)

Nothing here is to be removed without the owner's yes. Each item names what replaces it. Order: delete only after the
replacement runs on his machine for a week.

**Meetily glue (Meetily is dropped by decision 2)**
- `vault_ingest/meetily.py` (the read-only export from Meetily's database) and its tests.
- In `watcher_meetings.py`: `find_meetily`, `is_meetily_window`, `open_meetily`, the Meetily export thread, and the
  "Record (Meetily)" wording.
- In `desktop.py`: `default_meetily_database`, `default_meetily_paths`; `default_recordings_folder` becomes the
  app's own recordings folder.
- In `config.py`: the `TRANSCRIPT_SOURCE` setting (only `whisper` remains).
- In `companion.toml`: `[popup] meetily_paths`, the whole `[meetily]` section, and "meetily" in the microphone
  ignore list and the full-screen ignore list (the app's own name is already there).
- README and SETUP sections that describe Meetily.

**LM Studio scripts and special handling (LM Studio becomes optional by decision 3)**
- `windows/start-lm-studio.cmd`, `windows/use-local.cmd`, `windows/use-cloud.cmd` (replaced by the provider dropdown).
- Proposal with a question attached (section 7, question 7): reduce LM Studio to "any OpenAI-compatible address" and
  remove the `lms` load/unload lifecycle and the embedding-on-GPU checks from `gpu.ModelManager`
  (`embedding_offenders`, `embedding_status`), `transcribe.unload_lm_studio` and `cli._probe_lm_studio`. Not in v1:
  the owner still uses it.
- The legacy `LLM_PROVIDER` path in `config.py` and `.env.example` ("older setups").

**Scheduled tasks and install scripts (replaced by the installer, "start at login" and the in-app MCP server)**
- `windows/install.ps1`, `windows/register-startup.ps1`, `windows/hidden.vbs`, `windows/start-obsidian-mcp.cmd`.
- `unix/install.sh`, `unix/register-startup.sh`, `unix/start-obsidian-mcp.sh`.
- On the owner's own PC: the three Task Scheduler entries (ingest watcher, meeting companion, Obsidian MCP server),
  so the app and the old tasks do not run twice.
- The manual `bin/` download of `obsidian-mcp` (SETUP step 2).

**Old user interface (replaced by the Tauri window, tray and popup)**
- `vault_ingest/picker_ui.py` (tkinter window). `picker.py` stays: it holds the decisions and the tests.
- `vault_ingest/ui_child.py` (tray and hotkeys helper for macOS and Linux) and the tray code inside `gpu.GpuService`.
- `win_toast.py` and `notify.py`, once the shell shows the notifications.
- Dependencies that go with them: `pystray`, `windows-toasts`, the `python3-tk` system package; `pynput` only if
  the shell takes over the shortcuts.

**Owner-specific files and values (neutralise, not just delete)**
- `SETUP.md` (paths under `C:\Users\Faris`, his phone) -> replaced by an install section in the README.
- `configs/claude_desktop_config.json`, `cursor-mcp.json`, `codex-config.toml` with fixed paths -> generated by
  "Connect my AI tools". The instruction texts (`standing-instruction.txt`, `cowork-instructions.txt`,
  `vault-CLAUDE.md`) stay as templates.
- In `companion.toml`: `device_name = "Faris's A33"`, the USB id, `names = ["ProjectZomboid64.exe"]`.
- Defaults `ME_NAME=Faris` and `TRANSCRIBE_LANGUAGE=hr` -> asked in onboarding.

**Small leftovers**
- `requirements.txt` (shorter and older than the list in `pyproject.toml`).
- The stray last line `# local-vault-ingest` in `README.md`.
- `.env` next to the tool and `state/` in the tool folder -> moved to the per-user app folder.

---

## 7. Open questions for the owner (one line each)

| # | Question | Recommended answer |
|---|---|---|
| 1 | Which clock rules: the two-week job search, or a longer build? | Split them. This week: README promise + demo video of today's tool. The app is a separate, longer build shown slice by slice. |
| 2 | Do you have a Mac and a Linux machine, or a person on each, to test with? | If not: v1 says "Windows supported, macOS and Linux experimental", and the packages are still built. |
| 3 | Has anyone other than you used it or asked for an installer? | Assume no, and run test 4 in section 5 before polishing anything for other people. |
| 4 | Should the README speak to people with client calls, or pitch general "meeting notes"? | Client calls. It is the one angle the bigger projects do not own. |
| 5 | Auto-record: off by default? | Yes, off; offer to turn it on after the first manual recording. |
| 6 | Is keyword search enough for v1, with built-in semantic search in v1.1? | Yes. Until then, semantic search works if you point the MCP server at LM Studio or Ollama as today. |
| 7 | LM Studio as an option: keep the automatic load/unload of its model, or treat it as a plain server address? | Keep as it is in v1; reduce it to a plain address in v1.1 once the built-in engine has replaced it on your PC. |
| 8 | Phone import in v1 for Windows + Android only, with the other systems using a synced folder? | Yes. |

---

## Not checked, and risks to know

- I did not open the public GitHub repo, so its stars, issues and what exactly is published there are unverified.
- Competitor facts come from their README or blog pages on 2026-11-14, not from installing the products.
- No model was run and nothing was packaged; sections 4 and 5 assume the tests in section 5 pass. If test 1 or 2
  fails, the scope table changes (for example: Claude API as the default on machines without a GPU).
- Build effort is not estimated in days anywhere, because there is no basis for a number yet; the walking skeleton
  (test 5) gives the first real one.
- Recording other people has legal rules that differ by country. The app records only on the user's action unless
  he turns auto-record on; the README should say that the user is responsible for telling the other side.
