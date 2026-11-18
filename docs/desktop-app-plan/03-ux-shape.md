# 03 - UX shape: vault-ingest desktop app

Planning document. No UI code. Nothing in `/home/claude/work/vault-ingest` was changed.

Companion files: `PRODUCT.md` (product context) and `DESIGN-direction.md` (visual directions).

## 0. Inputs, method, and what is assumed

**Read for this plan:** `BRIEF.md`; `README.md`, `SETUP.md`, `companion.toml`, `.env.example`;
`vault_ingest/picker.py`, `picker_ui.py`, `watcher_meetings.py`, `sessions.py`, `phone.py`, `gpu.py`,
`screenshots.py`, `schemas.py`, the note renderers in `pipeline.py`; `prompts/call_note.md`.

**Method:** the impeccable skill's `shape` playbook (discovery, direction, brief), with `operate`, `onboard`,
`init` and `new-work` as references. The skill's launcher was not run (the brief forbids it), so project context
was read directly from the files above. The surface mode is **Operate**: the user is in a task, familiar patterns
win, and the product's character lives in precise details.

**The playbook asks for an interview. Nobody could answer, so these are my recommended answers, all marked
assumed.** Each one that matters is also in section 10.

| Playbook question | Answer used in this plan |
|---|---|
| What is the surface for? | One window where the owner sees every meeting and its note, reads the vault, and controls recording. (from the brief) |
| Who reaches it, in what state? | A freelance developer/consultant on Windows 11, usually mid-task, often in or right after a call. Second audience: a technical stranger who downloads the open-source build. (assumed) |
| The one thing they must be able to do | Start a recording with one click, then trust that a correct note appears under the right client without further work. (assumed) |
| What is uniquely true here | Everything stays in a plain Markdown vault on the user's disk, and the same vault is served to his AI tools. The app never deletes and only writes in three folders. (from README) |
| Realistic data ranges (assumed) | Clients 0 / 5-30 / 200. Meetings 0 / 3-15 per week / about 2,000 after two years. Meeting length 5 min - 2 h (extreme 4 h). Transcript parts per meeting 1-40. Screenshots per meeting 0-20. Recordings on a phone 0-500. Files in the vault up to about 20,000 (transcript parts dominate). |
| Fidelity | A full flow for v1, specified to wireframe level. (from the task) |
| What must stay untouched | The vault layout and note formats; the engine's safety rules; the picker's decisions (client, new client, personal, unsorted, skip). (from the brief and README) |
| What would make it feel wrong | Taking the keyboard during a call; a second "press record again" step; anything that looks like the vault is locked inside the app. (assumed) |

