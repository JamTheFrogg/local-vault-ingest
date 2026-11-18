"""Show one desktop notification in its own process, on Windows, macOS or Linux.

    python -m vault_ingest.notify <text> [open_path] [click_file]

`open_path` is opened when the notification is clicked and `click_file` is created then, where the platform
reports clicks (Windows toasts, Linux notify-send 0.7.12+). macOS `osascript` notifications cannot report a
click, so there the text says what to do instead.

On Windows this runs the toast code from `win_toast` (winrt must never load inside the watcher: it crashes
once Whisper's CUDA libraries are in the process).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

TITLE = "Vault ingest"


def applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def mac(text: str) -> int:
    script = f"display notification {applescript_string(text)} with title {applescript_string(TITLE)}"
    return subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15).returncode


def linux(text: str, open_path: str, click_file: str) -> int:
    if not shutil.which("notify-send"):
        print(f"notify-send not installed: {text}", file=sys.stderr)
        return 1
    if not open_path and not click_file:
        return subprocess.run(["notify-send", "-a", TITLE, TITLE, text], capture_output=True, timeout=15).returncode
    try:                                                      # libnotify 0.7.12+: wait for the click
        proc = subprocess.run(["notify-send", "-a", TITLE, "--action=default=Open", "--wait", TITLE, text],
                              capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return 0
    if proc.returncode != 0:                                  # older notify-send: no actions
        return subprocess.run(["notify-send", "-a", TITLE, TITLE, text], capture_output=True, timeout=15).returncode
    if proc.stdout.strip() == "default":
        if open_path:
            subprocess.Popen(["xdg-open", open_path], start_new_session=True)
        if click_file:
            Path(click_file).parent.mkdir(parents=True, exist_ok=True)
            Path(click_file).write_text("clicked\n", encoding="utf-8")
    return 0


def main(argv: list[str]) -> int:
    text = (argv[1] if len(argv) > 1 else "")[:200]
    open_path = argv[2] if len(argv) > 2 else ""
    click_file = argv[3] if len(argv) > 3 else ""
    try:
        if sys.platform == "win32":
            from .win_toast import main as windows_main

            return windows_main(argv)
        if sys.platform == "darwin":
            return mac(text)
        return linux(text, open_path, click_file)
    except Exception as e:  # noqa: BLE001
        print(f"Notification unavailable ({e}): {text}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
