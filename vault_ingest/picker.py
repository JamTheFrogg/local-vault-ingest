"""Client picker decisions, with no window. The tkinter shell and the tests both drive this."""
from __future__ import annotations

from dataclasses import dataclass

from .clients import RANK_LIMIT, ClientInfo, Hit, create_client, near_duplicate, rank_clients, sanitize_name
from .sessions import Session, SessionStore

PERSONAL = "Personal"          # one client folder for calls that aren't work; the person's name goes in the title

FIELD_PROMPTS = {
    "name": "New client name",
    "person": "Who is the call with? (name, then Enter)",
    "contact": "Contact name (Enter to skip)",
    "company": "Company (Enter to skip)",
    "note": "One-line note (Enter to skip)",
}
_FIELDS = ("contact", "company", "note")


@dataclass(frozen=True)
class Row:
    kind: str               # client | create | personal | unsorted | skip
    label: str
    client: str | None = None
    score: float | None = None
    via: str | None = None


@dataclass(frozen=True)
class Select:
    client: str
    title: str


@dataclass(frozen=True)
class Skip:
    title: str


@dataclass(frozen=True)
class DecideLater:
    pass


@dataclass(frozen=True)
class AskDuplicate:
    existing: str
    typed: str


@dataclass(frozen=True)
class AskField:
    field: str


@dataclass(frozen=True)
class Created:
    name: str
    contact: str | None
    company: str | None
    note: str | None
    title: str


