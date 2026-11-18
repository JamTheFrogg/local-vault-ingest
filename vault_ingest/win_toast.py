"""Show one Windows toast in its own process.

windows-toasts loads winrt/MSVCP140.dll. That copy access-violates inside the
watcher once Whisper loads its CUDA libraries, so toasts never run in-process.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    text = (argv[1] if len(argv) > 1 else "")[:200]
    open_path = argv[2] if len(argv) > 2 else ""
    click_file = argv[3] if len(argv) > 3 else ""
    try:
        from windows_toasts import Toast, WindowsToaster
        if not open_path and not click_file:
            WindowsToaster("Vault ingest").show_toast(Toast([text]))
            return 0
        from windows_toasts import InteractableWindowsToaster

        def activated(_args) -> None:
            if open_path:
                import os
                os.startfile(open_path)  # type: ignore[attr-defined]
            if click_file:
                Path(click_file).parent.mkdir(parents=True, exist_ok=True)
                Path(click_file).write_text("clicked\n", encoding="utf-8")

        InteractableWindowsToaster("Vault ingest").show_toast(Toast([text], on_activated=activated))
        time.sleep(180)
    except Exception as e:  # noqa: BLE001
        print(f"Toast unavailable ({e}): {text}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
