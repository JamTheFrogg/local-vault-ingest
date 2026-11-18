"""Always-on-top client picker. A meeting opens it without taking focus; Ctrl+Alt+C, the tray icon or a click
does. Closing it is never final: the tray icon (bottom right, next to the clock) brings it back."""
from __future__ import annotations

import logging
import queue
import re
import sys
import threading

from . import desktop
from .clients import CHAT_APPS, meeting_key
from .picker import AskDuplicate, AskField, Created, DecideLater, PickerState, Select, Skip

log = logging.getLogger("vault_ingest")

WIDTH, HEIGHT, MARGIN = 440, 440, 16

# dark card that stands out over a meeting window
FONT = desktop.ui_font()
BG, FIELD, EDGE = "#1f2128", "#2a2d36", "#3b3f4b"
FG, MUTED = "#eceef3", "#9aa0ad"
ACCENT, REC, REC_HOVER = "#4c8dff", "#e5484d", "#c7383d"

_TITLE_NOISE = re.compile(
    r"^(meeting (join|compact view)|meeting|call)\s*\|\s*"
    r"|\s*[|\-–—]\s*(microsoft teams|google chrome|microsoft ?edge|mozilla firefox|brave|opera)\s*$", re.I)


def headline(session, heading: str = "") -> tuple[str, str]:
    """(eyebrow, headline) for the top of the popup."""
    if heading:
        return "PHONE RECORDING", heading
    if session is None:
        return "CLIENT PICKER", "No meeting right now"
    title = (getattr(session, "window_title", "") or "").strip()
    for _ in range(3):
        title = _TITLE_NOISE.sub("", title).strip()
    app = getattr(session, "app", "") or "Meeting"
    line = f"{app}  ·  {title}" if title and title.lower() != app.lower() else app
    if len(line) > 90:
        line = line[:87] + "…"
    live = getattr(session, "end", None) is None
    return ("●  MEETING IN PROGRESS" if live else "MEETING ENDED"), line


def person_guess(session) -> str:
    """For a WhatsApp/Viber... call the window carries the contact's name; offer it for a personal call."""
    if session is None or (getattr(session, "app", "") or "").lower() not in CHAT_APPS:
        return ""
    title = (getattr(session, "window_title", "") or "").strip()
    return "" if title.lower() in CHAT_APPS or title.lower().endswith(" call") else title


def bottom_right(right: int, bottom: int, width: int = WIDTH, height: int = HEIGHT,
                 margin: int = MARGIN) -> tuple[int, int]:
    return max(0, right - width - margin), max(0, bottom - height - margin)


