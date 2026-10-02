# vault-ingest

Turns client call transcripts into chunked, searchable Obsidian notes, so every AI connected to your vault (Claude, Cursor, Codex…) has full client context without you explaining anything.

**First time? Follow [SETUP.md](SETUP.md).**

## What happens to a transcript

```
00_Inbox/Acme - 2026-09-30 - Kickoff.txt
        │  parse (.txt .md .vtt .srt .docx) → speaker turns, filler removed
        │  chunk → ~600–900 words per part, split only between speaker turns, ~100 words overlap
        │  AI: one "Context" line + key points per part
        │  AI: call note (summary, decisions, action items, open questions, numbers)
        │  AI: profile update (Current Status rewritten; items/decisions/log appended)
        ▼
Clients/Acme/
├── Acme.md                                   ← living profile (updated)
├── Calls/2026-09-30 Kickoff.md               ← call summary, links to every part
└── Transcripts/
    ├── 2026-09-30 Kickoff/…Part 01.md …      ← searchable parts (type: transcript-chunk)
    └── raw/Acme - 2026-09-30 - Kickoff.txt   ← the original, moved here
```

The AI steps all run **before** anything is written. If one fails, the vault is untouched and the file goes to `00_Inbox/_needs-review/` with a `.why.md` note explaining what went wrong.

## Commands

Run these from this folder:

```
.\.venv\Scripts\vault-ingest check              # settings, folders, AI connection
.\.venv\Scripts\vault-ingest run --dry-run      # preview what would happen (no AI calls, no writes)
.\.venv\Scripts\vault-ingest run                # process the inbox once
.\.venv\Scripts\vault-ingest watch              # keep running (the startup task does this)
.\.venv\Scripts\vault-ingest reprocess "Clients\Acme\Transcripts\raw\Acme - 2026-09-30 - Kickoff.txt"
.\.venv\Scripts\vault-ingest reprocess --all-local  # redo every call that was made in local mode
.\.venv\Scripts\vault-ingest mode               # show the current mode (cloud / local) and model
.\.venv\Scripts\vault-ingest mode local         # switch to LM Studio, then test the connection
.\.venv\Scripts\vault-ingest mode cloud         # switch back to the Claude API, then test the connection
.\.venv\Scripts\vault-ingest companion          # meeting companion in the foreground (the startup task runs it hidden)
.\.venv\Scripts\vault-ingest clients            # list client folders
.\.venv\Scripts\vault-ingest clients add "Acme" --contact "Amra" --alias "Acme d.o.o."
.\.venv\Scripts\vault-ingest clients find "amra"                    # fuzzy matches, with scores
.\.venv\Scripts\vault-ingest transcribe "C:\path\call.mp4"          # transcript only; prints timing and the first 30 lines
.\.venv\Scripts\vault-ingest transcribe "C:\path\call.mp4" --compare hr,bs   # first 5 minutes in both languages
```

`reprocess` redoes one call, for example after you edit the prompts. It overwrites that call's note and parts, and it won't duplicate lines in the profile.

`reprocess --all-local` finds every call in `_System/ingest-log.json` that was written in local mode, tells you how many there are, and asks before doing anything. It uses the **current** mode, so run `mode cloud` first if you want Claude to redo them.

## Cloud vs local

There are two profiles in `.env`, and one line, `LLM_MODE`, picks between them:

- **cloud** (default): Claude via the API. Uses `LLM_MODEL` and `ANTHROPIC_API_KEY`. Best quality; the transcript text is sent to Anthropic.
- **local**: LM Studio on this PC. Uses the `LOCAL_*` settings. Nothing leaves your machine.

Switching only rewrites the `LLM_MODE` line, so the other profile's settings (including your API key) are kept. To switch:

- Double-click `windows\use-local.cmd` or `windows\use-cloud.cmd`. They switch the mode, test the connection, and restart the "Vault ingest watcher" background task so the change applies immediately. Pass `/nopause` to skip the "press any key" at the end.
- Or run `vault-ingest mode local` / `vault-ingest mode cloud` yourself. The background watcher keeps its old mode until it restarts (or you log in again).

