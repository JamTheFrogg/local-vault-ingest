# Product

<!-- impeccable:product-schema 1 -->

<!--
Status: draft for the owner. Written from BRIEF.md, README.md, SETUP.md and the code, without an interview.
Lines marked (assumed) were inferred and need a yes or a correction from the owner.
Lines without a mark come from the brief's fixed decisions or from the current README/code.
This file is meant to live at the repository root once confirmed. It was written to the plan folder
because the planning phase must not touch the repository.
-->

## Platform

web

A desktop app: a Tauri shell with a web UI (TypeScript/React) on Windows, macOS and Linux. The interface is
one design on all three systems and uses each system's own window frame, tray and notifications. It is not a
website and has no mobile form.

## Stack

Decided by the owner, not open: Tauri (Rust) shell, web UI in TypeScript/React, the existing Python code
bundled as the engine (pipeline and tests kept, not rewritten). Built-in recorder for microphone and system
audio. Built-in AI engine (llama.cpp) and Whisper, with models downloaded on first run. LM Studio, Ollama and
the Claude API stay as options.

## Users

- **Primary: the owner.** A freelance developer and consultant who takes client calls in Zoom, Google Meet,
  Teams, WhatsApp and Viber, and sometimes records in-person meetings on his phone. Windows 11, NVIDIA GPU with
  8 GB of video memory, 32 GB RAM. He already uses the Python version daily. Calls are in Bosnian/Croatian and
  English.
- **His situation when he uses it:** in the middle of other work when a call starts; a few minutes after a call,
  checking the note; before a client meeting, looking something up; occasionally plugging in the phone.
- **Second audience (assumed):** technical people who download the open-source build from GitHub: freelancers,
  consultants and developers who want meeting notes that stay on their own disk. They have not seen the Python
  tool and will not read a setup guide.
- **Not a user (assumed):** teams sharing one workspace, and people who want a hosted service.

## Product Purpose

vault-ingest records a call, transcribes it, and writes notes into a folder of plain Markdown files: a call
note (summary, key points, decisions, action items, open questions, notable details, screenshots), searchable
transcript parts, and an updated profile for the client. The same folder is served to the user's AI tools
(Claude, Cursor, Codex), so they have the client's context without being told.

The desktop app exists so that this works as one program: no Meetily, no LM Studio, no Obsidian required, no
terminal. The owner opens one window and sees his meetings, a graph of his notes, and a Record button that
really records.

Success (assumed): a call is recorded with at most one click, a correct note appears under the right client
without further work, and a question about a client is answered from the app in under a minute.

## Positioning

- The notes are ordinary Markdown files in a folder the user chooses. The folder stays a valid Obsidian vault.
  Nothing is locked inside the app.
- Everything can run on the user's own computer. Sending meeting text to a cloud model is a choice, not the
  default path of the built-in engine.
- It records what the computer hears. No bot joins the call.
- The same vault is what the user's AI tools read. The app is the way in for people; the vault server is the
  way in for AI tools.
- The engine follows written safety rules: it writes only inside `00_Inbox/`, `Clients/` and `_System/`, never
  deletes, never overwrites on a move, backs up a profile before changing it, and writes nothing if an AI step
  fails.

## Operating Context

- **Vault layout the app displays:** `Clients/<Name>/<Name>.md` (profile: Snapshot, Current Status, Open Items,
  Decisions Log, Preferences & Watch-outs, Work Log), `Clients/<Name>/Calls/` (call notes),
  `Clients/<Name>/Transcripts/<call>/` (parts) and `Transcripts/raw/`, `00_Inbox/` (files waiting, and
  `_needs-review/`), `_System/` (meeting session files in `_System/meetings/`, screenshots, audio, logs, digest).
  `Clients/Personal` and `Clients/Unsorted` are real folders with a special meaning.
- **Around the app:** a video-call window, a code editor, and often a game or another GPU-heavy program on the
  same machine. Obsidian or another editor is where notes are edited.
- **Call detection:** by window title and by which app holds the microphone. A meeting starts after a short
  hold and ends 60 seconds after it was last seen.
- **The client picker's decisions:** an existing client (matched by name, alias or contact, ignoring case and
  diacritics), a new client, a personal call, "Unsorted", or "don't ingest this meeting". A recurring meeting
  remembers its client.
- **Phone:** an Android phone over USB in file-transfer mode; recordings are copied, never changed on the
  phone. Windows only in the current engine.
- **GPU:** speech-to-text and the notes model share one GPU with whatever else the user runs. Rules decide what
  has priority; the user can pause by hand.

