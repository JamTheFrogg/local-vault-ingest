# 04 - Addendum: decisions of 14.11.2026 17:47 and what they change

Status: plan only, no code written. This file updates `02-architecture.md` (written 17:45, before these
decisions) and `03-ux-shape.md`. Where the files disagree, this one wins.

"unverified" means not confirmed from a primary source or by a test.

---

## 1. Decisions

| # | Decision (Faris) | Changes |
|---|---|---|
| 9 | Design direction A, Oxide | `DESIGN-direction.md` section 3 is the design; section 4 (Night Line) is dropped. |
| 10 | Phone import works on every OS if possible; where not, file upload | Section 8 of `02-architecture.md` is replaced by section 2 below. |
| 11 | Auto-record is a setting per computer | One switch in Settings, stored on that computer. No per-microphone setting. |
| 12 | Call popup hidden from screen sharing by default, changeable in settings | Section 3 below. |
| 13 | The project is planned on its own terms | No outside deadline sets scope or order. |

<div style="page-break-before: always"></div>

## 2. Phone import on every OS

### Three sources, one screen

The Import screen keeps one list (name, date, length, state: new / imported / queued / skipped) and one
mass-import bar. What changes per OS is only where the list comes from. All three sources feed the same
`ImportLedger` and the same pipeline.

| Source | What it is | Where it works |
|---|---|---|
| A. Phone over USB | The phone's recordings folder read directly | See the table below |
| B. A folder | The user picks any folder: a phone mounted by the file manager, an SD card, a synced folder (Syncthing, iCloud Drive, Google Drive, OneDrive). Listed and marked exactly like source A. | Every OS |
| C. Upload files | Pick files or drag them into the app; they are copied into `00_Inbox`. | Every OS |

Source B is a reader with the same four methods as `ShellPhone` over a normal folder (about 40 lines, per
`02-architecture.md` section 8). Source C is `POST /inbox/import`, already in the engine API plan.

### Phone over USB, per OS and phone

| OS + phone | Route | State |
|---|---|---|
| Windows + Android | Existing code (`phone_mtp.ShellPhone`, Windows Shell and Windows Portable Devices) | Works today |
| Linux + Android | GNOME-family desktops mount the phone as a folder under `/run/user/<uid>/gvfs/` in a directory starting with `mtp:host=` (Arch wiki, MTP page). The app finds that folder by itself and reads it with source B's reader. | Expected to work; needs a test on a real Linux desktop. KDE does not mount there: the user picks a folder or uploads. |
| macOS + Android | macOS has no built-in MTP support. Route to try: the `mtp-rs` crate (pure Rust, MIT/Apache-2.0 per its docs.rs page) inside the Tauri shell, which is Rust already. | Unverified: nobody has run it here. If the test fails, macOS offers sources B and C. |
| Any OS + iPhone | No route found for reading Voice Memos over USB (unverified). | Sources B and C: share the recording from the phone to a synced folder or to the computer, then pick the folder or upload. |

Sources:
- https://wiki.archlinux.org/title/MTP (GVFS mount location)
- https://docs.rs/crate/mtp-rs/0.37.1 (pure-Rust MTP, licence, Windows backend)
- https://en.wikipedia.org/wiki/Media_Transfer_Protocol (macOS: PTP only, no MTP)

### What to test first

One day, before building the screen: a 50-line Rust program using `mtp-rs` that lists the recordings folder
of an Android phone. Run on a Mac and on Linux. If it works on both, the shell gets one USB route for macOS
and Linux, and the Linux GVFS lookup stays as the fallback. Windows keeps its existing tested code either way.

### Onboarding for the screen

Unchanged from `03-ux-shape.md`, with one addition: the first step asks "How do your recordings reach this
computer?" with the three sources, and preselects USB on Windows.

<div style="page-break-before: always"></div>

## 3. Popup hidden from screen sharing

Tauri exposes content protection for a window. One switch in Settings ("Hide the call popup from screen
sharing"), on by default.

| OS | What happens | Reliability |
|---|---|---|
| Windows 10 version 2004 and later | The window is left out of captures (Windows display affinity "exclude from capture"). | Reliable for screen sharing in Zoom, Teams, Meet, OBS and screenshots. |
| macOS up to 14 | The window's sharing type is set to "none". | Reliable. |
| macOS 15 and later | Same call, but apps that capture through ScreenCaptureKit can still see the window. | Not reliable. Settings says so next to the switch. |
| Linux | No equivalent. | Not available. Settings shows the switch as unavailable with one line of explanation. |

Consequence for the app's own participant screenshot: with protection on, the popup is not in the screenshot,
which is what the screenshot step wants.

Sources:
- https://www.github.com/tauri-apps/tauri/pull/5132 (Tauri content protection)
- https://www.producthunt.com/p/jobpath/how-apps-hide-windows-from-screen-capture-and-what-broke-in-macos-15
- Electron `setContentProtection` documentation (same OS calls, same macOS limit)

<div style="page-break-before: always"></div>

## 4. Where the three plans disagree

| Topic | `01-product-and-scope.md` | `02-architecture.md` | Needs |
|---|---|---|---|
| MCP server | Ship the third-party `obsidian-mcp` binary (MIT) | Write a small Python server inside the engine: five tools with today's names, keyword search | Faris's decision. Note: Faris's own vault rules use semantic search today; the Python server has keyword search until built-in semantic search arrives after 1.0. |
| Phone import off Windows | Windows + Android only, synced folder elsewhere | Windows only, file import elsewhere | Settled by decision 10 and section 2 above. |
| Popup position | - | Bottom-right as today | `03-ux-shape.md` moves it to the right edge, vertically centred. Follow the UX plan. |

## 5. Defaults taken from the plans unless Faris says otherwise

- Bundle identifier `io.github.jamthefrogg.vault-ingest`. Permanent: it names the settings folder.
- Default built-in model: Gemma 4 12B QAT if the phase 0 check passes on the 8 GB card, otherwise the lighter Gemma 4 E4B.
- Recordings kept as two 16 kHz WAV files (microphone, system audio), about 230 MB per meeting hour; compression after 1.0.
- Faris's install keeps its current recordings folder; new installs use `Music\vault-ingest`.
- Migration may disable (not delete) the old scheduled tasks, after asking once per task.
- Auto-record off by default.
- The graph shows the whole vault without `_System`, raw files and transcript parts.
- Version 1 does not change note text; it can change the client, reprocess and skip.
- In a mass phone import the AI may pick the client; unsure ones go to "Needs review".
- An automatic GPU pause is silent by default, with a setting.
- Engine additions for the UI: Mark moment, a progress feed, phone listing with multi-file import, "Run anyway", "Record again".