**Setting up LM Studio:**

1. Download a model. A **14B+ instruct** model (for example Qwen 2.5/3 14B Instruct, Gemma 3 12B+, Mistral Small 24B) is the practical minimum. 7–8B models run, but their summaries miss details and they break the JSON format more often. Expect noticeably weaker notes than Claude either way.
2. Load it in the **Developer** tab with a **context length of at least 16k** (16384). The default of 4k is too small for the call-note and profile prompts.
3. Start the server there (default `http://localhost:1234`). If the `lms` command-line tool is on your PATH, vault-ingest tries `lms server start` once by itself when the server isn't running.

With `LOCAL_MODEL=auto`, vault-ingest uses the first loaded model that isn't an embedding model. Set `LOCAL_MODEL` to a model id to pin one.

In local mode:

- If LM Studio isn't running, or has no model loaded, files **stay in the inbox**. `run` logs one warning and exits with code 1. `watch` retries every 60 seconds. Only real processing errors go to `_needs-review`.
- Requests use LM Studio's structured output (`json_schema`). If the server or model rejects it, vault-ingest falls back to the plain JSON prompt for the rest of that run. `<think>…</think>` reasoning is stripped before parsing.
- Calls longer than `LOCAL_DIRECT_SUMMARY_MAX_WORDS` (default 3000) are summarized from the per-part digests, so they fit smaller context windows. If a prompt still doesn't fit, the log says so. Load the model with a bigger context length, or lower `CHUNK_MAX_WORDS`.

Every call note, transcript part, and ingest-log record gets `generated_by: <mode>/<model>` (for example `local/qwen3-14b`), so you can tell later which calls to redo with `reprocess --all-local`.

Older `.env` files without `LLM_MODE` still work. `LLM_PROVIDER` (`anthropic`, `openai_compat`, `ollama`) is used as before. Once `LLM_MODE` is set, it wins.

## File naming

`<Client> - YYYY-MM-DD - <Title>.ext` is the reliable way to name files.

- If the client name matches an existing folder (case-insensitive), the call is filed there. Otherwise a new client folder is created from `_System/Templates/Client.md`.
- If the file name doesn't name a client, the AI reads the start of the call and picks one of your **existing** clients, but only when it's at least 75% sure. It never invents a client.
- Generic recorder names like `zoom_recording_2026-09-28` are fine. The date is taken from the name, and the AI writes a title.
- `<Title> - YYYY-MM-DD` (how Meetily exports are named) is filed under the client if `<Title>` is an existing client's name. Otherwise the AI picks the client as above, and `<Title>` becomes the call title. Only the full three-part name creates a new client folder.

## Safety rules the tool follows

- It only writes inside `00_Inbox/`, `Clients/`, and `_System/`. The meeting companion stays inside `00_Inbox/` and `_System/`, except when you create a client from the picker: that writes `Clients/<Name>/` only. Everything else in the vault is off limits in code.
- It never deletes anything. Moves never overwrite: name clashes get ` (2)`.
- The profile is backed up to `_System/backups/` before every update. Only **Current Status** is rewritten. Open Items, Decisions Log, Work Log, and Preferences are only appended to or ticked off. Snapshot is never changed except for appended new facts.
- When a profile goes over ~1500 words, the oldest Work Log and Decisions Log entries are moved to `<Client> - Archive.md`.
- Every processed file is recorded in `_System/ingest-log.json` by content hash, so the same transcript is never processed twice.
- Transcript text is treated as data. The prompts tell the AI to ignore any instructions spoken or written inside a call.

## Settings (`.env`)

