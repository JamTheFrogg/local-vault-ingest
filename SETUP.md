# Setup checklist

Everything is already configured for your PC:

- Vault: `C:\Users\Faris\Desktop\MyVault`
- This tool: `C:\Users\Faris\Desktop\Projects\vault-ingest`

These are the steps only you can do. Do them in order. Total time is about 15 minutes.

---

## 1. Install the tool

> **On macOS or Linux?** Run `bash unix/install.sh` and later `bash unix/register-startup.sh` instead of the PowerShell scripts below, and see [macOS and Linux](README.md#macos-and-linux) for the permissions and packages each needs. The rest of this checklist is the same.

Open **PowerShell** in this folder. In File Explorer, open the folder, click the address bar, type `powershell`, and press Enter. Then run:

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
```

If it says Python is missing, install Python 3.12 from <https://www.python.org/downloads/>. Tick **"Add python.exe to PATH"** during setup, then run the command again. The installer ends with `Installed OK.` once the self-tests pass.

## 2. Download the Obsidian MCP server

1. Go to <https://github.com/lstpsche/obsidian-mcp/releases/latest>
2. Download the file ending in **`x86_64-pc-windows-msvc.zip`**. If there is also an `obsidian-semanticd-...-windows...zip`, download that too.
3. Extract **all `.exe` files** into this tool's `bin` folder, so you end up with `vault-ingest\bin\obsidian-mcp.exe`.

Windows may warn that the program is unrecognized the first time it runs. It's an unsigned open-source tool, so click *More info → Run anyway*.

## 3. Add your Anthropic API key

1. Create a key at <https://console.anthropic.com/> under *API Keys*. The API is billed separately from a Claude subscription. Each call summary costs a few cents.
2. Open the `.env` file in this folder with Notepad and paste the key after `ANTHROPIC_API_KEY=`. Don't share this file.
3. Check that everything is connected:

```powershell
.\.venv\Scripts\vault-ingest check
```

You want to see all ✔ and `LLM answered`.

## 4. Start both background services (runs at every login)

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\register-startup.ps1
```

This starts the **Obsidian MCP server**, which your AIs use to search, read, and write the vault. It also starts the **ingest watcher**, which processes new transcripts, and the **Meeting companion**. It should end with `Obsidian MCP server is up`.

### Phone recordings

Plug the phone in and choose **File transfer**, not charging only. The Meeting companion then watches `Internal storage/Recordings/Voice Recorder` on Faris's A33. Settings are the `[phone]` block in `companion.toml` (device name, USB id, folder, and how often to look). A new recording opens the client picker. The hash ledger is `state/phone_imports.json` in this folder. Delete that file and restart the companion to rebuild it from `_System/audio`.

## 5. Connect your AI apps

The ready-made config files are in the `configs` folder.

**Cursor**: Settings → *MCP* → *Add new global MCP server*. This opens `C:\Users\Faris\.cursor\mcp.json`. Paste the `obsidian` entry from `configs\cursor-mcp.json` into its `mcpServers` section.

**Claude Code**: run once in any terminal:
```
claude mcp add --transport http --scope user obsidian http://127.0.0.1:37842/mcp
```

**Claude Desktop**: Settings → *Developer* → *Edit Config*. This opens `%APPDATA%\Claude\claude_desktop_config.json`. Merge in the `obsidian` entry from `configs\claude_desktop_config.json`, then fully quit Claude Desktop (tray icon → Quit) and reopen it.

**Claude Cowork** uses that same Desktop connector. No second MCP config. Connect the vault folder as in step 6.

**Codex**: open `C:\Users\Faris\.codex\config.toml` and paste the block from `configs\codex-config.toml` at the end.

If a config file already has other `mcpServers`, add `"obsidian": {...}` next to them. Don't replace the whole file.

## 6. Tell each AI to use the vault

Paste the sentence from `configs\standing-instruction.txt` into:

| App | Where |
|---|---|
| Claude Desktop | Settings → the *personal preferences* box |
| Claude Code | `C:\Users\Faris\.claude\CLAUDE.md` (create it if missing) |
| Cursor | Settings → *Rules* → *User Rules* |
| Codex | `C:\Users\Faris\.codex\AGENTS.md` (create it if missing) |

The real rules live in the vault at `_System/AI-Instructions.md`. To change how every AI behaves, edit only that file.

### Claude Cowork

Cowork works on a folder you connect. Point it at the vault, not at this tool.

1. `configs\vault-CLAUDE.md` is already copied to `MyVault\CLAUDE.md`. Cowork reads that file whenever the vault folder is connected. It tells Cowork how to find client context and what not to touch. If you ever replace the vault copy, copy this file again.
2. In Claude Desktop, open **Projects** → **+** → **Use an existing folder**, and choose `C:\Users\Faris\Desktop\MyVault`. Name the project `Vault`.
3. In that project's **Instructions**, paste `configs\cowork-instructions.txt`. In **Description**, put: `Faris's client vault. Read CLAUDE.md and _System/AI-Instructions.md before answering.`
4. Start tasks from that project. In the task's **+** menu, leave the **obsidian** connector on so Cowork can search the vault, not only open files it already knows.
5. If your message box has no Chat / Cowork switch, you already have the combined Claude. Same project, same folder. Tasks that need the vault still need Claude Desktop open on this PC.

For tasks you start outside that project, also paste `configs\cowork-instructions.txt` into **Settings → Cowork → Global instructions**. If that page is gone, use **Settings → General → Instructions for Claude** instead. Global instructions apply to every conversation, so prefer the project when you can.

Cowork does not process transcripts. Dropping a file in `00_Inbox` is still how a call gets ingested. Keep the desktop app open for the whole task if it is using local files.

## 7. Test it

1. Name your transcript file like this and drop it into `MyVault\00_Inbox\`:
   **`<Client> - YYYY-MM-DD - <Title>.txt`**, for example `Acme - 2026-09-30 - Kickoff.txt`.
   `.txt`, `.md`, `.vtt`, `.srt`, and `.docx` all work. A new client name creates the client folder automatically.
2. Wait about a minute. The file moves to `Clients\Acme\Transcripts\raw\`, and you get:
   - `Clients\Acme\Calls\2026-09-30 Kickoff.md`, the call summary
   - `Clients\Acme\Transcripts\2026-09-30 Kickoff\...Part 01.md` and the other parts, the searchable pieces
   - an updated `Clients\Acme\Acme.md` profile. Fill in its **Snapshot** section yourself once.
3. Open a **new** chat in Claude Desktop, Cursor, or Codex — or a task in the Cowork **Vault** project — and ask: *"Where are we with Acme and what do I owe them?"*

If a file ends up in `00_Inbox\_needs-review\`, open the `.why.md` note next to it. It explains the problem, which is usually that the file name doesn't say which client the call was with.

## If something doesn't work

- Watcher log: `MyVault\_System\ingest.log`
- MCP server log: `%LOCALAPPDATA%\obsidian-mcp\obsidian-mcp.log`
- Is the server up? Open <http://127.0.0.1:37842/health> in a browser.
- Restart both services: log out and back in, or in Task Scheduler right-click each task → *Run*.