## Capabilities and Constraints

Fixed by the owner for v1:

- Read, graph and search over the vault. Editing stays in Obsidian or any editor; the app has an "Open in
  Obsidian" button.
- A built-in recorder. Auto-record of every detected call is a setting per device.
- A call popup whose Record button starts recording.
- Phone import with its own screen: what to turn on on the phone, the list of recordings marked imported or
  not, and mass import. The "newest recording" check stays and leads to that screen.
- Screenshots on a shortcut and as a quick action in the call popup, shown on the meeting.
- GPU pause that is not only for games: the user sets what has priority over the app, and can pause and resume
  by hand.
- "Connect my AI tools" for Claude, Cursor and Codex.
- Per-system packages (Windows .exe/.msi, macOS .dmg, Linux AppImage/.deb), unsigned at first.
- Same repository, app name stays "vault-ingest", MIT licence.

Constraints:

- LM Studio and Obsidian cannot be bundled (their terms do not allow it). Meetily's code is MIT and may be
  reused with attribution.
- Fonts, icons and any other bundled asset must be redistributable from an MIT repository.
- No secrets in code. Nothing is deleted without asking.
- No AI or assistant attribution anywhere in the repository (commits, pull requests, README).

Terms used in the interface (assumed): "meeting" for the recorded event, "note" for a Markdown file, "client"
for a folder under `Clients/`, "part" for a transcript chunk. The vault folder `Calls/` keeps its name.

Undecided (see the open questions in `03-ux-shape.md`): what "per device" means exactly; whether the popup is
hidden from screen sharing; the graph's default scope; whether v1 may change any note text.

## Brand Commitments

- **Name:** vault-ingest. No logo or app icon exists yet.
- **Look, in the owner's words:** dark, calm, tool-like (close to Obsidian / Linear).
- **Voice (assumed, taken from how the README is written):** plain, direct, factual. Short sentences. It says
  what the tool did and what it will not do ("It never deletes anything."). No exclamation marks, no jokes, no
  marketing words, no "AI magic". Error messages name the cause and the next step.
- **Anti-references (assumed):**
  - The current flow where Record opens another app and Record must be pressed again.
  - Setup that needs a terminal, a config file or a third-party download.
  - Meeting products that send a bot into the call or keep the notes on their servers.
  - A generic dark dashboard: cards with big numbers, a purple accent, glowing graph nodes.
  - Anything that suggests the vault belongs to the app (lock icons, "your data is safe with us").

## Evidence on Hand

- The working Python tool: about 16,000 lines, 233 tests, at `/home/claude/work/vault-ingest` (private working
  copy) and `github.com/JamTheFrogg/vault-ingest` (public, MIT).
- Real copy and behaviour to draw on: `README.md`, `SETUP.md`, `companion.toml`, the prompts in `prompts/`,
  and the picker's wording in `vault_ingest/picker.py` and `picker_ui.py`.
- The current popup's colours (`picker_ui.py`) and tray colours (`gpu.py`: grey idle, blue processing, amber
  paused). They are the only existing visual material.
- **Absent, and not to be invented:** screenshots of a desktop app, a logo, user quotes, usage numbers,
  benchmarks, model sizes (they come from the engine plan), and any claim about accuracy.

## Product Principles

1. **The call comes first.** While a call is on, the app never takes the keyboard, never covers the call's
   buttons, never asks a question that must be answered now.
2. **The vault is the product; the app is a window onto it.** Every note is a file the user can open elsewhere.
   The app shows where things are on disk and never hides the folder.
3. **Say what is happening and what happens next.** Every wait has a reason and an end: what is running, what
   is paused and why, what is waiting, when it continues.
4. **Nothing is lost and nothing is deleted.** A failed step leaves the vault untouched. A closed popup does
   not stop a recording. A recording is safe before anything else is attempted.
5. **One program, no homework.** Anything the Python version needed a terminal, a config file or another app
   for has a screen here, or it is not in v1.

## Accessibility & Inclusion

- Target (assumed): WCAG 2.2 level AA for contrast, keyboard access and focus visibility, on a dark interface.
- Everything works from the keyboard, including the graph and the audio timeline.
- State is never shown by colour alone.
- "Reduce motion" and "more contrast" system settings are respected.
- Client names, people's names and notes are often Bosnian/Croatian: every bundled font must cover č ć đ š ž,
  and search must ignore diacritics the way the client picker already does.
- Interface language is English in v1 (assumed).