| Setting | Default | Notes |
|---|---|---|
| `VAULT_PATH` | — | your vault folder |
| `LLM_MODE` | `cloud` | `cloud` or `local`; switch with `vault-ingest mode` (see [Cloud vs local](#cloud-vs-local)) |
| `LLM_MODEL` | `claude-sonnet-5-5` | cloud model |
| `ANTHROPIC_API_KEY` | — | cloud mode only |
| `LLM_CONCURRENCY` | `4` | parallel AI calls in cloud mode |
| `DIRECT_SUMMARY_MAX_WORDS` | 6000 | cloud: longer calls are summarized from per-part digests |
| `LOCAL_BASE_URL` | `http://localhost:1234/v1` | LM Studio server |
| `LOCAL_MODEL` | `auto` | `auto` = first loaded non-embedding model, or a model id |
| `LOCAL_CONCURRENCY` | `1` | parallel AI calls in local mode |
| `LOCAL_TIMEOUT` | `900` | seconds per AI call in local mode |
| `LOCAL_DIRECT_SUMMARY_MAX_WORDS` | 3000 | local: longer calls are summarized from per-part digests |
| `NOTES_LANGUAGE` | same as transcript | e.g. `English` |
| `CHUNK_MIN_WORDS` / `CHUNK_MAX_WORDS` / `CHUNK_OVERLAP_WORDS` | 600 / 900 / 100 | |
| `PROFILE_MAX_WORDS` | 1500 | archive threshold |
| `CLIENT_CONFIDENCE` | 0.75 | minimum confidence when the AI picks the client |
| `MEETING_MATCH_TOLERANCE_MINUTES` | 15 | how far a transcript's time may be off from a companion meeting session |
| `TRANSCRIBE_LANGUAGE` | `hr` | Whisper language; see [Transcription](#transcription) |
| `TRANSCRIBE_MODEL` | `large-v3` | |
| `TRANSCRIBE_DEVICE` | `auto` | `auto`, `cuda`, or `cpu` |
| `ME_NAME` | `Faris` | microphone-track label when mic and system audio are separate files |
| `TRANSCRIPT_SOURCE` | `whisper` | `whisper` transcribes Meetily's audio; `meetily` uses Meetily's transcript |
| `LLM_PROVIDER` | `anthropic` | only used when `LLM_MODE` is not set (older setups): `anthropic`, `openai_compat`, `ollama` |

`LLM_MODE` is read from `.env` unless it is also set as a Windows environment variable, which then takes priority.

## Transcription

Meetily is the recorder. Its own transcript is not used, because its language list has no Bosnian, Croatian, or Serbian and auto-detect jumps between languages. `TRANSCRIPT_SOURCE=whisper` (the default) transcribes the audio locally with faster-whisper. Set `TRANSCRIPT_SOURCE=meetily` to go back to copying Meetily's transcript out of its database.

Audio and video dropped in `00_Inbox` (`.wav .mp3 .m4a .mp4 .mkv .webm .ogg .flac`) are transcribed first, then processed like any other transcript. The file kept in `Clients/<Client>/Transcripts/raw/` is the `.txt` transcript. The audio is not copied into the client folder. If you dropped it in the inbox, it is moved to `_System/audio/`. The call note's `audio_source` is that path (or the original path, when the audio lives outside the vault). `transcribed_by` records the engine, for example `faster-whisper/large-v3/hr`. `generated_by` is still the model that wrote the notes.

The companion watches `%USERPROFILE%\Music\meetily-recordings`. Meetily saves each meeting as one folder with a single mixed `audio.mp4` (microphone and system audio already combined, so the transcript has no speaker labels). When that file's size has stayed the same for 30 seconds, the companion transcribes it in place — it never moves or deletes Meetily's files — and writes `00_Inbox/<meeting name> - YYYY-MM-DD.txt`. The inbox watcher then ingests that text. The same folder is not transcribed twice (`_System/whisper-sources.json`).

Whisper large-v3 and a local chat model do not both fit in 8 GB. Before transcribing, vault-ingest runs `lms unload --all` when the `lms` command is available. It unloads Whisper before the summary calls, and LM Studio loads its model again on the next request.

Install the engine once, from this folder:

```
.\.venv\Scripts\python.exe -m pip install -e ".[transcribe]"
```

On Windows the CUDA build also needs the `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` packages (they are part of that extra). The first run downloads `large-v3`. Device `auto` tries the GPU at float16, then int8_float16, then the CPU at int8.

`_System/Glossary.md` is a list of names and terms (one bullet per line). They are added to Whisper's prompt together with your client folder names, capped at about 200 tokens, so unusual words are spelled the way you write them.

`vault-ingest transcribe <file>` only transcribes. `--compare hr,bs` runs the first 5 minutes in each language and writes `_System/<name> - compare.md` with the two transcripts side by side. It does not ingest the file.

## Meeting companion

`vault-ingest companion` runs in the background (the "Meeting companion" startup task) and logs to `_System/companion.log`. Its settings and detection rules are in `companion.toml` next to this README. Restart it after editing that file.

**Meeting detection.** Every 3 seconds it looks at the visible windows. A meeting starts after 2 sightings in a row and ends 60 seconds after the last one. Out of the box it recognizes:

- Zoom desktop: the "Zoom Meeting" window or the Zoom Workplace in-meeting window, not the home window
- Google Meet in Chrome, Edge, Brave, Firefox or Opera: titles starting with `Meet - ` or containing a code like `abc-defg-hij`
- Zoom in a browser: titles containing both "Zoom" and "Meeting"
- Microsoft Teams: meeting or call windows (titles with "Meeting" or "Call"). Teams names scheduled meetings after their subject, so add a rule if yours aren't picked up.

A browser only shows the title of the active tab. While the meeting's browser is playing audio, the meeting counts as ongoing, so switching tabs doesn't end it (`audio_keeps_meeting_alive`). The flip side is that other audio in that browser, such as a video, keeps the meeting open too. To recognize another app, add a `[[rules]]` block with the process names and a title regex.

**Popup.** When a meeting starts, the [client picker](#client-picker) opens in the corner and leaves the keyboard in the meeting. **Record in Meetily** brings Meetily to the front, or starts it (`[popup] meetily_paths`). It never clicks anything inside Meetily, so you press Meetily's own record button yourself. If the picker window cannot start, a Windows notification is shown instead.

**Session files.** Each meeting is written to `_System/meetings/YYYY-MM-DD HH-MM-SS <app>.json`. A file holds the app, window title, start, end, duration in seconds, whether Record was clicked, and the screenshots taken during the meeting. Times are local with their UTC offset, for example `2026-10-01T14:05:00+02:00`.

**Screenshots.** Turn on Snipping Tool > Settings > "Automatically save screenshots". Snips from Win+Shift+S (or the companion's hotkey, `Ctrl+Alt+S` by default) then land in `Pictures\Screenshots`. The companion **copies** each new one to `_System/screenshots/YYYY-MM-DD/YYYY-MM-DD HH-MM-SS.png`, named after its creation time. Your original is never moved or deleted. Snips already in the folder when the companion starts are left alone, except the last 10 minutes' worth. A snip taken during a meeting, or less than 2 minutes after one ended, is added to that meeting's session file with its offset from the meeting start. Every copy is listed in `_System/screenshots/index.json`, with `"session": null` when it was taken outside a meeting.

**When the transcript is ingested**, the pipeline looks for the session of that call. The session has to be on the same date and overlap the call's time window, give or take 15 minutes (`MEETING_MATCH_TOLERANCE_MINUTES`). That window is estimated from the transcript file's creation or modified time and the last timestamp in the transcript. If several sessions qualify, the one with the biggest overlap wins. If none does, the call is processed exactly as before. A client chosen in the picker is used as-is (`identified_by: picker`). With no choice, the file name or the AI picks the client as usual. When a session matches:

- Its screenshots are copied to `Clients/<Client>/Calls/attachments/<call name>/`.
- The AI describes each one: a caption, the key text it can read, and the kind (slide, document, website, chat, diagram, other). Claude needs nothing extra. In local mode, load a vision model in LM Studio (for example Gemma 3 or 4). A text-only model is detected automatically. The screenshots are then still attached, just without descriptions.
- The call note gets a **Screenshots** section. Each entry has its time into the call, the caption, the embedded image, and the readable text in a collapsed callout. The frontmatter gets `meeting_session`, `meeting_start`, `meeting_end`, `duration` and `screenshot_count`. The summary prompt sees the captions too, so the summary can mention what was shown.
- The transcript part covering a screenshot's time gets a `📷 [[image]] — caption (text)` line at that moment, so search finds it.

The description prompt is `prompts/screenshot.md`.

**Meetily's own transcript.** With `TRANSCRIPT_SOURCE=meetily`, the companion reads `%APPDATA%\com.meetily.ai\meeting_minutes.sqlite` every 30 seconds and never writes to it. When a transcript has stopped changing for 2 minutes, or Meetily marks its summary as finished, it writes `00_Inbox/<Meeting title> - YYYY-MM-DD.txt`. Exported meetings are listed in `_System/meetily-exported.json`. Recordings that existed before the first run are not exported. The default `TRANSCRIPT_SOURCE=whisper` skips this and transcribes `audio.mp4` instead (see [Transcription](#transcription)).

## Client picker

Meetily stays the recorder. The picker only chooses where the call is filed.

**Hotkeys**

| Hotkey | What it does |
|---|---|
| `Ctrl+Alt+C` | Open the client picker and give it the keyboard (`[popup] picker_hotkey`) |
| `Ctrl+Alt+S` | Open the snip overlay, same as Win+Shift+S (`[screenshots] hotkey`) |

When a meeting is detected, a small always-on-top window (about 420×320, bottom-right) opens **without** taking focus, so you can keep typing in the meeting. It takes the keyboard when you click it or press `Ctrl+Alt+C`. You can open it any time, including when no meeting is running.

Type to filter. Up and Down move. Enter confirms. Esc closes the window and leaves the choice for later (the file name or the AI decides, as before). The list shows the top 8 matches, then three fixed rows:

- **Create new client:** followed by what you typed
- **Not a client call (Unsorted)** — files the call under `Clients/Unsorted`
- **Don't ingest this meeting** — nothing from that time window is processed. A recording is not transcribed, and a transcript already in the inbox is moved to `_System/skipped/`

Matching ignores case and diacritics (`c/č/ć`, `s/š`, `z/ž`, `dj/đ`) and looks at the client folder name, `aliases` in the profile frontmatter, and `Contact:` names in the Snapshot section. There is an optional call-title field. **Record in Meetily** launches or focuses Meetily and does not click inside it.

**Create new client** reuses the normal client template and creates `Calls`, `Transcripts`, and `Work`. The name is sanitized. If an existing client scores above 90 (case- and diacritics-insensitive), the picker asks "Did you mean \<existing\>?". Enter accepts that client. Otherwise you can type a contact name, a company, and a one-line note; Enter on an empty field skips it. Contact and company are written to Snapshot and to `aliases`. The note is written to Snapshot only.

The same commands work from a terminal: `vault-ingest clients`, `clients add "<Name>" [--alias ..] [--contact ..] [--company ..] [--note ..]`, and `clients find "<query>"` (scores included).

**Recurring meetings.** The window title is turned into a key: a Google Meet code (`abc-defg-hij`), a Zoom meeting id (or the Zoom title when there is no id), or the Teams title. Confirming stores that key in `_System/meeting-map.json`. The next time that meeting appears, its client is already selected and the window says **Enter to confirm**. Nothing is saved until you press Enter.

You can change the choice until that call's processing starts. The pipeline then uses the session's client instead of asking the AI. If you change the client after the call was already filed, the next `run` or `watch` moves the call note, transcript parts, attachments, and raw file into the new client folder (the same layout as a normal ingest; nothing is deleted or overwritten). The old profile is left untouched. `_System/digest.md` gets an entry listing the profile lines that were added to the old client, so you can remove them by hand.

## Editing the prompts

The prompts are plain Markdown in `prompts/`. Edit them freely: the `## System` / `## User` headings and the `{{placeholders}}` must stay. Then use `reprocess` on a call to see the effect.

## Tests

```
.\.venv\Scripts\python -m pytest -q
```
# local-vault-ingest