class PickerState:
    """Search box, top matches, and the three fixed rows. Enter confirms; Esc decides later."""

    def __init__(self, records: list[ClientInfo], preselect: str | None = None, title: str = "",
                 skip_label: str = "Don't ingest this meeting", person: str = ""):
        self.records = list(records)
        self.preselect = preselect or None
        self.query = ""
        self.title = title or ""
        self.skip_label = skip_label or "Don't ingest this meeting"
        self.selected = 0
        self.mode = "browse"          # browse | name | person | duplicate | contact | company | note
        self.person = (person or "").strip()      # a guess for the personal-call name (a WhatsApp contact)
        self.field_value = ""                     # what the text box starts with in a field step
        self.pending_name = ""
        self.duplicate_of: str | None = None
        self.contact: str | None = None
        self.company: str | None = None
        self.note: str | None = None
        self.rows: list[Row] = []
        self.refresh()

    def set_query(self, text: str) -> None:
        self.query = text
        self.selected = 0
        self.refresh()

    def move(self, delta: int) -> None:
        if self.mode != "browse" or not self.rows:
            return
        self.selected = max(0, min(len(self.rows) - 1, self.selected + delta))

    def refresh(self) -> None:
        hits = self._hits()
        rows = [Row("client", _hit_label(h), h.name, h.score, h.via) for h in hits]
        typed = self.query.strip()
        rows.append(Row("create", f"＋ New client: {typed}" if typed else "＋ New client…"))
        rows.append(Row("personal", f"♥ Personal: {typed}" if typed else "♥ Personal call (not a client)…", PERSONAL))
        rows.append(Row("unsorted", "Not a client call (Unsorted)", "Unsorted"))
        rows.append(Row("skip", self.skip_label))
        self.rows = rows
        if not self.query.strip() and self.preselect in ("Unsorted", PERSONAL):
            kind = "unsorted" if self.preselect == "Unsorted" else "personal"
            self.selected = next(i for i, row in enumerate(rows) if row.kind == kind)
        if self.selected >= len(rows):
            self.selected = len(rows) - 1

    def _hits(self) -> list[Hit]:
        hits = rank_clients(self.records, self.query, limit=RANK_LIMIT)
        if self.query.strip() or not self.preselect or self.preselect in ("Unsorted", PERSONAL):
            return hits
        if not any(r.name == self.preselect for r in self.records):
            return hits
        hits = [h for h in hits if h.name != self.preselect]
        hits.insert(0, Hit(self.preselect, 100.0, "name", self.preselect))
        return hits[:RANK_LIMIT]

    @property
    def hint(self) -> str:
        if self.mode != "browse" or not self.preselect or self.query.strip() or not self.rows:
            return ""
        row = self.rows[self.selected]
        if row.client == self.preselect and row.kind in ("client", "unsorted", "personal"):
            return "Enter to confirm"
        return ""

    @property
    def prompt(self) -> str:
        if self.mode == "duplicate" and self.duplicate_of:
            return f"Did you mean {self.duplicate_of}?"
        return FIELD_PROMPTS.get(self.mode, "")

    def back(self) -> None:
        """Esc in a field step: back to the list, nothing decided."""
        self.mode = "browse"
        self.duplicate_of = None
        self.field_value = ""
        self.refresh()

    def confirm(self) -> Select | Skip | AskDuplicate | AskField | None:
        if self.mode != "browse" or not self.rows:
            return None
        row = self.rows[self.selected]
        title = self.title.strip()
        if row.kind == "client" and row.client:
            return Select(row.client, title)
        if row.kind == "unsorted":
            return Select("Unsorted", title)
        if row.kind == "skip":
            return Skip(title)
        if row.kind == "create":
            if not self.query.strip():                    # nothing typed yet: ask for the name right here
                self.mode = "name"
                self.field_value = ""
                return AskField("name")
            return self._start_create(self.query)
        if row.kind == "personal":
            self.mode = "person"
            self.field_value = self.query.strip() or self.person
            return AskField("person")
        return None

    def _start_create(self, typed: str) -> AskDuplicate | AskField | None:
        typed = typed.strip()
        name = sanitize_name(typed)
        if not typed or not name or name == "Untitled":
            return None
        self.pending_name = name
        self.field_value = ""
        dup = near_duplicate(name, self.records)
        if dup:
            self.mode = "duplicate"
            self.duplicate_of = dup
            return AskDuplicate(dup, name)
        self.mode = "contact"
        return AskField("contact")

    def answer_duplicate(self, yes: bool) -> Select | AskField:
        title = self.title.strip()
        if yes and self.duplicate_of:
            client = self.duplicate_of
            self.mode = "browse"
            return Select(client, title)
        self.duplicate_of = None
        self.mode = "contact"
        return AskField("contact")

    def submit_field(self, value: str) -> AskField | AskDuplicate | Created | Select | None:
        if self.mode == "name":
            asked = self._start_create(value)
            return asked if asked is not None else AskField("name")       # empty name: keep asking
        if self.mode == "person":
            person = " ".join(value.split())
            if not person:
                return AskField("person")
            self.mode = "browse"
            self.field_value = ""
            title = self.title.strip()
            return Select(PERSONAL, f"{person} - {title}" if title and title != person else person)
        if self.mode not in _FIELDS:
            return None
        text = value.strip() or None
        if self.mode == "contact":
            self.contact = text
            self.mode = "company"
            return AskField("company")
        if self.mode == "company":
            self.company = text
            self.mode = "note"
            return AskField("note")
        self.note = text
        created = Created(self.pending_name, self.contact, self.company, self.note, self.title.strip())
        self.mode = "browse"
        return created

    def cancel(self) -> DecideLater:
        self.mode = "browse"
        return DecideLater()


def _hit_label(hit: Hit) -> str:
    if hit.via == "name":
        return hit.name
    return f"{hit.name}  ({hit.via}: {hit.matched})"


def apply_picker_result(action, session: Session | None, store: SessionStore | None, settings,
                        meeting_map) -> str | None:
    """Write a confirmed choice onto the session and the meeting map. Returns the client, if one was chosen."""
    title = (getattr(action, "title", None) or "").strip() or None
    if isinstance(action, Created):
        client = create_client(settings, action.name, contact=action.contact, company=action.company, note=action.note)
    elif isinstance(action, Select):
        client = action.client
    elif isinstance(action, Skip):
        if session is not None and store is not None:
            store.update(session, skip=True, client=None, call_title=title)
        return None
    else:
        return None
    if session is not None and store is not None:
        store.update(session, client=client, call_title=title, skip=False)
        key = session.meeting_key
        if key and client:
            meeting_map.put(key, client)
    return client