def work_area(root=None) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the screen area above the taskbar / Dock."""
    return desktop.work_area(root)


def top_hwnd(widget) -> int:
    if sys.platform != "win32":
        return int(widget.winfo_id())
    import ctypes

    hwnd = int(widget.winfo_id())
    parent = ctypes.windll.user32.GetParent(hwnd)
    return int(parent or hwnd)


def foreground_hwnd() -> int:
    if sys.platform != "win32":
        return 0
    import ctypes

    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def give_focus_back(hwnd: int) -> None:
    """Showing a Tk window activates it; hand the keyboard straight back to the meeting."""
    if not hwnd or sys.platform != "win32":
        return
    import ctypes

    ctypes.windll.user32.SetForegroundWindow(hwnd)


def place_window(hwnd: int, x: int, y: int, width: int, height: int, activate: bool) -> None:
    """Show the window topmost. Without activate, the meeting keeps the keyboard."""
    if sys.platform != "win32":
        return                                  # Tk's -topmost and geometry already placed it
    import ctypes

    HWND_TOPMOST = -1
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040
    flags = SWP_SHOWWINDOW | (0 if activate else SWP_NOACTIVATE)
    ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, width, height, flags)
    if activate:
        ctypes.windll.user32.SetForegroundWindow(hwnd)


class PickerWindow:
    """Tk on its own thread. The companion only posts show/refresh; it never touches widgets."""

    def __init__(self, load_records, on_result, on_record, meeting_map, wait: bool = True,
                 own_thread: bool = True):
        self.load_records = load_records
        self.on_result = on_result
        self.on_record = on_record
        self.meeting_map = meeting_map
        self.queue: queue.Queue = queue.Queue()
        self.ready = threading.Event()
        self.failed = False
        self.session = None
        self.state: PickerState | None = None
        self.heading = ""
        self.shown = False
        self._ignore_query = False
        self.thread = None
        if not own_thread:
            # macOS: Tk only runs on the main thread. The caller runs `run()` there and the companion's
            # loop moves to a worker thread; show/refresh still only post to the queue.
            return
        self.thread = threading.Thread(target=self._main, name="client-picker", daemon=True)
        self.thread.start()
        ready = self.ready.wait(timeout=8)
        if wait and (not ready or self.failed):
            raise RuntimeError("the client picker window did not start")

    def show(self, session, activate: bool = False, *, skip_label: str | None = None,
             heading: str = "", title: str | None = None, hide_record: bool = False) -> None:
        self.shown = True
        self.queue.put(("show", session, activate, {
            "skip_label": skip_label, "heading": heading, "title": title, "hide_record": hide_record,
        }))

    def refresh_session(self, session) -> None:
        self.queue.put(("refresh", session))

    def close(self) -> None:
        self.queue.put(("quit",))

    def run(self) -> None:
        """Run the window on the calling thread until `close()` (macOS main thread)."""
        self._main()

    # ---------------------------------------------------------------- thread
    def _main(self) -> None:
        try:
            import tkinter as tk
        except Exception as e:  # noqa: BLE001
            log.warning("tkinter is not available (%s)", e)
            self.failed = True
            self.ready.set()
            return
        try:
            self.tk = tk
            self.root = tk.Tk()
            self.root.withdraw()
            self.root.title("Client picker")
            self.root.resizable(False, False)
            self.root.attributes("-topmost", True)
            self._build()
            self.ready.set()
            self.root.after(50, self._poll)
            self.root.mainloop()
        except Exception:  # noqa: BLE001
            log.exception("Client picker window failed")
            self.failed = True
            self.ready.set()

    def _build(self) -> None:
        tk = self.tk
        root = self.root
        root.geometry(f"{WIDTH}x{HEIGHT}")
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self._close)
        font = lambda size, weight="normal": (FONT, size, weight)  # noqa: E731

        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=16, pady=(14, 6))
        self.eyebrow_var = tk.StringVar(value="CLIENT PICKER")
        self.eyebrow = tk.Label(header, textvariable=self.eyebrow_var, bg=BG, fg=REC, font=font(8, "bold"),
                                anchor="w")
        self.eyebrow.pack(fill="x")
        self.headline_var = tk.StringVar()
        tk.Label(header, textvariable=self.headline_var, bg=BG, fg=FG, font=font(11, "bold"), anchor="w",
                 justify="left", wraplength=WIDTH - 40).pack(fill="x", pady=(2, 0))

        self.prompt_var = tk.StringVar()
        self.prompt = tk.Label(root, textvariable=self.prompt_var, anchor="w", justify="left", bg=BG, fg=MUTED,
                               font=font(9), wraplength=WIDTH - 32)
        self.prompt.pack(fill="x", padx=16, pady=(4, 4))

        self.query_var = tk.StringVar()
        self.query_var.trace_add("write", self._on_query)
        self.search = self._entry(root, self.query_var, size=11)
        self.search.pack(fill="x", padx=16, ipady=6)

        self.list_frame = tk.Frame(root, bg=BG)
        self.list_frame.pack(fill="both", expand=True, padx=16, pady=(8, 4))
        self.list = tk.Listbox(self.list_frame, height=8, activestyle="none", exportselection=False, bg=BG, fg=FG,
                               selectbackground=ACCENT, selectforeground="#ffffff", highlightthickness=0,
                               borderwidth=0, relief="flat", font=font(10))
        self.list.pack(fill="both", expand=True)
        self.list.bind("<<ListboxSelect>>", self._on_list_click)
        self.list.bind("<Double-Button-1>", lambda _e: self._enter())

        self.title_var = tk.StringVar()
        title_row = tk.Frame(root, bg=BG)
        title_row.pack(fill="x", padx=16, pady=(2, 2))
        tk.Label(title_row, text="Title", bg=BG, fg=MUTED, font=font(9)).pack(side="left", padx=(0, 8))
        self.title_entry = self._entry(title_row, self.title_var, size=10)
        self.title_entry.pack(side="left", fill="x", expand=True, ipady=3)

        self.hint_var = tk.StringVar()
        tk.Label(root, textvariable=self.hint_var, anchor="w", bg=BG, fg=ACCENT, font=font(9, "bold")).pack(
            fill="x", padx=16)

        self.bottom = tk.Frame(root, bg=BG)
        self.bottom.pack(fill="x", side="bottom", padx=16, pady=(0, 10))
        record = tk.Button(self.bottom, text="●  Record + capture participants", command=self._record, bg=REC,
                           fg="#ffffff", activebackground=REC_HOVER, activeforeground="#ffffff", relief="flat",
                           borderwidth=0, cursor="hand2", font=font(10, "bold"), pady=7)
        record.bind("<Enter>", lambda _e: record.configure(bg=REC_HOVER))
        record.bind("<Leave>", lambda _e: record.configure(bg=REC))
        record.pack(fill="x", pady=(6, 6))
        self.record = record
        self.reopen = tk.Label(self.bottom, text="↑↓ choose · Enter confirm · Esc later · tray icon reopens this",
                               anchor="w", bg=BG, fg=MUTED, font=font(8))
        self.reopen.pack(fill="x")
        for widget in (root, self.search, self.list, self.title_entry, record):
            widget.bind("<Up>", self._up)
            widget.bind("<Down>", self._down)
            widget.bind("<Return>", self._enter_event)
            widget.bind("<Escape>", lambda _e: self._escape())
            widget.bind("<Button-1>", self._clicked, add="+")
        self.search.bind("<KeyPress-n>", self._no_key)
        self.search.bind("<KeyPress-N>", self._no_key)

    def _entry(self, parent, var, size: int):
        return self.tk.Entry(parent, textvariable=var, bg=FIELD, fg=FG, insertbackground=FG, relief="flat",
                             borderwidth=0, highlightthickness=1, highlightbackground=EDGE, highlightcolor=ACCENT,
                             font=(FONT, size))

    def _poll(self) -> None:
        try:
            while True:
                cmd = self.queue.get_nowait()
                kind = cmd[0]
                if kind == "quit":
                    self.root.quit()
                    return
                if kind == "show":
                    _, session, activate, *rest = cmd
                    try:
                        self._open(session, activate, **(rest[0] if rest else {}))
                    except Exception:  # noqa: BLE001  one bad open must not stop later meetings
                        log.exception("Could not open the client picker")
                        self.shown = False
                elif kind == "refresh":
                    self._refresh(cmd[1])
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _open(self, session, activate: bool, skip_label: str | None = None, heading: str = "",
              title: str | None = None, hide_record: bool = False) -> None:
        self.session = session
        preselect, chosen_title = None, ""
        if session is not None:
            key = session.meeting_key or meeting_key(session.app, session.window_title)
            preselect = session.client or self.meeting_map.get(key)
            chosen_title = session.call_title or ""
            if key and not session.meeting_key and session.app != "Phone":
                session.meeting_key = key
        if title is not None:
            chosen_title = title
        try:
            records = self.load_records()
        except Exception:  # noqa: BLE001
            log.exception("Could not read client folders")
            records = []
        self.heading = heading or ""
        self.root.title("Phone recording" if self.heading else "Meeting companion")
        eyebrow, line = headline(session, self.heading)
        self.eyebrow_var.set(eyebrow)
        self.eyebrow.configure(fg=REC if eyebrow.startswith("●") else ACCENT)
        self.headline_var.set(line)
        self.state = PickerState(records, preselect=preselect, title=chosen_title,
                                 skip_label=skip_label or "Don't ingest this meeting", person=person_guess(session))
        self._ignore_query = True
        self.query_var.set("")
        self.title_var.set(chosen_title)
        self._ignore_query = False
        self.record.pack_forget()
        self.reopen.pack_forget()
        if not hide_record:
            self.record.pack(fill="x", pady=(6, 6))
        self.reopen.pack(fill="x")
        self._render()
        self._place(activate)

    def _refresh(self, session) -> None:
        """The meeting title settled. Preselect its client when the user hasn't started typing."""
        if self.state is None or self.session is None or session is None or session.id != self.session.id:
            return
        self.session = session
        if self.state.mode != "browse" or self.state.query.strip():
            return
        key = session.meeting_key or meeting_key(session.app, session.window_title)
        chosen = session.client or self.meeting_map.get(key)
        if not chosen or chosen == self.state.preselect:
            return
        self.state.preselect = chosen
        self.state.refresh()
        self._render()

    def _place(self, activate: bool) -> None:
        _left, _top, right, bottom = work_area(self.root)
        x, y = bottom_right(right, bottom)
        self.root.geometry(f"{WIDTH}x{HEIGHT}+{x}+{y}")
        # Tk must map the window itself. Showing a withdrawn Tk window only through SetWindowPos left it
        # blank or invisible, which is why meetings got no popup. deiconify takes the keyboard, so without
        # `activate` it goes straight back to the meeting window.
        previous = 0 if activate else foreground_hwnd()
        self.root.deiconify()
        self.root.lift()
        self.root.update_idletasks()
        hwnd = top_hwnd(self.root)
        place_window(hwnd, x, y, WIDTH, HEIGHT, activate)
        self.root.update()
        if not activate and previous and previous != hwnd:
            give_focus_back(previous)
        log.info("Meeting popup shown at %d,%d%s.", x, y, "" if activate else " (meeting keeps the keyboard)")
        if activate:
            try:
                self.search.focus_set()
            except Exception:  # noqa: BLE001  the window is up even if focus is refused
                log.debug("Could not focus the client picker", exc_info=True)

    def _clicked(self, _event) -> None:
        """A click is the other way the window is allowed to take the keyboard."""
        if sys.platform != "win32":
            return
        import ctypes
        ctypes.windll.user32.SetForegroundWindow(top_hwnd(self.root))

    def _render(self) -> None:
        state = self.state
        if state is None:
            return
        browsing = state.mode == "browse"
        if browsing:
            self.prompt_var.set("Which client is this recording for? Type to search."
                                if self.heading else "Who is this call with? Type to search or name a new client.")
            self.list.pack(fill="both", expand=True)
            self._sync_list()
        else:
            self.prompt_var.set(state.prompt)
            self.list.pack_forget()
            self._ignore_query = True
            self.query_var.set(state.field_value)
            self._ignore_query = False
            self.search.focus_set()
            self.search.icursor("end")
        self.hint_var.set(state.hint)

    def _sync_list(self) -> None:
        state = self.state
        self.list.delete(0, "end")
        for row in state.rows:
            self.list.insert("end", row.label)
        self.list.selection_clear(0, "end")
        self.list.selection_set(state.selected)
        self.list.activate(state.selected)
        self.list.see(state.selected)
        self.hint_var.set(state.hint)

    def _on_query(self, *_args) -> None:
        if self._ignore_query or self.state is None or self.state.mode != "browse":
            return
        self.state.set_query(self.query_var.get())
        self._sync_list()

    def _on_list_click(self, _event) -> None:
        if self.state is None or self.state.mode != "browse":
            return
        picked = self.list.curselection()
        if picked:
            self.state.selected = picked[0]
            self.hint_var.set(self.state.hint)

    def _up(self, _event):
        if self.state is not None and self.state.mode == "browse":
            self.state.move(-1)
            self._sync_list()
        return "break"

    def _down(self, _event):
        if self.state is not None and self.state.mode == "browse":
            self.state.move(1)
            self._sync_list()
        return "break"

    def _no_key(self, event):
        if self.state is not None and self.state.mode == "duplicate" and not self.query_var.get():
            self._apply(self.state.answer_duplicate(False))
            return "break"
        return None

    def _enter_event(self, _event):
        self._enter()
        return "break"

    def _enter(self) -> None:
        state = self.state
        if state is None:
            return
        state.title = self.title_var.get()
        if state.mode == "duplicate":
            self._apply(state.answer_duplicate(True))
        elif state.mode in ("name", "person", "contact", "company", "note"):
            value = self.query_var.get()
            self._apply(state.submit_field(value))
        else:
            self._apply(state.confirm())

    def _escape(self) -> None:
        if self.state is not None and self.state.mode != "browse":
            self.state.back()                         # Esc in a name/detail step goes back to the list
            self._ignore_query = True
            self.query_var.set("")
            self._ignore_query = False
            self._render()
            return
        self._close()

    def _close(self) -> None:
        """The window's X, or Esc on the list: decide later. The tray icon brings the popup back."""
        if self.state is None:
            self.root.withdraw()
            self.shown = False
            return
        self._apply(self.state.cancel())

    def _apply(self, action) -> None:
        if action is None:
            self._render()
            return
        if isinstance(action, (AskDuplicate, AskField)):
            self._render()
            return
        if isinstance(action, (Select, Skip, Created, DecideLater)):
            try:
                self.on_result(self.session, action)
            except Exception:  # noqa: BLE001
                log.exception("Could not save the client choice")
            self.root.withdraw()
            self.shown = False

    def _record(self) -> None:
        """Save the picked client and title, hide the popup so it doesn't cover a participant tile,
        take the screenshot, open Meetily. Comes back only if no client was saved."""
        saved = self._save_pick_for_record()
        if saved:
            self.shown = False
        self.root.withdraw()
        self.root.update()
        self.root.after(350, self._record_now)

    def _save_pick_for_record(self) -> bool:
        """Record used to ignore the list and the title field; only Enter saved them."""
        state = self.state
        if state is None or self.session is None or state.mode != "browse" or not state.rows:
            return False
        row = state.rows[state.selected]
        if row.kind != "client" or not row.client:
            return False                              # create / unsorted / skip still go through Enter
        state.title = self.title_var.get()
        action = state.confirm()
        if not isinstance(action, Select):
            return False
        try:
            self.on_result(self.session, action)
        except Exception:  # noqa: BLE001
            log.exception("Could not save the client choice")
            return False
        log.info("Record clicked: saved client %s, title %r.", action.client, action.title)
        return True

    def _record_now(self) -> None:
        try:
            self.on_record(self.session)
        except Exception:  # noqa: BLE001
            log.exception("Could not record")
        if self.shown:
            self._place(activate=False)
