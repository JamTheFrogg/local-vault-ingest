"""The tray icon and global hotkeys in their own process (macOS and Linux).

macOS lets only a process's main thread run a tray icon (and Tk), and pynput's macOS listener can crash a
process that also runs Tk. So on macOS and Linux the companion starts this helper instead:

    python -m vault_ingest.ui_child tray '{"title": "...", "icon": "idle", "items": [...]}'
    python -m vault_ingest.ui_child hotkeys '{"combos": ["ctrl+alt+c", ...]}'

The parent writes commands on stdin (`icon <phase>`, `title <text>`, `quit`); the helper writes events on
stdout (`click <id>`, `hotkey <combo>`). If the helper dies, only the tray icon or the hotkeys are gone.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from typing import Callable

log = logging.getLogger("vault_ingest")

COLORS = {"idle": (120, 120, 120), "processing": (70, 130, 220), "paused": (220, 160, 40)}


def icon_image(phase: str):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((8, 8, 56, 56), fill=COLORS.get(phase, COLORS["idle"]))
    return image


def pynput_hotkey(combo: str) -> str:
    names = {"control": "ctrl", "win": "cmd", "windows": "cmd", "super": "cmd", "option": "alt", "opt": "alt",
             "command": "cmd"}
    keys = [names.get(k, k) for k in combo.lower().replace(" ", "").split("+") if k]
    return "+".join(f"<{k}>" if len(k) > 1 else k for k in keys)


def _emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


# ------------------------------------------------------------------ child side
def run_tray(spec: dict) -> int:
    import pystray

    def clicked(item_id: str):
        return lambda _icon, _item: _emit(f"click {item_id}")

    items = []
    for it in spec.get("items") or []:
        if it == "-":
            items.append(pystray.Menu.SEPARATOR)
        else:
            items.append(pystray.MenuItem(str(it["label"]), clicked(str(it["id"])), default=bool(it.get("default"))))
    icon = pystray.Icon("vault-ingest", icon_image(spec.get("icon", "idle")), spec.get("title", "Vault ingest"),
                        menu=pystray.Menu(*items))

    def read_commands() -> None:
        for raw in sys.stdin:
            cmd, _, arg = raw.strip().partition(" ")
            try:
                if cmd == "icon":
                    icon.icon = icon_image(arg)
                elif cmd == "title":
                    icon.title = arg
                elif cmd == "quit":
                    break
            except Exception:  # noqa: BLE001
                pass
        icon.stop()                                           # stdin closed: the companion is gone

    threading.Thread(target=read_commands, daemon=True).start()
    icon.run()                                                # main thread, as macOS requires
    return 0


def run_hotkeys(spec: dict) -> int:
    from pynput import keyboard

    combos = [c for c in spec.get("combos") or [] if c]
    mapping = {pynput_hotkey(c): (lambda c=c: _emit(f"hotkey {c}")) for c in combos}
    listener = keyboard.GlobalHotKeys(mapping)
    listener.daemon = True
    listener.start()
    for raw in sys.stdin:                                     # until the companion quits
        if raw.strip() == "quit":
            break
    listener.stop()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("tray", "hotkeys"):
        print(__doc__, file=sys.stderr)
        return 2
    spec = json.loads(argv[2])
    try:
        return run_tray(spec) if argv[1] == "tray" else run_hotkeys(spec)
    except Exception as e:  # noqa: BLE001
        print(f"{argv[1]} helper failed: {e}", file=sys.stderr)
        return 1


# ------------------------------------------------------------------ parent side
def parse_event(line: str) -> tuple[str, str] | None:
    kind, _, arg = line.strip().partition(" ")
    if kind in ("click", "hotkey") and arg:
        return kind, arg
    return None


class ChildUI:
    """Parent handle for one helper. `on_event(kind, arg)` runs on a reader thread."""

    def __init__(self, mode: str, spec: dict, on_event: Callable[[str, str], None]):
        self.mode = mode
        self.on_event = on_event
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "vault_ingest.ui_child", mode, json.dumps(spec)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
            encoding="utf-8", errors="replace")
        threading.Thread(target=self._read, name=f"{mode}-helper", daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            event = parse_event(line)
            if event is None:
                continue
            try:
                self.on_event(*event)
            except Exception:  # noqa: BLE001
                log.exception("%s action failed", self.mode)
        code = self.proc.wait()
        err = (self.proc.stderr.read() if self.proc.stderr else "").strip()
        if code not in (0, None):
            log.warning("The %s helper stopped (exit %s)%s", self.mode, code, f": {err[-300:]}" if err else "")

    def send(self, line: str) -> None:
        try:
            if self.proc.poll() is None and self.proc.stdin is not None:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        self.send("quit")
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            self.proc.kill()


class ChildHotkeys:
    """Same interface as a pynput listener (`stop()`), with the listener in a helper process."""

    def __init__(self, combos: dict[str, Callable[[], None]]):
        self.actions = {c: a for c, a in combos.items() if c}
        self.child = ChildUI("hotkeys", {"combos": list(self.actions)}, self._event)

    def _event(self, kind: str, combo: str) -> None:
        action = self.actions.get(combo)
        if kind == "hotkey" and action is not None:
            threading.Thread(target=action, name="hotkey-action", daemon=True).start()

    def stop(self) -> None:
        self.child.stop()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