**Words used in the UI (assumed):** the recorded event is a **meeting** everywhere in the UI (the owner's word).
The Markdown file is a **note**. The vault folder keeps its name `Calls/`. "Call" is used only in "call popup"
and "call audio".

**One correction to the task text:** it lists a `Sessions/` folder. In the code there is none. Session files are
JSON in `_System/meetings/` (`sessions.py`). The app reads them for start, end, screenshots and the picked
client. They are not notes and never appear in the graph.

**Engine additions this design needs.** These are small, but they do not exist today. All are unverified until
the engine plan confirms them:

1. `marks` on a session (offset + optional label) for "Mark moment".
2. A progress feed from the engine (stage, percent, step n of m, queue length, pause reason).
3. A list call for the phone folder (today only the newest file is looked at) and a multi-file import.
4. A temporary override for an automatic GPU pause ("Run anyway").
5. A "record again into the same meeting" path after an accidental Stop.

---

## 1. Usage scenes

**(a) A call starts while he is working.** His hands are on the keyboard and his attention is on the people in
the call. He needs to see, without reading, that the app noticed the call, and to start recording with one
click, or to see that it already started. Naming the client can wait until a quiet moment. Nothing may take the
keyboard, make a sound, or sit on the meeting's own buttons.

**(b) After the call, checking the note.** He has a couple of minutes before the next thing. First he needs to
know the recording is safe and roughly when the note will exist. When it is ready he checks three things: is it
under the right client, are the decisions and action items right, and what was actually said where a line looks
wrong. One click should take him from a claim to the audio or the transcript part behind it. Fixes are few:
change the client, reprocess, or open the note in Obsidian to edit it.

**(c) Monday morning, before a client meeting.** Five minutes, not in a hurry but not exploring either. He needs
"where are we with Acme and what do I owe them": the current status, the open items, the last meeting's
decisions, and sometimes one exact number or name. This is reading. He finds things by client first, by a
remembered word second.

**(d) First run on a fresh machine.** He, or a stranger from GitHub, has just clicked through an operating
system warning about an unsigned app. He needs a reason to trust it, a folder choice he understands, proof that
the app can hear both his microphone and the call, an honest number for the model download, and a first note
without opening a terminal or a config file.

**(e) Plugging in the phone.** He recorded an in-person meeting with the phone's voice recorder and plugs the
phone in. He needs the app to say whether it can read the phone, and what to tap on the phone if it cannot. Then
he needs to see what is new and what is already in the vault, pick several recordings, file them under the right
clients, and know that nothing on the phone is changed.

---

## 2. Information architecture

Four destinations, one utility, three things that are always present. This is smaller than the starting
proposal: Search is not a section, and Home is the Meetings list.

```
ALWAYS PRESENT
  Record control      top of the rail. Idle: [Record]. Call detected: [Record - Google Meet].
                      Recording: red, timer, [Stop].
  Search field        in the top bar. Opens the command palette (Ctrl/Cmd+K).
  Status area         bottom of the rail. One line of state + one line of detail.
                      Click opens the Activity drawer (queue, progress, recent events, pause controls).

DESTINATIONS (left rail)
  1 Meetings   (home) every meeting at every stage: recording, waiting, transcribing, writing notes,
                      ready, needs review. List on the left, meeting detail on the right.
  2 Clients           client list -> client page (profile, open items, meetings of that client).
                      "Personal" and "Unsorted" sit at the end of the list.
  3 Graph             the vault's notes and links. Filters, preview, open.
  4 Import            two tabs: Phone, Files.

UTILITY
  Settings            bottom of the rail and Ctrl/Cmd+,.

NOT SECTIONS
  Note reader         the detail view for any Markdown note. Meetings and client pages are the reader
                      plus a header built from frontmatter. Reached by clicking anything.
  Search results      a page reached from the palette ("Show all results"). Not in the rail.
  Activity drawer     slides over the right edge from the status area.

SEPARATE WINDOWS
  Call popup          small, always on top (section 4).
  Tray icon + menu    section 6.
  First-run setup     replaces the main window content until done (section 5).
```

What changed from the proposed starting point, and why:

| Proposed | Here | Reason |
|---|---|---|
| Home/Meetings | Meetings is home | A separate dashboard would repeat the list. Scene (b) and (c) both start from a meeting or a client. |
| Search as a section | Field in the top bar + palette + a results page | Search is an action from anywhere. A rail item would be an empty page most of the time. |
| Import (phone + files) | Kept, two tabs | Both are "bring a recording in". The file inbox (`00_Inbox`) is not shown as a folder: its files appear as waiting meetings. |
| Status area | Kept, plus an Activity drawer | Long jobs need one place that explains them. A fifth rail item would be too much for that. |
| (none) | Note reader as the shared detail view | Keeps one renderer for call notes, profiles, parts and any other note. |

Navigation model: history like a browser. Every click on a `[[link]]`, a list row or a graph node pushes a
history entry. Back and Forward work everywhere (Alt+Left / Alt+Right, Cmd+[ / Cmd+] on macOS, mouse buttons 4/5).

---

## 3. Screens

### Window sizes and collapse rules

| Width | Rail | Meetings / Clients | Detail side panel |
|---|---|---|---|
| 1200 px and up (design size 1280 x 800) | 208 px, labels | list 320 px + detail | 248 px column on the right |
| 900 - 1199 px | 56 px, icons with tooltips | list 280 px + detail | becomes tabs under the header |
| Minimum 900 x 600 | 56 px | list 280 px + detail 564 px | tabs |

The window cannot be made smaller than 900 x 600. The reader column never exceeds about 68 characters per
line, however wide the window is. Extra width goes to margins and the side panel.

Wireframe legend: `[Button]`  `[x]` checkbox  `( )` radio  `(o)` record dot  `>` current item  `v` menu
`...` more actions  `#` client colour swatch.

### 3.1 Meetings: list + meeting detail (1280 x 800)

```
+--------------------------------------------------------------------------------------------------+
| vault-ingest          [ Search notes or run a command                Ctrl+K ]           _  []  X |
+----------------+---------------------------+-----------------------------------------------------+
| [(o) Record  ] | Meetings              [v] | # Acme                                              |
|                | All  Working 2  Review 1  | Kickoff                    [Open in Obsidian] [...] |
| > Meetings     +---------------------------+ Tue 30 Sep 2026, 14:05-14:47 . 42 min . Google Meet |
|   Clients      | TODAY                     | Amra K., Faris, Emir (3 people) . notes: built-in   |
|   Graph        | (o) Recording   12:04     +-----------------------------------------------------+
|   Import       |   Google Meet . Acme      | [>] 07:12 |----|---|--o----|--^-----|----| 42:10 1x |
|                | (~) Transcribing    42%   |           p1   p2  p3 shot  p4 mark  p5   p6        |
|                |   Voice 002 . Unsorted    +-------------------------------+---------------------+
|                | YESTERDAY                 | Summary                       | ON THIS PAGE        |
|                | # Acme                42m |  Acme wants the booking flow  |  Summary            |
|                | > Kickoff                 |  live before the trade fair.  |  Key points         |
|                |   3 decisions . 5 actions |  We agreed to cut the admin   |  Decisions       3  |
|                | # Bilal               18m |  panel from the first release |  Action items    5  |
|                |   Wardrobe site           |  ...                          |  Open questions  2  |
|                |   (!) Needs review        |                               |  Notable details    |
|                | THIS WEEK                 | Decisions                     |  Screenshots     4  |
|                | # Wortox             1h05 |  - Launch moves to 15 Oct     |  Transcript      6  |
|                |   Pricing page            |    "let's say the fifteenth"  |                     |
|                |   1 decision . 2 actions  |  - Admin panel is phase two   | SCREENSHOTS         |
|                | ...                       |                               |  [img] [img] [img]  |
|                |                           | Action items                  |  [img]              |
+----------------+                           |  [ ] Send the staging link    |                     |
| (~) Transcribing                           |      Faris . by Friday        | LINKED FROM       2 |
|   1 waiting    |                           |  [ ] Share brand files        |  Acme (profile)     |
| [*] Settings   |                           |      Amra . this week         |  2026-10-07 Review  |
+----------------+---------------------------+-------------------------------+---------------------+
```

**List (left).** One row per meeting, newest first, grouped by day (Today, Yesterday, This week, then by
month). Each row: client swatch and name, title, duration, and one line of either counts ("3 decisions . 5
actions") or state. Rows for meetings that are not finished show their stage in place of the counts:

| Stage | Row shows | Row action |
|---|---|---|
| Recording | red dot, live timer | Stop |
| Saved, waiting | "Waiting" + reason if any ("a call is in progress", "paused") | Skip |
| Transcribing | percent | - |
| Writing notes | "Writing notes, step 2 of 4" | - |
| Ready | counts | open |
| Needs review | "(!) Needs review" in the error colour | open: shows why + fixes |
| Not ingested | "Skipped" in muted text (hidden unless the filter asks) | Ingest after all |

Filter chips above the list: All, Working (n), Review (n). The `[v]` menu adds: client, date range, has
screenshots, include skipped. Typing `/` filters the list by title or client.

**Detail (right).** Built from the call note file plus its session file.

- **Header.** Client (link to the client page), title, date and time, duration, app, participants, and which
  engine wrote it (`generated_by`, `transcribed_by`). `[Open in Obsidian]` is the one visible button. The
  `[...]` menu holds: Change client..., Reprocess notes, Reprocess from audio, Reveal in file manager, Copy
  link, Copy as Markdown. "Change client" and "Reprocess" are engine operations that already exist; they
  are not note editing.
- **Timeline strip.** The whole meeting drawn as one line scaled to its length. Tick marks show where each
  transcript part starts (p1..pn), where screenshots were taken, and where moments were marked. It is also the
  audio player: Play/Pause, current time, speed (1x, 1.25x, 1.5x, 2x). Click anywhere to seek. Clicking a part
  tick opens that transcript part at the same time. If the audio file is missing: "Audio not found at
  <path>" with [Locate...]; the strip still works for navigation.
- **Note body (centre).** The call note rendered by the note reader (3.4): Summary, Key Points, Decisions,
  Action Items, Open Questions, Notable Details, Screenshots, Transcript. Evidence quotes that the pipeline
  verified are shown under the decision or action they support, in muted text. Action item checkboxes are
  displayed, not clickable (v1 does not edit notes).
- **Side panel (right).** "On this page" (section list with counts, click to jump), Screenshots (thumbnails;
  click opens a lightbox with caption, the time into the meeting, "People on screen", and the text read from
  the image), Linked from (backlinks).
- **Transcript.** The "Transcript" section lists parts: "Part 3 . 14:02 - 21:40 . <context line>". Opening a
  part shows it in the reader with Previous / Next part. Timestamps in the transcript are clickable and seek
  the audio. A sticky mini player stays at the top while reading a part.

**Detail while the meeting is not ready.**

```
| # Unsorted                                          |
| Voice 002                                    [...]  |
| Today, 09:12 . 12 min . Phone recording             |
+-----------------------------------------------------+
| [>] 00:00 |-----------------------------| 12:04  1x |
+-----------------------------------------------------+
|  (~) Transcribing            42%  [=========      ] |
|      about 3 min left                               |
|  ( ) Writing notes                                  |
|  ( ) Filing under a client                          |
|                                                     |
|  Client: [ Unsorted                v ]  Title: [ Voice 002      ]|
|  You can change these until note writing starts.    |
+-----------------------------------------------------+
```

The audio can already be played. The three steps are the pipeline's real stages. Client and title stay
editable until the notes step starts (the engine allows that today).

**Needs review.** The detail shows the reason from the `.why.md` file in plain words at the top, then the
fixes that apply: [Choose client], [Try again], [Switch AI engine], [Open the file]. Example: "The file name
does not say which client this was with, and the AI was not sure enough to pick one."

**Minimum size (900 x 600).**

```
+------------------------------------------------------------------------+
| vault-ingest     [ Search or run a command      Ctrl+K ]       _ [] X  |
+----+----------------------+--------------------------------------------+
|(o) | Meetings         [v] | # Acme                                     |
|    | All  Working 2  Rev 1| Kickoff            [Open in Obsidian] [...]|
| >M +----------------------+ Tue 30 Sep, 14:05 . 42 min . Google Meet   |
|  C | TODAY                +--------------------------------------------+
|  G | (o) Recording  12:04 | [>] 07:12 |---|--|--o---|--^---|--| 42:10  |
|  I |   Meet . Acme        +--------------------------------------------+
|    | YESTERDAY            | Note | Transcript 6 | Screenshots 4 | Info |
|    | # Acme           42m +--------------------------------------------+
|    | > Kickoff            | Summary                                    |
|    |   3 dec . 5 actions  |  Acme wants the booking flow live before   |
|    | # Bilal          18m |  the trade fair. We agreed to cut ...      |
|    |   (!) Needs review   |                                            |
|----|                      | Decisions                                  |
|(~) |                      |  - Launch moves to 15 Oct                  |
|[*] |                      |  - Admin panel is phase two                |
+----+----------------------+--------------------------------------------+
```

The rail shows icons only (tooltips carry the labels). The side panel becomes four tabs. "Info" holds the
backlinks, participants and file paths.

### 3.2 Clients and the client page

```
+----------------+---------------------------+-----------------------------------------------------+
| [(o) Record  ] | Clients          [+ New]  | # Acme                     [Open in Obsidian] [...] |
|                | [ Filter clients        ] | active . last contact 30 Sep . Amra K. (contact)    |
|   Meetings     +---------------------------+ also known as: Acme d.o.o.                          |
| > Clients      | # Acme               3 !  +-----------------------------------------------------+
|   Graph        |   30 Sep . 12 meetings    | WHERE THINGS STAND                                  |
|   Import       | # Bilal              1 !  |  Kickoff done. Booking flow is the first release;   |
|                |   22 Oct . 4 meetings     |  launch moved to 15 Oct. Waiting on brand files.    |
|                | # Wortox             0    +-------------------------------+---------------------+
|                |   12 Nov . 31 meetings    | OPEN ITEMS                  3 | MEETINGS         12 |
|                | # Halcom             2 !  |  [ ] Send the staging link    |  o 30 Sep  Kickoff  |
|                |   ...                     |      Faris . by Friday        |  |   42m  3 dec     |
|                |                           |  [ ] Share brand files        |  o 23 Sep  Intro    |
|                | OTHER                     |      Amra . this week         |  |   18m            |
|                |   Personal        7       |  [ ] Decide on hosting  . ?   |  o 02 Sep  Scoping  |
|                |   Unsorted        2       |                               |  |   55m  1 dec     |
|                |                           | LAST MEETING . 30 Sep Kickoff |  ...                |
|                |                           |  Summary (first 3 lines) ...  |                     |
|                |                           |  [Open the note]              | NOTES             2 |
|                |                           +-------------------------------+  Work/Proposal v1   |
|                |                           | Snapshot                      |  Acme - Archive     |
|                |                           | Decisions log                 |                     |
|                |                           | Preferences & watch-outs      |                     |
|                |                           | Work log                      |                     |
+----------------+---------------------------+-------------------------------+---------------------+
```

**Client list.** Name, date of last meeting, number of meetings, number of open items (the `!` count).
Sort: last contact (default), name, open items. `[+ New]` runs the same steps as the picker: name, then the
near-duplicate question ("Did you mean Acme?"), then optional contact, company, one-line note.
"Personal" and "Unsorted" are real client folders in the vault, so they are listed, but apart.

**Client page.** Built for scene (c). Top to bottom:

1. Header from the profile's frontmatter and Snapshot: status, last contact, contact names, aliases.
2. **Where things stand**: the profile's "Current Status" section, shown first and in full.
3. **Open items**: from the profile, each with owner and due text. Display only.
4. **Last meeting**: first lines of its summary and a link.
5. The rest of the profile as collapsible sections (Snapshot, Decisions Log, Preferences & Watch-outs,
   Work Log), rendered by the note reader.
6. Right column: all meetings of the client as a vertical time list (date, title, duration, decision count),
   then other notes in the client folder (`Work/`, the archive file).

Unsorted has one extra action per meeting: [Choose client], which uses the engine's existing move.

### 3.3 Graph

```
+----------------+--------------------------------------------------------+------------------------+
| [(o) Record  ] | Graph    [ Find a note            ]  [Fit] [- 100% +]   | Acme                  |
|                +--------------------------------------------------------+ client profile         |
|   Meetings     | SHOW                                                    | 12 meetings . 3 open  |
|   Clients      | [x] Clients  [x] Meetings  [ ] Transcript parts         | 14 links in . 9 out   |
| > Graph        | [x] Other notes  [ ] Missing notes  [ ] Unlinked notes  |                       |
|   Import       | Clients: [ All                v ]  From [    ] To [   ] | Kickoff done. Booking |
|                +--------------------------------------------------------+ flow is the first ...  |
|                |                                                         |                       |
|                |            o        o                                   | [Open]                |
|                |             \      /           o                        | [Open in Obsidian]    |
|                |      o----( Acme )------o     /                         | [Show only its links] |
|                |             /      \         o--( Wortox )--o           |                       |
|                |            o        o       /       |    \              | LINKS                 |
|                |                            o        o     o             |  2026-09-30 Kickoff   |
|                |        ( Bilal )--o                                     |  2026-09-23 Intro     |
|                |           |                         <> Proposal v1      |  Work/Proposal v1     |
|                |           o                                             |  ...                  |
|                |                                                         |                       |
|                | 214 notes . 388 links                    [Show as list] |                       |
+----------------+--------------------------------------------------------+------------------------+
```

**What a node is.** One Markdown file in the vault. Nothing else. People, tags and screenshots are not nodes
in v1.

| Node | Drawn as | Label |
|---|---|---|
| Client profile (`type: client`) | large filled circle in the client's colour | always visible |
| Meeting note (`type: call`) | small filled circle in its client's colour | on hover, when selected, when zoomed in |
| Transcript part (`type: transcript-chunk`) | tiny dot, off by default | on hover |
| Other note | small diamond, neutral colour | on hover, when zoomed in |
| Missing note (a link to a file that does not exist) | hollow dashed circle, off by default | on hover |

Node size grows a little with the number of links. Shape and size carry the type, so colour is never the only
signal.

**What an edge is.** One resolved `[[wikilink]]` or embed from one note to another. One line per pair of
notes, however many times they link. Lines have no arrowheads at rest; when a node is selected, its outgoing
links are solid and its incoming links are dashed.

**Excluded always:** `_System/`, `Transcripts/raw/`, attachments, `.obsidian/`.

**Filters.** Type checkboxes; a client multi-select; a date range (uses the note's `date`); "Unlinked notes"
shows notes with no links. The Find field highlights matching nodes and dims the rest; Enter selects the first
match and centres it.

**Interaction.**

| Input | Result |
|---|---|
| Hover a node | Its label appears; the node and its direct neighbours stay bright, everything else dims. No panel change. |
| Click a node | Selects it. The right panel shows type, client, counts, the first lines, and its links as a list. |
| Double-click, or Enter on a selected node | Opens the note in the reader. |
| Click empty space, or Esc | Clears the selection. |
| Drag a node | Moves and pins it for this session. |
| Drag empty space / wheel | Pan / zoom toward the pointer. |
| Right-click a node | Open, Open in Obsidian, Show only its links, Copy link. |
| [Show only its links] | Local graph: the node, its neighbours, and their neighbours (depth 1-3 stepper). [Back to full graph] returns. |
| Arrow keys with a node selected | Move the selection to the nearest linked node in that direction. Tab moves through the node's links in the panel. |
| [Show as list] | The same notes as a sortable table (note, type, client, links in, links out). This is the non-visual route to everything the graph offers. |

The layout settles within about 1.5 seconds and then stops moving. Positions are remembered per vault so the
picture is the same next time.

Minimum size: the filter bar collapses into a [Filters] button, and the right panel becomes a bottom sheet
that appears only when a node is selected.

### 3.4 Note reader

```
+----------------+----------------------------------------------------------+----------------------+
| [(o) Record  ] | <  >   Clients / Acme / Calls / 2026-09-30 Kickoff       | ON THIS PAGE         |
|                |                          [Find] [Open in Obsidian] [...] |  Summary             |
|   Meetings     +----------------------------------------------------------+  Key points          |
|   Clients      | v Properties                                             |  Decisions           |
|   Graph        |   type call . client Acme . date 2026-09-30 . chunks 6   |  ...                 |
|   Import       |   generated_by built-in/gemma-12b                        |                      |
|                |                                                          | LINKED FROM        2 |
|                | # 2026-09-30 Kickoff                                     |  Acme                |
|                |                                                          |   "... agreed in     |
|                | Client: [[Acme]] . Date: 2026-09-30 14:05                |   [[2026-09-30       |
|                |                                                          |   Kickoff]] ..."     |
|                | ## Summary                                               |  2026-10-07 Review   |
|                | Acme wants the booking flow live before the trade fair.  |   "... follow-up to  |
|                | ...                                                      |   ..."               |
|                |                                                          |                      |
|                | ## Screenshots                                           | LINKS OUT          8 |
|                | - [00:14:02 into the call] Pricing slide                 |  Acme                |
|                |   [ image ]                                              |  Part 1 ... Part 6   |
|                |   > Text in image                          (collapsed)   |  (!) Old proposal    |
|                |                                                          |      does not exist  |
+----------------+----------------------------------------------------------+----------------------+
```

- **Renders** what the engine writes and what a normal Obsidian vault contains: headings, lists, task lists
  (display only), tables, quotes, code, callouts including the collapsed form the pipeline uses for "Text in
  image" (`> [!note]-`), image embeds (`![[...]]`), and frontmatter as a collapsible "Properties" block.
- **Links.** `[[Note]]`, `[[path/Note|alias]]` and links to headings open inside the app and add a history
  entry. Hovering a link for half a second shows a small preview (title and first lines). Web links open in the
  system browser. A link to a note that does not exist is shown with a dashed underline and the tooltip "This
  note does not exist"; it does nothing when clicked, because v1 creates no notes.
- **Backlinks** ("Linked from"): every note that links here, with the sentence around the link. Click opens it.
- **Open in Obsidian.** Opens the same file in Obsidian through its URL scheme (`obsidian://open?path=<absolute
  path>`; Obsidian finds the vault that contains the path - source: https://obsidian.md/help/uri). The button
  needs Obsidian installed and the folder opened in Obsidian at least once. What Obsidian does when it does not
  know the vault is not documented (unverified), so: if the URL cannot be handled, the button's menu offers
  "Open in the default editor" and "Reveal in file manager", and Settings shows a one-line hint.
- **Live reload.** When the file changes on disk (an edit in Obsidian, a reprocess), the reader reloads and
  keeps the scroll position. A quiet "Updated" note appears next to the title for a few seconds.
- **Find in note:** Ctrl/Cmd+F, with next/previous.
- **Not found.** If a note was moved or deleted outside the app: "This note is no longer at <path>." with
  [Search for it] and [Back].
- **Read-only is said once.** The `[...]` menu has "Edit in Obsidian" and "Edit in the default editor". There
  is no disabled edit button and no lock icon.

Minimum size: the right column folds into a [Links] button in the toolbar.

### 3.5 Search

Search lives in the command palette. The field in the top bar is the same thing.

```
              +--------------------------------------------------------------+
              | [ staging link                                             ] |
              +--------------------------------------------------------------+
              | MEETINGS                                                     |
              | > # Acme . Kickoff . 30 Sep         "...send the staging..." |
              |   # Acme . Review . 07 Oct          "...staging link still.."|
              | TRANSCRIPT                                                   |
              |   # Acme . Kickoff . Part 4 . 31:10 "I'll send you the sta.."|
              | CLIENTS                                                      |
              |   (none)                                                     |
              | COMMANDS                                                     |
              |   Copy link to this note                                     |
              +--------------------------------------------------------------+
              | Enter open . Ctrl+Enter open in Obsidian . Tab show all 3    |
              +--------------------------------------------------------------+
```

- Empty field: recent notes (5), then the commands that fit the moment (for example "Stop recording" while
  recording).
- Typing searches note titles, client names and aliases, participants, and note text. Matching ignores case
  and diacritics the same way the client picker does (c/č/ć, s/š, z/ž, dj/đ). At most 5 results per group.
- A transcript result opens the part scrolled to the match, with the audio set to that time.
- Prefixes: `>` commands only, `@` clients only.
- **Show all results** (Tab, or the last row) opens the results page: one list with snippets, and filters for
  client, type (meeting, transcript, profile, other) and date. Same row design as the Meetings list.
- **Search by meaning** (embeddings) is shown as a toggle on the results page only if the engine provides an
  embedding index (unverified; depends on the AI plan). Without it there is no toggle and no mention.
- No results: "Nothing matches 'stagging link'." plus [Search transcripts too] if that group was filtered out,
  and [Clear filters] on the results page.

### 3.6 Import: Phone

Phone import over USB exists today only on Windows (it uses Windows' portable-device interface; see
`phone_mtp.py` and the README table). On macOS and Linux this tab says so in one sentence and points to the
Files tab. That is a fact about the engine, not a design choice.

**Step view (shown until a phone has been read once, and again whenever the phone cannot be read).**

```
+----------------+---------------------------------------------------------------------------------+
|                | Import      [ Phone ]  Files                                                    |
|                +---------------------------------------------------------------------------------+
|                | Bring recordings in from your phone                                             |
|                | Nothing on the phone is changed, moved or deleted.                              |
|                |                                                                                 |
|                |  (v) 1  Plug the phone in with a USB cable that carries data                    |
|                |         Found: Galaxy A33                                                       |
|                |                                                                                 |
|                |  (>) 2  Unlock the phone                                                        |
|                |                                                                                 |
|                |  ( ) 3  On the phone, tap the USB notification and choose "File transfer"       |
|                |         It is usually set to "Charging only". The name differs by brand:        |
|                |         "File transfer", "Transferring files", "MTP".                           |
|                |         [ small drawing of the phone notification ]                             |
|                |                                                                                 |
|                |  ( ) 4  Choose the folder with your recordings                                  |
|                |         Suggested: Internal storage / Recordings / Voice Recorder   [Change...] |
|                |                                                                                 |
|                |  Waiting for the phone to allow file access ...        [My phone is not listed] |
+----------------+---------------------------------------------------------------------------------+
```

The steps tick themselves as the app sees each one happen (cable seen, storage readable, folder found). The
current step is marked; nothing has to be clicked to move on. Step 4 offers a folder browser of the phone's
storage, with common recorder folders suggested first. "My phone is not listed" opens short help: try another
cable or port, Android only, iPhone recordings are not reachable this way (unverified; iPhones do not expose
voice memos as files over USB, as far as I know).

**List view.**

```
| Import      [ Phone ]  Files                              Galaxy A33 . connected    [Refresh]    |
+--------------------------------------------------------------------------------------------------+
| [ Not imported 14 ]  Imported 61   Skipped 3   All 78           [ Filter by name or date       ]|
+--------------------------------------------------------------------------------------------------+
| [x] Recording                 Recorded          Length   Size    Client               State      |
| [x] Voice 014.m4a             Today 09:12       12:04    11 MB   [ AI decides      v ] New       |
| [x] Voice 013.m4a             Yesterday 16:40   48:31    44 MB   [ Bilal           v ] New       |
| [ ] Voice 012.m4a             12 Nov 10:02      03:10     3 MB   [ AI decides      v ] New       |
| [ ] Meeting Halcom.m4a        11 Nov 13:00    1:02:44    57 MB   [ Halcom          v ] New       |
| [-] Voice 011.m4a             now               ...      ...     still being recorded            |
| ...                                                                                              |
+--------------------------------------------------------------------------------------------------+
| 2 selected . 55 MB       File under: [ as set per row v ]   [Not meetings]   [Import 2]          |
+--------------------------------------------------------------------------------------------------+
```

- **State per recording.** New (never seen), Imported (links to the meeting), Waiting / Transcribing / Writing
  notes (already queued), Skipped ("not a meeting"), Still being recorded (cannot be selected), Failed (with
  the reason). The default tab is "Not imported", so the first thing shown is what needs a decision.
- **How "imported" is known.** The engine keeps a ledger keyed by the file's SHA-256 hash, with a fast match on
  path + size + time for files it has seen. A recording that is new by the fast match is hashed when it is
  copied; if its hash is already in the vault it becomes "Already in the vault" and is not imported twice.
- **Selection.** Click a checkbox; Shift+click selects a range; Ctrl/Cmd+A selects all in the current tab;
  Space toggles the focused row. The header checkbox selects all visible.
- **Client per row.** Prefilled when the recorder's file name starts with a client name or alias. Otherwise
  "AI decides": the engine reads the start of the recording and picks an existing client only when it is sure
  enough (today 75%); the unsure ones land in Meetings as "Needs review" with [Choose client]. The drop-down is
  the picker's list: clients, New client, Personal, Unsorted.
- **Bulk bar** (appears when anything is selected). "File under" sets one client for all selected rows, or
  leaves them as set per row. [Not meetings] marks the selection as skipped so it stops being offered. [Import
  n] starts.
- Title: taken from the recorder's file name. It can be changed in the meeting afterwards, until note writing
  starts.

**Progress and errors.**

```
| Importing 12 recordings     Copying 3 of 12 . 148 of 610 MB  [====      ]  [Stop after this one] |
+--------------------------------------------------------------------------------------------------+
| (v) Voice 014.m4a             Today 09:12       12:04    Unsorted -> AI decides   Waiting        |
| (v) Voice 013.m4a             Yesterday 16:40   48:31    Bilal                    Waiting        |
| (~) Meeting Halcom.m4a        11 Nov 13:00    1:02:44    Halcom                   Copying 34%    |
| ( ) Voice 010.m4a             ...                                                 Not copied yet |
| (!) Voice 009.m4a             09 Nov 08:15      00:42    -                        Could not read |
|     The phone stopped answering while this file was copied.       [Try again]                    |
| (=) Voice 008.m4a             08 Nov 17:20      22:10    Acme                   Already in vault |
+--------------------------------------------------------------------------------------------------+
```

- Copying is one file at a time (a phone over USB is slow and fragile). Copied files join the normal queue and
  show the same stages as any meeting. The list stays usable during the import.
- **Phone unplugged or locked mid-import:** the banner turns to the warning colour: "The phone disconnected.
  3 recordings were copied and are safe. Plug it in and unlock it to copy the other 9." [Continue] appears when
  the phone is readable again. Nothing is copied twice.
- **Other errors, each in the row:** phone locked ("Unlock the phone"), file removed on the phone, not enough
  disk space (says how much is needed), file type not supported (lists the supported ones).
- When everything is copied: "12 recordings copied. Notes will appear in Meetings as they are written."
  [Go to Meetings].

**The "newest recording is not imported" popup.** The existing check stays: when the phone becomes readable,
the engine looks at the newest recording. If it is not in the vault, the small popup window (section 4) shows:

```
+------------------------------------------------------+
| PHONE . Galaxy A33                                 x |
| Voice 014.m4a . today 09:12 . 12 min                 |
| Not in your vault yet. 13 older ones are new too.    |
| [Import...]     [Not a meeting]     Later            |
+------------------------------------------------------+
```

[Import...] opens the main window on Import > Phone with that recording ticked and focused. [Not a meeting]
marks only that file as skipped. "Later" or closing does nothing and asks again on the next connect (today's
behaviour). The popup does not take the keyboard. If the newest recording is already imported, a quiet
notification says so, as today.

### 3.7 Import: Files

```
| Import      Phone  [ Files ]                                                                     |
+--------------------------------------------------------------------------------------------------+
|  +--------------------------------------------------------------------------------------------+  |
|  |  Drop recordings or transcripts here, or [Choose files...]                                 |  |
|  |  Audio and video: wav mp3 m4a mp4 mkv webm ogg flac     Text: txt md vtt srt docx          |  |
|  +--------------------------------------------------------------------------------------------+  |
|                                                                                                  |
|  READY TO ADD                                                                                    |
|  File                               Client                  Date           Title                 |
|  zoom_recording_2026-09-28.mp4      [ AI decides        v ] [ 2026-09-28 ] [ (AI writes one)   ] |
|  Acme - 2026-09-30 - Kickoff.txt    [ Acme              v ] [ 2026-09-30 ] [ Kickoff           ] |
|                                                                      [Remove]   [Add 2 to queue] |
+--------------------------------------------------------------------------------------------------+
```

Client, date and title are read from the file name when it follows `<Client> - YYYY-MM-DD - <Title>`; otherwise
the date comes from the name if it has one, and the client is "AI decides". Files are copied into `00_Inbox`;
the originals stay where they were. After [Add], the rows leave this screen and show up in Meetings as Waiting.
Dropping files anywhere on the main window opens this tab with them listed.

### 3.8 Settings

Left sub-navigation, content on the right. Changes apply at once; there is no Save button. Anything that needs
a restart of the engine says so next to the control and offers [Restart now].

```
+----------------+--------------------+------------------------------------------------------------+
|                | Settings           | Recording                                                  |
|                |                    |                                                            |
|                |   General          | MICROPHONE                                                 |
|                | > Recording        |  [ Headset (Jabra Evolve)                             v ]  |
|                |   AI engine        |  You   [||||||||        ]  speaking level is fine          |
|                |   GPU priority     |                                                            |
|                |   Shortcuts        | CALL AUDIO (what the others say)                           |
|                |   Screenshots      |  [x] Record the computer's sound                           |
|                |   Connect AI tools |  Call  [||             ]  play something to test           |
|                |   Updates & about  |                                                            |
|                |   Advanced         | WHEN A CALL IS DETECTED                 this computer only |
|                |                    |  ( ) Show the popup; I press Record                        |
|                |                    |  (o) Start recording by itself                             |
|                |                    |      Tell the people you record. The law on this differs   |
|                |                    |      by country.                                           |
|                |                    |                                                            |
|                |                    |  App                Detect    Record by itself             |
|                |                    |  Google Meet         [x]         [x]                       |
|                |                    |  Zoom                [x]         [x]                       |
|                |                    |  Microsoft Teams     [x]         [x]                       |
|                |                    |  WhatsApp            [x]         [ ]                       |
|                |                    |  Viber               [x]         [ ]                       |
|                |                    |  ... 6 more                                                |
|                |                    |  Any other app that uses the microphone  [x]   [ ]         |
|                |                    |                                                            |
|                |                    | THE POPUP                                                  |
|                |                    |  [x] Show the call popup                                   |
|                |                    |  [x] Hide it from screen sharing where the system allows   |
|                |                    |  Position [ Right edge, middle v ]   [Reset position]      |
+----------------+--------------------+------------------------------------------------------------+
```

| Section | Contents |
|---|---|
| **General** | Vault folder (path, [Change...], [Open folder]; changing it explains what moves and what does not). Your name in transcripts (`ME_NAME`). Language of notes (same as the meeting / a named language). Language spoken in meetings (Whisper language). [x] Start at login. [x] Start hidden in the tray. [x] Keep running in the tray when the window is closed. Notifications: note ready [x], needs review [x], paused/resumed [ ]. |
| **Recording** | As drawn: microphone with a live meter; call audio with a live meter; auto-record choice for this computer and per app; popup options; "A call counts after the microphone is held for [15] s"; "A call ends [60] s after it was last seen". Where recordings are kept, with the total size and [Open folder]. |
| **AI engine** | See below. |
| **GPU priority** | See below. |
| **Shortcuts** | See section 7. Each row: action, current keys, [Change], [Clear]. Conflicts are reported when the keys are pressed. |
| **Screenshots** | Shortcut (same row as in Shortcuts). [x] Capture the meeting window when recording starts, to read who is in the meeting. [x] Once more after [2] min for late joiners. [x] Also pick up snips I take with the system tool during a meeting (folder: path, [Change...]). |
| **Connect AI tools** | See below. |
| **Updates & about** | Version, [Check for updates]. v1 builds are unsigned, so an update is shown as "Version 1.3 is out" with the release notes and [Open the download page]; it is not installed silently. Licence (MIT), third-party licences (fonts, icons, models, Meetily code if reused), link to the repository. |
| **Advanced** | [Run a health check] (the same checks as `vault-ingest check`, shown as a list of ticks and problems). Prompts folder [Open]. Part size (min / max / overlap words). Profile archive threshold. Client confidence. Logs [Open folder]. [Copy diagnostic info]. |

**AI engine.**

```
| AI engine                                                                                        |
|                                                                                                  |
| WHO WRITES THE NOTES                                                                             |
|  (o) Built-in       Runs on this computer. Nothing leaves it.                                    |
|  ( ) LM Studio      A model you already run in LM Studio.            address [ localhost:1234 ]  |
|  ( ) Ollama         A model you already run in Ollama.               address [ localhost:11434]  |
|  ( ) Claude API     Best notes. Meeting text is sent to Anthropic.   key [ ************ ] [Test] |
|                                                                                                  |
| MODELS ON THIS COMPUTER                            This PC: NVIDIA, 8 GB video memory, 32 GB RAM |
|  Speech to text   Whisper large-v3        3.1 GB   (v) Installed              [Remove]           |
|                   Whisper small           0.5 GB   faster, less accurate      [Download]         |
|  Notes            Gemma 12B               7.9 GB   (~) 2.4 of 7.9 GB . 6 min  [Pause]            |
|                   A smaller model         2.5 GB   for PCs without a GPU      [Download]         |
|  Search by meaning  bge-m3                1.2 GB   optional                   [Download]         |
|                                                                                                  |
|  Models are kept in  C:\Users\...\vault-ingest\models   12.2 GB used . 184 GB free   [Change...] |
|                                                                                                  |
| WHEN THE ENGINE CHANGES                                                                          |
|  Notes already written stay as they are. Each note records which engine wrote it.                |
|  [Rewrite the notes that were made on this computer ...]   (asks first, shows how many)          |
```

The model names and sizes in this wireframe are **placeholders for layout (unverified)**. The real list, sizes
and the recommendation for the detected hardware come from the engine plan. What the screen must always show
for each model: what it is for, download size, state, and one action. "Rewrite the notes" is the existing
`reprocess --all-local`.

**GPU priority.**

```
| GPU priority                                                                                     |
|                                                                                                  |
| NOW      Free. 2.1 of 8 GB in use. Nothing of ours is loaded.            [Pause AI work  v]      |
|                                                                                                  |
| THESE COME BEFORE VAULT-INGEST                                                                   |
| When one of them is true, vault-ingest stops its AI work, frees the GPU, and continues later.    |
| Recording is never paused.                                                                       |
|                                                                                                  |
|  [x] Games                    anything started from Steam, Epic, GOG or Xbox folders             |
|  [x] Full-screen apps         that use the GPU       Never count these: Chrome, Zoom, ... [Edit] |
|  [x] Apps I name              Project Zomboid  x    DaVinci Resolve  x      [+ Add an app]       |
|  [x] Anything else using more than [ 3000 ] MB of video memory                                   |
|  [x] A meeting in progress    new AI work waits until the call has ended                         |
|                                                                                                  |
|  Continue [ 60 ] seconds after the GPU is free again.                                            |
|                                                                                                  |
|  [What matches right now?]   -> "Nothing. AI work may run."                                      |
```

- This screen is the same rule set as `GPU_BUSY_RULES` in `companion.toml`, with the game rule made one of
  several. [+ Add an app] lists running programs to pick from, or takes a path. Because the brief says "not only
  for games", the wording is "these come before vault-ingest", not "game mode".
- `[Pause AI work v]`: For 1 hour / Until I resume. While paused the button reads [Resume]. A manual pause is
  never lifted by the timer (today's rule).
- "What matches right now?" runs the rules once and names the match, so a rule can be tested without waiting.

**Connect AI tools.**

```
| Connect AI tools                                                                                 |
| Your AI tools can search and read this vault through a small local server.                       |
|                                                                                                  |
| VAULT SERVER     (v) Running on this computer only, 127.0.0.1:37842             [Stop] [Test]    |
|                                                                                                  |
|  Claude Desktop   (v) Connected                                    [Disconnect]                  |
|  Claude Code      (>) Connected. Restart Claude Code to finish.                                  |
|  Cursor           ( ) Not connected                                [Connect]  [Copy the config]  |
|  Codex            (-) Not found on this computer                   [Copy the config]             |
|                                                                                                  |
| TELL EACH TOOL TO USE THE VAULT (optional, once)                                                 |
|  "For anything about my clients, read the vault first ..."         [Copy]                        |
|  Where to paste it:  Claude Desktop > Settings > personal preferences . Cursor > Rules > User    |
|  Rules . Claude Code: ~/.claude/CLAUDE.md [Add it] . Codex: ~/.codex/AGENTS.md [Add it]          |
```

State per tool:

| State | Meaning | Action |
|---|---|---|
| Not found | The tool's config folder does not exist | [Copy the config] for manual use |
| Not connected | Tool found, no entry for this vault | [Connect] |
| Connected, restart needed | Entry written; the tool reads its config only at start (Claude Desktop must be fully quit) | text only |
| Connected | Entry present and the server answered a test call | [Disconnect] |
| Problem | Entry present but points somewhere else, or the server is not running | [Fix], with the reason |

[Connect] never replaces a tool's config file. It shows the exact lines it will add and the file path
("This adds 6 lines to C:\Users\...\.cursor\mcp.json"), makes a backup copy next to the file, adds only its own
entry, and reports the result. [Disconnect] removes only that entry. The file locations per tool are the ones
in the current `SETUP.md` (step 5). Which server program runs behind this screen is the engine plan's decision;
the screen only needs its address and state.

Minimum size for Settings: the sub-navigation becomes a drop-down at the top of the content.

### 3.9 Activity drawer

Opened from the status area. 360 px, over the right edge, closes with Esc or a click outside.

```
+--------------------------------------+
| Activity                           x |
| (~) Transcribing  Voice 002    42%   |
|     [=========            ] 3 min    |
|                       [Pause AI work v]|
| WAITING                            2 |
|  # Acme . Kickoff                    |
|  Voice 013 . AI decides              |
| DOWNLOADS                            |
|  Gemma 12B   2.4 of 7.9 GB  [Pause]  |
| EARLIER TODAY                        |
|  09:40  Note ready: Bilal - Wardrobe |
|  09:12  Paused: Project Zomboid      |
|  09:31  Resumed (2 waiting)          |
+--------------------------------------+
```

"Earlier today" shows the same events the engine writes to `_System/digest.md`.

---

## 4. The call popup

A separate small window that floats above other windows. It replaces the tkinter picker and keeps every
decision the picker offers today.

**Two shapes.** A **bar** (360 x 64) is the resting shape in every state. A **card** (360 x about 440) is the bar
plus the client picker; it opens only when the user asks for it.

### States

**1. Call detected, auto-record off.** One click on Record starts recording.

```
+------------------------------------------------------+
| Google Meet . abc-defg-hij                         x |
| [(o) Record]         # Acme  v          Not this one |
+------------------------------------------------------+
```

- The client chip shows the remembered client for a recurring meeting (the meeting map), or "Client?" when
  there is none.
- [Record] starts recording at once. If a client is shown, it is saved with the click, as the Record button
  does today. If none is shown, recording still starts; the client can be chosen any time until notes are
  written.
- "Not this one" is the picker's "Don't ingest this meeting": nothing from this meeting is processed.
- `x` hides the popup and decides nothing.

**2. Call detected, auto-record on.** Already recording. The popup only asks for client and title.

```
+------------------------------------------------------+
| (o) 00:08   You [||||   ]  Call [||     ]          x |
| Who is this with?   # Acme  v     Title (optional)   |
+------------------------------------------------------+
```

If the client is already known for this recurring meeting, the question line is not shown and the popup goes
straight to state 3.

**3. Recording.**

```
+------------------------------------------------------+
| (o) 12:04   You [||||   ]  Call [|||||| ]   # Acme v |
| [Stop]          [Screenshot v]       [Mark]        x |
+------------------------------------------------------+
```

- **Timer** counts up. **Two level meters**: "You" (microphone) and "Call" (the computer's sound). If "Call"
  stays flat for 30 seconds while "You" moves, the label changes to "Call: no sound?" in the warning colour.
  That catches a wrong output device while it can still be fixed.
- **[Stop]** ends the recording at once. No confirmation, because a dialog during a call is worse than a
  mistake that can be undone: state 5 offers [Record again] while the call is still detected.
- **[Screenshot]** takes a region snip (the same as the screenshot shortcut). Its `v` offers "Whole screen
  (who is in the meeting)", which is today's participant capture. The popup hides itself for the capture, as
  the current code does.
- **[Mark]** stores the current time as a marked moment. The button shows "Marked 12:04" for two seconds. A
  label can be added later on the meeting's timeline.
- The client chip opens the card (state 4).
- After 10 seconds without the pointer over it, the second line folds away and the popup is the 360 x 40 top
  line only. Moving the pointer over it brings the buttons back.

**4. Picker card** (opened from the client chip, the shortcut, or the tray).

```
+------------------------------------------------------+
| (o) 12:04   You [||||   ]  Call [|||||| ]            |
+------------------------------------------------------+
| Who is this call with?                               |
| [ ac                                               ] |
|  > Acme                                              |
|    Accounting Plus   (alias: ACP)                    |
|    Wortox            (contact: Jaca)                 |
|    + New client: ac                                  |
|    Personal call: ac                                 |
|    Not a client call (Unsorted)                      |
|    Don't ingest this meeting                         |
| Title [ Kickoff                                    ] |
| Up/Down choose . Enter confirm . Esc later           |
+------------------------------------------------------+
```

Same rules as `picker.py`: top 8 matches by name, alias and contact; diacritics ignored; "New client" asks
for the name, then the near-duplicate question, then contact, company and a one-line note, each skippable with
Enter; "Personal call" asks who it is with; a remembered client is preselected with "Enter to confirm". Esc in
a sub-step goes back to the list; Esc on the list closes the card and decides nothing. Enter closes the card
and returns to the bar. The card is the only popup state that holds the keyboard, and only because the user
opened it.

**5. Call ended.**

```
+------------------------------------------------------+
| Saved . 42 min . # Acme . Kickoff                  x |
| (~) Transcribing 12%                  [Open meeting] |
+------------------------------------------------------+
```

- Second line follows the real stage: "Waiting: Project Zomboid is running", "Transcribing 12%", "Writing
  notes", "Note ready".
- If no client was chosen: the first line reads "Saved . 42 min . Client?" and the chip opens the card. If it is
  left alone, the engine decides the client the way it does today (file name, then AI).
- If the call is still detected after a manual Stop: [Record again] is shown for 60 seconds.
- The popup closes itself 20 seconds after the call ended, or stays until the pointer leaves if it is hovered.
- A recording made with no call detected (the Record control in the main window) uses the same states.

**6. Problem while recording.** The bar's first line turns to the warning colour and says what is still being
captured: "Microphone lost. Still recording the call." / "Disk almost full: 4 minutes left." A recording that
was cut off by a crash is offered on the next start: "A recording from 14:05 (12 min) was recovered." with
[Keep] (unverified; needs the recorder to write to disk as it goes).

### What the popup must never do

1. **Never take the keyboard on its own.** Not when it appears, not when its state changes, not when the call
   ends. It gets keyboard focus only from a click inside it, the popup shortcut, or the tray. (Tauri windows
   can be created unfocused and non-focusable - fields `focus` and `focusable`, source:
   https://docs.rs/tauri-utils/2.9.2/tauri_utils/config/struct.WindowConfig.html . Whether that holds on every
   Linux window manager is unverified.)
2. **Never cover the meeting's own controls.** Default position: right edge of the screen, vertically centred,
   16 px in. Zoom and Google Meet keep their call buttons along the bottom and Teams along the top; today's
   bottom-right corner sits on Meet's chat and people buttons (these layouts are from my own knowledge of the
   apps, unverified; check against the current versions). The user can drag it; the position is remembered
   per screen arrangement. If there is a second screen and the engine knows which screen the meeting window is
   on, the popup prefers the other one (unverified: needs the window position from the engine).
3. **Never make a sound**, and never flash the taskbar.
4. **Never block.** No confirmation dialogs. Recording never waits for a client.
5. **Never end or lose a recording because it was closed.** `x` and Esc only hide it.
6. **Never appear in its own screenshots**, and by default never appear in a screen share. Windows can leave
   a window out of screen capture completely, on Windows 10 version 2004 and later; on macOS 15 the equivalent
   switch no longer hides a window from apps that use the newer capture system, so Zoom and Teams still see it
   (source: https://www.producthunt.com/p/jobpath/how-apps-hide-windows-from-screen-capture-and-what-broke-in-macos-15 ;
   Tauri exposes this as content protection for Windows and macOS, source:
   https://www.github.com/tauri-apps/tauri/pull/5132 ; Linux: unverified, assume not available). Where it cannot
   be hidden, Settings says so.
7. **Never grow or move while the pointer is over it.**
8. **Never show while a rule says the microphone use is game voice chat** (existing `skip_while_gaming`).

### How it is reopened

- **Tray icon.** Click (Windows, macOS) or the menu item "Show call popup" (all systems; on Linux the tray
  click is not delivered to apps, source: https://docs.rs/crate/tray-icon/0.24.0/source/src/lib.rs ). It opens
  for the call in progress, or for the one that ended in the last 30 minutes (today's rule).
- **Shortcut.** Ctrl+Alt+C (today's key), which also gives it the keyboard and opens the card.
- **Main window.** Clicking the red Record control in the rail.
- **Notification.** The "meeting detected" notification, when notifications are on.

On Linux under Wayland, always-on-top placement and global shortcuts cannot be relied on (global shortcuts:
not supported by Tauri's plugin there as of September 2025, source:
https://github.com/tauri-apps/tauri/discussions/10017 ; always-on-top: unverified). The fallback is a system
notification with a Record action, plus the tray menu.

---

## 5. First run and empty states

### First-run setup

Three required steps and one optional. A step counter ("2 of 3") and a Back link on every step. The app is
usable after step 3 even while models are still downloading: recording works without them, and notes are
written as soon as the model is there.

**Step 1 - Notes folder.**

```
+----------------------------------------------------------------------------------+
| vault-ingest                                                            1 of 3   |
|                                                                                  |
| Where should your notes live?                                                    |
| vault-ingest writes plain Markdown files into a folder you choose. Obsidian or   |
| any editor can open the same folder.                                             |
|                                                                                  |
|  (o) Create a new folder       [ C:\Users\Faris\Documents\Vault        ] [Change]|
|  ( ) Use a folder I already have (for example an Obsidian vault)        [Choose] |
|                                                                                  |
|  It adds three folders: 00_Inbox, Clients, _System. It writes only inside them,  |
|  and it never deletes anything.                                                  |
|                                                                        [Continue]|
+----------------------------------------------------------------------------------+
```

Choosing an existing folder that already has `Clients/` shows "Found 14 clients and 212 meetings. They will
appear as soon as setup is done."

**Step 2 - Sound check.** The proof that recording will work, and the place where permissions are asked.

```
| Can vault-ingest hear the call?                                         2 of 3   |
|                                                                                  |
|  You (microphone)      [ Headset (Jabra Evolve)              v ]                 |
|                        [||||||||          ]   (v) Heard you                      |
|                                                                                  |
|  The call (computer sound)                                                       |
|                        [                  ]   Play any sound to test  [Play a tone]|
|                                                                                  |
|  [ what is missing, with one button, appears here ]                              |
|                                                              [Skip]   [Continue] |
```

What appears in the "what is missing" line, per system:

| System | Microphone | Call audio | Other |
|---|---|---|---|
| Windows | If the meter stays flat: "Windows is blocking the microphone for desktop apps." [Open the Windows setting] (Privacy & security > Microphone; the same page the README names). | No permission is asked for; the system's loopback capture is used (source: https://www.recall.ai/blog/how-to-get-access-to-system-audio ). If silent: "Check which speaker or headset the call plays on." | - |
| macOS | The system's own microphone prompt appears when the meter starts. If it was denied: [Open System Settings]. | Needs a system permission. Which one depends on the capture method the engine uses: screen-recording permission from macOS 13, or the audio-only method from macOS 14.2 (source: same). One button triggers the prompt; if macOS asks for a restart of the app, the step says "Reopen vault-ingest to finish" and resumes here. | Reading meeting window titles and taking screenshots need the screen-recording permission (README). Asked here once, with the reason. |
| Linux | No prompt. The list offers PulseAudio/PipeWire inputs. | No prompt. The app picks the monitor of the default output; a list allows another. | Under Wayland, meeting windows cannot be seen (README), so calls are detected by microphone use only. Said in one line. |

[Skip] is allowed; the status area then shows "Sound not checked" until it is done.

**Step 3 - The AI.**

```
| Who writes the notes?                                                   3 of 3   |
|                                                                                  |
|  (o) Download the built-in AI                              11.0 GB . about 25 min |
|      Runs on this computer. Nothing leaves it.                                   |
|      This PC: NVIDIA with 8 GB. Good enough.    Free disk space: 184 GB.         |
|      You can start using the app while it downloads.                             |
|                                                                                  |
|  ( ) Use something I already have                                                |
|        ( ) LM Studio     [ http://localhost:1234   ]  [Test]                     |
|        ( ) Ollama        [ http://localhost:11434  ]  [Test]                     |
|        ( ) Claude API    [ paste a key             ]  [Test]                     |
|      Speech-to-text still runs on this computer and needs a 3.1 GB download.     |
|                                                                        [Finish]  |
```

Sizes and time are placeholders (unverified); the real numbers come from the engine plan. The time estimate is
measured from the first seconds of the download, not promised in advance. If the disk is too small, the option
says how much is missing and [Finish] stays available for the "already have" path.

**Optional - Connect AI tools.** Not a step. After [Finish] the main window opens on Meetings, whose empty
state (below) carries a short "Still to do" list with this item on it.

### Empty states

Every empty state says what will be here and offers the one action that fills it. No illustrations; one icon at
most.

| Screen | Empty state |
|---|---|
| **Meetings, first use** | "Your meetings will appear here, each with its note." Then "Still to do": (v) Notes folder, (v) Sound check, (~) Built-in AI: 2.4 of 11.0 GB, ( ) Connect your AI tools [Connect], ( ) Make a first note: [Record a test] / [Import a recording]. The list hides itself when all are done or after it is dismissed. |
| Meetings, filter shows nothing | "No meetings match." [Clear filters] |
| Meeting detail, nothing selected | "Choose a meeting on the left." At minimum size the list fills the space instead. |
| Meeting detail, no screenshots | The Screenshots section is absent, not shown empty. First three times only, a hint under the timeline: "Ctrl+Alt+S during a meeting adds a screenshot here." |
| Meeting detail, no audio | Timeline without Play: "This meeting came from a transcript file. There is no audio." |
| **Clients, first use** | "Clients are created the first time you file a meeting under a new name." [New client] |
| Client page, no meetings | "No meetings with Acme yet. The next one you file here will fill this page." The profile sections still show. |
| **Graph, empty vault** | "The graph draws your notes and the links between them. It needs at least one meeting." [Go to Import] |
| Graph, filters hide everything | "Nothing to show with these filters." [Reset filters] |
| **Reader, note missing** | "This note is no longer at <path>." [Search for it] [Back] |
| **Search, no query** | Recent notes and commands. |
| Search, no results | "Nothing matches '<query>'." [Search transcripts too] / [Clear filters] |
| **Import > Phone, no phone** | The step view (3.6) with step 1 current. |
| Import > Phone, not Windows | "Importing from a phone over USB works on Windows in this version. On this system, copy the recordings to this computer and use Files." [Go to Files] |
| Import > Phone, folder empty | "No recordings in <folder>." [Choose another folder] |
| Import > Phone, all imported | "Everything on this phone is already in your vault." The Imported tab opens. |
| **Import > Files** | The drop zone alone. |
| **Settings > Connect AI tools, none found** | "No supported AI tool was found on this computer." [Copy the config] and a link to the manual steps. |
| **Activity, nothing running** | "Nothing is running. Everything recorded so far has a note." |
| Models not downloaded, built-in engine chosen | Banner on Meetings: "The built-in AI is not downloaded yet. Recordings are kept and wait." [Download 11.0 GB] [Use another engine] |

---

## 6. States and feedback

### Where state is shown

One rule: **every long job is visible in three places at three levels of detail.** The tray icon says the kind
of state. The status area says the state and one detail. The Activity drawer and the meeting's own row say
everything.

### Long operations

| Operation | Progress | Pause | Failure |
|---|---|---|---|
| **Model download** | Bytes, percent, speed, time left. In Settings > AI engine, the Activity drawer, and the status area ("Downloading AI 2.4 of 11 GB"). | [Pause] / [Resume] per model; survives closing the app; continues from where it stopped. | No network: retries by itself with growing gaps, says "Waiting for the network", [Try now]. Damaged file: "The download was damaged. Downloading it again." Disk full: "Needs 3.2 GB more free space." |
| **Recording** | Timer and two meters (popup); red control (rail); red tray icon. | None. Recording is never paused by rules. | Section 4, state 6. |
| **Transcription** | Real percent (audio seconds done / total) and time left, on the meeting row, in the detail and the drawer. | By GPU rules or by hand. The job is cancelled and goes back to the front of the queue; nothing was written for that attempt (today's behaviour). Shown as "Paused at 42%; will start over" only if it truly restarts, otherwise just "Paused" (unverified which). | "Could not transcribe: <reason>." [Try again] [Use the CPU] |
| **Writing notes** | Steps, not a percent: "Reading parts 3 of 7" - "Writing the note" - "Checking quotes" - "Updating the client profile". These are the pipeline's real steps. | Same as transcription. | The engine retries three times (30 s, 60 s, 120 s) and shows "Trying again in 60 s". After the third failure the meeting becomes Needs review with the reason and fixes. The vault is untouched until all steps succeed. |
| **Phone import** | Per file and overall (3.6). | [Stop after this one]. | Per row, with [Try again]; a disconnect pauses the whole import. |
| **File import** | Copy progress only for large files; then the normal queue. | - | "Could not read <file>." in the row. |
| **Reprocess** | The meeting row returns to Writing notes; the old note stays readable until the new one is written. | Same as above. | The old note is kept. "Reprocess failed: <reason>." |
| **Connect a tool / health check** | Short spinner on the button (under 2 s typically). | - | Reason and the manual steps. |

### When AI work is paused

Paused always shows **why**, **what is waiting**, and **when it continues**.

| Cause | Status area | Extra |
|---|---|---|
| A priority rule matched | "Paused: Project Zomboid is running" / "2 meetings waiting" | Drawer: "Continues 60 s after it closes." [Run anyway] (needs engine support, unverified) |
| Paused by hand, for 1 hour | "Paused by you . 43 min left" | [Resume] |
| Paused by hand, until resumed | "Paused by you" | [Resume] |
| A call is in progress | "Waiting for the call to end" | Not called a pause. Not the warning colour. |
| Models missing | "Waiting for the AI download" | [Open downloads] |

- The status area and tray icon use the warning (amber) colour and a pause glyph for the first three rows.
- A job that was running is stopped and requeued; the meeting row shows "Paused" with the same reason.
- No notification is shown for an automatic pause by default. The current tool shows one, but the usual cause
  is a game or a full-screen app, which is the worst moment for a notification. The tray icon and status area
  carry it. (assumed; a setting turns the notifications back on)
- On resume: the status area returns to the running job. A notification "Resumed (2 waiting)" only if the
  setting is on.

### Tray icon

One glyph (the app mark) with a small state badge. On macOS it is a menu-bar icon that follows the menu bar's
light or dark appearance; the red recording dot is the only colour used there.

| State | Icon | First menu line (also the tooltip on Windows and macOS) |
|---|---|---|
| Idle | plain glyph | "vault-ingest: nothing running" |
| Call detected, not recording | glyph + hollow dot | "Google Meet call detected" |
| Recording | glyph + red dot | "Recording 12:04" |
| Working (transcribing, writing notes, downloading) | glyph + blue dot | "Transcribing Voice 002, 42%" |
| Paused | glyph + amber pause mark | "Paused: Project Zomboid is running. 2 waiting" |
| Needs attention | glyph + rose exclamation | "1 meeting needs review" |

Priority when several apply: Recording > Needs attention > Paused > Working > Call detected > Idle.

These keep today's colours (grey idle, blue processing, amber paused) and add red for recording.

Tray menu:

```
  Recording 12:04                      (status line, not clickable)
  -----------------------------------
  Stop recording            Ctrl+Alt+R     (or "Start recording")
  Show call popup           Ctrl+Alt+C
  Take a screenshot         Ctrl+Alt+S
  Mark this moment          Ctrl+Alt+M     (only while recording)
  -----------------------------------
  Pause AI work for 1 hour
  Pause AI work until I resume             (or "Resume AI work")
  -----------------------------------
  Open vault-ingest
  Import from phone...
  Settings...
  -----------------------------------
  Quit
```

Left click on Windows and macOS: the call popup if a call is on or ended under 30 minutes ago, otherwise the
main window. On Linux a tray tooltip is not available and a click is not delivered (source:
https://docs.rs/crate/tray-icon/0.24.0/source/src/lib.rs ), so the status line at the top of the menu carries
the state, and the menu is the only way in.

Closing the main window keeps the app in the tray. The first time, a one-off message says so: "vault-ingest
keeps running here so it can notice calls. Quit from this menu."

### System notifications

Used for three things only, each clickable: "Note ready: Acme - Kickoff" (opens it), "A meeting needs review"
(opens it), and "Call detected" with a Record action where the popup cannot be shown. Each can be turned off.

---

## 7. Keyboard

### Shortcuts that work anywhere on the computer (all can be changed or cleared in Settings)

| Action | Default | Today |
|---|---|---|
| Show the call popup and give it the keyboard | Ctrl+Alt+C | same |
| Take a screenshot (region) | Ctrl+Alt+S | same |
| Pause / resume AI work | Ctrl+Alt+G | same |
| Start / stop recording | Ctrl+Alt+R | new |
| Mark this moment | Ctrl+Alt+M | new |

On macOS the same keys are Control+Option+letter. Two cautions: under Wayland these cannot be registered (see
section 4), and on keyboard layouts where the right Alt key types symbols, Ctrl+Alt+letter can collide with a
character (unverified for the Bosnian/Croatian layout; test G, C, S, M and R before shipping defaults). Settings
reports a conflict when a combination cannot be registered.

### Inside the main window

| Keys | Action |
|---|---|
| Ctrl/Cmd+K | Command palette / search |
| Ctrl/Cmd+1 2 3 4 | Meetings, Clients, Graph, Import |
| Ctrl/Cmd+, | Settings |
| Ctrl/Cmd+J | Activity drawer |
| Alt+Left / Alt+Right (Cmd+[ / Cmd+]) | Back / Forward |
| / | Filter the current list |
| Up / Down, or J / K | Move in a list |
| Enter | Open the selected row |
| Esc | Close palette, drawer, lightbox, card; clear a selection; leave a filter field |
| Ctrl/Cmd+O | Open the current note in Obsidian |
| Ctrl/Cmd+F | Find in the current note |
| Ctrl/Cmd+L | Copy a link to the current note |
| Space | Play / pause the meeting's audio (not while typing) |
| Left / Right | Back / forward 5 seconds (when the timeline has focus) |
| [ / ] | Previous / next transcript part |
| Ctrl/Cmd+A, Shift+click, Space | Select all, select a range, toggle a row (Import > Phone) |
| + / - / 0 | Zoom in, zoom out, fit (Graph) |
| ? | Show this list |
| F6 | Move between rail, list and detail |

Page reload and the webview's own menu shortcuts are switched off.

### Command palette contents

| Group | Commands |
|---|---|
| **Recording** | Start recording / Stop recording . Take a screenshot . Mark this moment . Show the call popup |
| **Go to** | Meetings . Clients . Graph . Import from phone . Import files . Settings (and each settings section by name) . Activity |
| **Open** | Any client by name or alias . Any meeting by title . Any note by title . Recent notes |
| **This note / meeting** (only when one is open) | Open in Obsidian . Open in the default editor . Reveal in file manager . Copy link . Copy as Markdown . Change client... . Reprocess notes . Reprocess from audio . Show in graph |
| **Clients** | New client... |
| **AI work** | Pause for 1 hour . Pause until I resume . Resume . Run anyway . Switch engine to Built-in / LM Studio / Ollama / Claude API |
| **Setup** | Connect AI tools . Run a health check . Check for updates . Open the logs folder . Open the notes folder . Keyboard shortcuts |
| **App** | Quit vault-ingest |

Commands that cannot run now are not listed (there is no "Stop recording" when nothing is recording).

---

## 8. Accessibility and platform notes

### Contrast on dark

- Body and secondary text at least 4.5:1 against every surface it sits on, including hover and selected rows.
  Both palettes in `DESIGN-direction.md` were computed, not estimated; the lowest text pair is 4.58:1 (muted text
  on a hovered row).
- Borders of inputs, checkboxes and buttons at least 3:1. Decorative dividers may be fainter.
- Pure white on pure black is avoided: text is an off-white, grounds are tinted near-black.
- State is never colour alone. Every state has an icon and a word: recording (dot + "Recording"), paused
  (pause glyph + "Paused"), error (exclamation + reason). Recording and error both live near red, so they also
  differ in shape and in hue (see the design file).
- Client colours never stand alone; the name is always next to the swatch. In the graph, type is carried by
  shape and size.
- When the system asks for more contrast, borders and muted text step up. When Windows "contrast themes"
  (forced colours) are on, the app uses the system colours and drops its own.

### Focus and keyboard

- Everything works without a mouse, including the graph (arrow keys, and the list alternative), the timeline
  (it is a slider: arrows seek, Home/End jump), the phone list and the popup card.
- Focus order follows reading order: top bar, rail (Record first), list, detail header, detail body, side panel.
  F6 jumps between those regions.
- A visible focus ring, 2 px, with a gap, on every focusable thing. It is never removed.
- The palette, drawer, lightbox and card keep focus inside while open and return it to where it came from.
- The popup bar is not in any focus order until the user enters it; then Tab moves Stop, Screenshot, Mark,
  client, close.
- Click targets at least 28 px high in dense lists and 32 px elsewhere; the popup's Stop and Record are 36 px.

### Screen readers

- The rail is a navigation landmark; list and detail are labelled regions; the meeting list is a list with
  rows that read "Acme, Kickoff, 30 September, 42 minutes, 3 decisions, 5 action items".
- Status changes are announced politely ("Transcribing Voice 002, 42 percent" at most every 25 percent; "Note
  ready"). Start and stop of recording are announced at once.
- The level meters have a text form ("microphone level: good", "call audio: silent").
- The graph announces the selected node and its number of links; the list view is the full alternative.
- Notes carry the language of the note where it is known, so Bosnian text is not read with English rules.

### Motion

- With "reduce motion" on: no pulsing record dot (solid dot), no sliding drawer (it appears), no animated
  graph layout (the finished layout is shown), no smooth scrolling, no shimmer.
- Without it, motion is still only for state: 120-180 ms for hover, press, drawer and popup changes. No
  animation when the app or a section opens.
- The level meters and progress bars are data, not decoration, and keep moving.

### Text size

- A text size setting (90%, 100%, 110%, 125%) and Ctrl/Cmd + and -. Layout holds at 125% at the minimum window
  size because the side panel and rail have already collapsed.
- The reader's line length stays near 68 characters at every size.

### Window chrome per system

| | Windows | macOS | Linux |
|---|---|---|---|
| Main window | Native title bar in dark mode; the search field sits in an app toolbar below it. A custom title bar is left for later, so window snapping keeps working (unverified whether a frameless Tauri window keeps the snap menu). | Native traffic lights overlaid on the app toolbar (the title bar style setting exists for macOS, source: Tauri WindowConfig). Standard app menu with Preferences (Cmd+,) and Quit. | Native decorations from the desktop. No custom title bar. |
| Closing the window | Hides to the tray (setting). | Hides; the app stays in the menu bar and Dock. Cmd+Q quits. | Hides to the tray if a tray exists; otherwise minimises. A tray needs the desktop's AppIndicator support and its library installed (source: tray-icon docs). |
| Popup | Borderless, always on top, not in the taskbar, not focused. Rounded corners and shadow come from the system. | Borderless, always on top, shown on all Spaces (supported on macOS and Linux, not on Windows - source: Tauri WindowConfig). | Borderless with a 1 px border (window shadow is not available there - same source). X11: always on top works. Wayland: see section 4. |
| Start at login | Yes. | Yes. | Yes (autostart entry). |
| Phone import | Yes. | No (engine). | No (engine). |
| Unsigned build | The installer and first start trigger a system warning. The download page and the first-run screen say this in one sentence and show how to proceed. | Same, with the macOS steps. | Not applicable for AppImage/.deb in the same way. |

File paths are shown in the system's own form. Shortcuts are shown with the system's own key names.

---

## 9. Deliberately not in v1

- **Editing notes.** No editor, no ticking of action items, no renaming of sections. Obsidian or any editor
  does that; the app reloads the file.
- **Deleting anything.** The engine never deletes; neither does the app. Removing a meeting is done in the
  file manager.
- **A light theme, custom themes, accent colour pickers.** The tokens are named by role so a light theme can
  come later.
- **A live transcript during the call.** Notes come after.
- **Speaker identification controls** (naming speakers, merging voices).
- **Calendar integration** (titles and attendees from a calendar).
- **A cross-client "everything I owe" view.** The connected AI tools answer that from the vault today.
- **Chat with the vault inside the app.** Same reason: "Connect AI tools" is the route.
- **More than one vault at a time.** One vault; changing it is a setting.
- **Phone import on macOS and Linux, over Wi-Fi, or from iPhones.**
- **Graph extras:** saved views, custom groups and colours, a time-ordered layout, people or tags as nodes.
- **Editing prompts in the app.** Advanced opens the prompts folder.
- **Silent auto-update.** v1 says a new version exists and opens the download page.
- **Languages other than English for the interface.** Notes are in any language already.
- **Sync, accounts, sharing, telemetry.** None.
- **A meeting bot that joins calls.** The app records what this computer hears.

---

## 10. Open questions for the owner

Each has the answer this plan already assumes.

1. Is auto-record "per device" meant per computer or per microphone/headset? **Assumed: per computer, with a per-app list where WhatsApp, Viber and "any other app" start switched off.**
2. Should the popup be hidden from screen sharing so clients do not see it? **Assumed: yes by default; it works on Windows, not reliably on macOS 15+, and Settings says so.**
3. Should the graph show the whole vault or only client notes at first? **Assumed: the whole vault, without `_System`, raw files and transcript parts; parts are a checkbox.**
4. May v1 change note text at all (tick an action item, fix a title)? **Assumed: no; only the engine's own operations (change client, reprocess, skip).**
5. In a mass phone import, may the AI pick the client when none is set, with the unsure ones going to "Needs review"? **Assumed: yes, with "one client for all" and a per-row choice.**
6. Are three small engine additions fine for v1: Mark moment, "Run anyway" during an automatic pause, "Record again" after Stop? **Assumed: yes to all three; Mark moment first.**
7. Should an automatic GPU pause stay silent (tray and status only) instead of a notification, as today? **Assumed: silent by default, with a setting.**
8. Which visual direction: A "Oxide" (warm, reading serif in notes; recommended) or B "Night Line" (cool, compact, line-map graph)? **Assumed: A.**
