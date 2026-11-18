"""The ingest pipeline: inbox file -> chunk notes + call note + updated client profile."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .chunker import Chunk, chunk_turns
from .config import Settings
from .gpu import SERVER_START_FAILED, GpuInterrupted
from .llm import LLM, ImageInput, LLMError, LLMTransient, LLMUnavailable, Prompts, VisionUnsupported
from .parsers import (AUDIO_EXTENSIONS, SUPPORTED_EXTENSIONS, Turn, format_turn, last_timestamp_seconds, parse_file,
                      parse_transcript_text, total_words, ts_seconds, turns_to_text)
from .transcribe import (TranscriberUnavailable, build_initial_prompt, glossary_terms, lookup_source,
                         media_seconds, read_glossary, transcribe_path)
from .schemas import (CallNote, ChunkDigest, ClientGuess, EvidenceCheck, ProfileUpdate,
                      ScreenshotDescription)
from .screenshots import created_ts
from .sessions import (Session, SessionStore, hms, hms_seconds, iso, match_session, parse_iso, to_local,
                       transcript_windows)
from .vault import (Registry, Vault, append_to_section, get_frontmatter, get_section, list_entries,
                    make_note, merge_open_items, normalize, open_items, replace_section, retract_lines,
                    sanitize_name, set_frontmatter_field, sha256_file, word_count)

log = logging.getLogger("vault_ingest")

LLM_ATTEMPTS = 3
LLM_RETRY_DELAYS = (30.0, 60.0, 120.0)
QUOTE_MATCH = 90                 # rapidfuzz partial ratio; an exact substring always counts
VARIANT_MATCH = 75               # Glossary term vs a Whisper token; needs a shared 2-letter prefix
AUDIO_MATCH_SECONDS = 600        # last timestamp is a line start, so the file may run several minutes longer
EVIDENCE_WINDOW_WORDS = 1500     # transcript words per evidence request, so a long call fits a small local context
EVIDENCE_BATCH = 8               # claims per evidence request, so the answer fits max_tokens
PENDING_RETRY_SECONDS = 300
PENDING_BACKOFF_SECONDS = (300, 900, 3600, 14400)   # 5 min, 15 min, 1 h, 4 h
PENDING_REVIEW_AFTER = 5
PROFILE_CONTEXT_RESERVE = 3200                       # len//4 undercounts, so this leaves room for the JSON answer
PHONE_NAME_RE = re.compile(r"^voice\s+\d+$", re.I)


def _backoff_phrase(seconds: int) -> str:
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return "1 hour" if hours == 1 else f"{hours} hours"
    if seconds % 60 == 0:
        return f"{seconds // 60} minutes"
    return f"{seconds} seconds"


def _approx_tokens(text: str) -> int:
    return len(text) // 4


# Work Log, Decisions Log, and Snapshot go first. Open items stay in their own list, so the copy inside
# the profile is next. Anything else in the profile goes last. The call note is not in this text.
_PROFILE_TRIM_FIRST = ("Work Log", "Decisions Log", "Snapshot", "Open Items", "Preferences & Watch-outs")


def _profile_trim_order(text: str) -> list[str]:
    found = re.findall(r"(?m)^## (.+?)\s*$", text)
    ordered = [heading for heading in _PROFILE_TRIM_FIRST if heading in found]
    ordered.extend(heading for heading in found if heading not in ordered)
    return ordered


def _drop_oldest_profile_line(text: str) -> str | None:
    """Drop one oldest profile bullet so the prompt can fit. The call note is not in this text."""
    for heading in _profile_trim_order(text):
        body = get_section(text, heading)
        if not body:
            continue
        entries = list_entries(body)
        if not entries:
            continue
        oldest = entries[0]
        lines = body.split("\n")
        for i, line in enumerate(lines):
            if line == oldest:
                updated = "\n".join(lines[:i] + lines[i + 1:]).strip()
                return replace_section(text, heading, updated)
    return None

FILENAME_RE = re.compile(r"^(?P<client>.+?)\s+-\s+(?P<date>\d{4}-\d{2}-\d{2})(?:\s+-\s+(?P<title>.+))?$")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

DEFAULT_TEMPLATE = """---
type: client
client: {{client}}
status: active
last_contact:
tags: [client]
---
# {{client}}

## Snapshot
Who they are, what they do, who you deal with, how they like to work.

## Current Status
No calls processed yet.

## Open Items

## Decisions Log

## Preferences & Watch-outs

## Work Log
"""


class NeedsReview(Exception):
    pass


class LeaveQueued(Exception):
    """The file stays in the inbox. Used when a phone recording is waiting on the client picker."""


# words that recorders put in auto-generated file names; a title made only of these is no title
GENERIC_TITLE_WORDS = {
    "zoom", "teams", "meet", "google", "otter", "fireflies", "fathom", "tldv", "recording", "rec", "audio",
    "video", "transcript", "transcription", "meeting", "call", "untitled", "new", "voice", "memo", "gmt",
    "utc", "am", "pm", "export", "final", "copy", "snimak", "sastanak", "poziv",
}


def created_stamp(path: Path) -> tuple[str, str]:
    """Local date and HH:MM from the file's creation time."""
    local = dt.datetime.fromtimestamp(created_ts(path.stat())).astimezone()
    return local.date().isoformat(), local.strftime("%H:%M")


def file_date_title(path: Path) -> tuple[str, str | None]:
    """Date and title the way `identify` reads them from a file name (or the file's creation time)."""
    stem = path.stem.strip()
    if PHONE_NAME_RE.match(stem):
        date, _clock = created_stamp(path)
        return date, None
    m = FILENAME_RE.match(stem)
    if m:
        title = clean_title(m.group("title")) if m.group("title") else None
        return m.group("date"), title
    dm = DATE_RE.search(stem)
    date = dm.group(1) if dm else created_stamp(path)[0]
    return date, clean_title(DATE_RE.sub(" ", stem))


def clean_title(raw: str) -> str | None:
    t = re.sub(r"[_]+", " ", raw)
    t = re.sub(r"\b\d{1,2}[.:h]\d{2}(?:[.:]\d{2})?\b", " ", t)      # times like 14.05 / 14:05:33
    t = sanitize_name(re.sub(r"\s*-\s*", " - ", t).strip(" -_"))
    tokens = [w for w in re.split(r"[\s\-]+", t.lower()) if w]
    if not tokens or all(w in GENERIC_TITLE_WORDS or w.isdigit() for w in tokens):
        return None
    return t


@dataclass
class Identity:
    client: str
    date: str
    title: str | None
    new_client: bool = False
    how: str = "filename"
    clock: str | None = None          # local HH:MM from the file's creation time (phone recordings)


SHOT_PURPOSE = {
    "": "It was taken by hand during the call.",
    "participants": ("It is a picture of the whole desktop taken when recording started, so that the people in the "
                     "meeting can be identified. Read every participant name shown in the meeting window (video "
                     "tiles, name labels, the participants list, the call window title) into `people`."),
}


@dataclass
class Shot:
    """A screenshot from the linked meeting session."""
    source: Path                      # the copy in _System/screenshots
    timestamp: str
    seconds: int                      # offset from the meeting start
    desc: ScreenshotDescription | None = None
    attachment: Path | None = None    # copy next to the call note
    kind: str = ""                    # "participants": taken by the companion when Record was clicked

    @property
    def offset(self) -> str:
        return hms(self.seconds)

    @property
    def caption(self) -> str:
        return self.desc.caption.strip() if self.desc and self.desc.caption.strip() else "Screenshot"

    @property
    def visible_text(self) -> str:
        return " ".join(self.desc.visible_text.split()) if self.desc else ""

    @property
    def people(self) -> list[str]:
        return list(self.desc.people) if self.desc else []


def chunk_for_offset(chunks: list[Chunk], seconds: int) -> int:
    """Index of the transcript part covering `seconds` into the call (the last part starting at or before it)."""
    idx = 0
    for i, c in enumerate(chunks):
        start = ts_seconds(c.start)
        if start is not None and start <= seconds:
            idx = i
    return idx


def first_words(text: str, n: int = 150) -> str:
    body = re.sub(r"^---\r?\n.*?\r?\n---\r?\n", "", text, count=1, flags=re.S)
    words = body.split()
    return " ".join(words[:n]) if words else "(none)"


def me_excerpt(vault: Path, n: int = 150) -> str:
    path = vault / "Me.md"
    if not path.is_file():
        return "(none)"
    try:
        return first_words(path.read_text(encoding="utf-8"), n)
    except OSError:
        return "(none)"


def snapshot_excerpt(profile: str) -> str:
    text = (get_section(profile, "Snapshot") or "").strip()
    return text or "(none)"


@dataclass
class VerifiedQuotes:
    decisions: list[tuple[str, str]] = field(default_factory=list)
    actions: list[tuple[str, str]] = field(default_factory=list)
    numbers: list[tuple[str, str]] = field(default_factory=list)


def fold_text(text: str) -> str:
    """Lowercase, collapse whitespace, and drop diacritics."""
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", folded).casefold().strip()


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", fold_text(text), flags=re.UNICODE)


def glossary_names(glossary: str) -> list[str]:
    """Folded Glossary terms, plus each word of a term that is at least 6 characters."""
    names: list[str] = []
    for term in glossary_terms(glossary):
        folded = fold_text(term)
        if len(folded) >= 6 and folded not in names:
            names.append(folded)
        for word in folded.split():
            if len(word) >= 6 and word not in names:
                names.append(word)
    return names


def spelling_variants(glossary: str, *texts: str) -> dict[str, str]:
    """Map a misspelled token to the Glossary term it stands for (Infinoks -> infinox)."""
    from rapidfuzz.fuzz import ratio

    names = glossary_names(glossary)
    found: dict[str, str] = {}
    for text in texts:
        for token in _words(text):
            if len(token) < 6 or token in names or token in found:
                continue
            best: str | None = None
            best_score = 0.0
            for name in names:
                if token[:2] != name[:2]:
                    continue
                score = ratio(token, name)
                if score >= VARIANT_MATCH and score > best_score:
                    best, best_score = name, score
            if best:
                found[token] = best
    return found


def _canon(text: str, variants: dict[str, str]) -> str:
    folded = fold_text(text)

    def repl(match: re.Match) -> str:
        return variants.get(match.group(0), match.group(0))

    return re.sub(r"[^\W_]+", repl, folded, flags=re.UNICODE)


def quote_in_text(quote: str, text: str, glossary: str = "") -> bool:
    """True when `quote` appears in `text`, allowing diacritics and Glossary spelling variants."""
    variants = spelling_variants(glossary, text, quote)
    q = _canon(quote, variants)
    blob = _canon(text, variants)
    if not q:
        return False
    if q in blob:
        return True
    if len(q) < 12:
        return False
    from rapidfuzz.fuzz import partial_ratio

    return partial_ratio(q, blob) >= QUOTE_MATCH


def is_glossary_entity(claim: str, glossary: str) -> bool:
    """True when the claim, or the text after its last colon, is itself a Glossary term."""
    names = set(glossary_names(glossary))
    if not names:
        return False
    if fold_text(claim) in names:
        return True
    if ":" in (claim or ""):
        return fold_text(claim.rsplit(":", 1)[-1]) in names
    return False


def _unverified(text: str) -> str:
    text = (text or "").rstrip()
    if text.endswith("(unverified)"):
        return text
    return f"{text} (unverified)"


def _same_claim(claim: str, item: str) -> bool:
    a, b = normalize(claim), normalize(item)
    if not a or not b:
        return False
    if a == b or (min(len(a), len(b)) > 12 and (a in b or b in a)):
        return True
    from rapidfuzz.fuzz import ratio

    return ratio(a, b) >= 88


def evidence_windows(transcript: str, max_words: int) -> list[str]:
    """Split the transcript text on line boundaries into pieces of at most max_words words.

    A recording without speaker labels can arrive as one very long line, so a line that is longer
    than a window is cut by words.
    """
    pieces: list[str] = []
    for line in transcript.split("\n"):
        words = line.split()
        if len(words) <= max_words:
            pieces.append(line)
        else:
            pieces += [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]
    windows: list[str] = []
    current: list[str] = []
    count = 0
    for piece in pieces:
        n = len(piece.split())
        if current and count + n > max_words:
            windows.append("\n".join(current).strip())
            current, count = [], 0
        current.append(piece)
        count += n
    if current:
        windows.append("\n".join(current).strip())
    return [w for w in windows if w] or [""]


def merge_evidence(into: EvidenceCheck, found: EvidenceCheck) -> EvidenceCheck:
    """Collect the quotes found in each transcript window. Empty quotes carry no information."""
    return EvidenceCheck(
        decisions=into.decisions + [c for c in found.decisions if c.quote.strip()],
        action_items=into.action_items + [c for c in found.action_items if c.quote.strip()],
        numbers=into.numbers + [c for c in found.numbers if c.quote.strip()],
    )


def apply_evidence(note: CallNote, check: EvidenceCheck, transcript: str,
                   glossary: str = "") -> tuple[CallNote, VerifiedQuotes]:
    """Keep decisions and action items. Mark a failed quote "(unverified)", except a Glossary name.

    A notable detail whose quote does not match is removed, unless it is a Glossary entity.
    """
    note = note.model_copy(deep=True)
    quotes = VerifiedQuotes()

    def hit_for(claims, *labels: str):
        for i, claim in enumerate(claims):
            if i in used:
                continue
            if any(_same_claim(claim.claim, label) for label in labels) and quote_in_text(
                    claim.quote, transcript, glossary):
                return i, claim
        return None

    used: set[int] = set()
    decisions = []
    for item in note.decisions:
        hit = hit_for(check.decisions, item)
        if hit:
            used.add(hit[0])
            decisions.append(item)
            quotes.decisions.append((item, " ".join(hit[1].quote.split())))
        elif is_glossary_entity(item, glossary):
            decisions.append(item)
        else:
            decisions.append(_unverified(item))
    note.decisions = decisions

    used = set()
    actions = []
    for item in note.action_items:
        hit = hit_for(check.action_items, item.task, item.as_line())
        if hit:
            used.add(hit[0])
            actions.append(item)
            quotes.actions.append((item.task, " ".join(hit[1].quote.split())))
        elif is_glossary_entity(item.task, glossary):
            actions.append(item)
        else:
            actions.append(item.model_copy(update={"task": _unverified(item.task)}))
    note.action_items = actions

    used = set()
    numbers = []
    for item in note.notable_details:
        hit = hit_for(check.numbers, item)
        if hit:
            used.add(hit[0])
            numbers.append(item)
            quotes.numbers.append((item, " ".join(hit[1].quote.split())))
        elif is_glossary_entity(item, glossary):
            numbers.append(item)
    note.notable_details = numbers
    return note, quotes


def evidence_groups(decisions: list[str], actions: list, numbers: list[str],
                    size: int = EVIDENCE_BATCH) -> list[tuple[list, list, list]]:
    """Split claims into groups of at most `size` so each answer fits the token cap."""
    items: list[tuple[str, object]] = [("d", d) for d in decisions]
    items += [("a", a) for a in actions]
    items += [("n", n) for n in numbers]
    groups = []
    for start in range(0, len(items), size):
        chunk = items[start:start + size]
        groups.append(([x for kind, x in chunk if kind == "d"],
                       [x for kind, x in chunk if kind == "a"],
                       [x for kind, x in chunk if kind == "n"]))
    return groups


def quotes_verified(check: EvidenceCheck) -> int:
    rows = (*check.decisions, *check.action_items, *check.numbers)
    return sum(1 for row in rows if (row.quote or "").strip())


def evidence_callout(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return ""
    lines = ["> [!quote]- Evidence"]
    for claim, quote in pairs:
        q = " ".join(quote.split()).replace('"', "'")
        lines.append(f'> - {claim}: "{q}"')
    return "\n".join(lines)


@dataclass
class Result:
    source: Path
    status: str                       # processed | review | skipped | planned | waiting (left in inbox)
    client: str | None = None
    call_note: Path | None = None
    chunk_notes: list[Path] = field(default_factory=list)
    message: str = ""
    planned: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, s: Settings, llm: LLM | None, dry_run: bool = False, concurrency: int = 4,
                 models=None):
        self.s = s
        self.llm = llm
        self.dry_run = dry_run
        self.concurrency = max(1, concurrency)
        self.models = models              # ModelManager; loads the chat model for the LLM stage
        self.gpu = None                   # GpuService, when the watcher or `run` started one
        self.vault = Vault(s.vault_path, (s.inbox_dir, s.clients_dir, s.system_dir))
        self.prompts = Prompts(s.prompts_dir)
        self.registry = Registry(self.vault, s.system / "ingest-log.json")
        self.vision_ok = True             # off for the rest of the run once the model turns out to be text-only
        self.llm_retry_delays = LLM_RETRY_DELAYS
        self.announce = False            # toasts, on for `run` and `watch`
        self.track_status = False        # writes _System/ingest-status.json
        self.current_file: str | None = None
        self.step: str | None = None
        self.last_error = ""
        self._claimed: set[str] = set()

    # ================================================================ helpers
    @property
    def generated_by(self) -> str:
        """'<mode>/<model>' of the LLM that wrote the notes (the resolved model once LM Studio auto-picked one)."""
        model = getattr(self.llm, "model", None) or getattr(self.llm, "name", None) or self.s.llm_model
        return f"{self.s.effective_mode}/{model}"

    def _log_llm(self, label: str) -> None:
        st = getattr(self.llm, "last_stats", None)
        if st is None:
            return
        reasoning = f", {st.reasoning_tokens} reasoning tokens" if st.reasoning_tokens else ""
        log.info("%s done in %.0f s, %d tokens%s, %.0f tok/s",
                 label, st.seconds, st.output_tokens, reasoning, st.tokens_per_sec)

    def today(self) -> str:
        return dt.date.today().isoformat()

    def existing_clients(self) -> list[str]:
        if not self.s.clients.exists():
            return []
        return sorted(p.name for p in self.s.clients.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))

    def match_client(self, name: str) -> str | None:
        n = normalize(name)
        for c in self.existing_clients():
            if normalize(c) == n:
                return c
        return None

    def profile_path(self, client: str) -> Path:
        return self.s.clients / client / f"{client}.md"

    def _under(self, path: Path, folder: Path) -> bool:
        try:
            path.resolve().relative_to(folder.resolve())
        except ValueError:
            return False
        return True

    def _transcribed_by(self) -> str:
        return f"faster-whisper/{self.s.transcribe_model}/{self.s.transcribe_language}"

    def _transcribe(self, path: Path) -> tuple[str, str]:
        glossary = read_glossary(self.s.system / "Glossary.md")
        prompt = build_initial_prompt(glossary, self.existing_clients())
        result = transcribe_path(
            path, model_name=self.s.transcribe_model, device=self.s.transcribe_device,
            language=self.s.transcribe_language, initial_prompt=prompt, me_name=self.s.me_name)
        log.info("Transcribed %s in %.0fs (%s, %s/%s)", path.name, result.seconds, result.transcribed_by,
                 result.device, result.compute_type)
        return result.text, result.transcribed_by

    def set_step(self, filename: str | None, step: str | None, error: str | None = None) -> None:
        self.current_file = filename
        self.step = step
        if error:
            self.last_error = error
        if not self.track_status:
            return
        payload = {
            "pid": os.getpid(),
            "file": filename or "",
            "step": step or "",
            "last_error": self.last_error or "",
            "updated": dt.datetime.now().isoformat(timespec="seconds"),
        }
        try:
            self.vault.write(self.s.system / "ingest-status.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        except Exception:  # noqa: BLE001
            log.debug("Could not write ingest status", exc_info=True)

    def _note_unavailable(self, exc: BaseException) -> None:
        if SERVER_START_FAILED in str(exc):
            self._toast(SERVER_START_FAILED)
            self.set_step(self.current_file, "summary", str(exc))

    def _toast(self, text: str, *, open_path: Path | None = None, click_file: Path | None = None) -> None:
        if not self.announce:
            return
        from .gpu import show_toast
        show_toast(text, open_path=str(open_path) if open_path else None,
                   click_file=str(click_file) if click_file else None)

    def _cached_transcript(self, digest: str) -> Path | None:
        folder = self.s.system / "transcripts"
        if not folder.is_dir():
            return None
        marker = f"[{digest[:12]}].txt"
        for path in folder.iterdir():
            if path.is_file() and path.name.endswith(marker) and path.stat().st_size > 0:
                return path
        return None

    def _duplicate_reason(self, path: Path, digest: str) -> str | None:
        prior = self.registry.get(digest)
        if prior:
            return f"Already processed on {prior.get('processed_at', '?')} as {prior.get('call_note')}."
        own = f"{sanitize_name(path.stem)} [{digest[:12]}].txt"
        cached = self._cached_transcript(digest)
        if cached is not None and cached.name != own:
            return f"Already transcribed as {self.vault.rel(cached)} (same recording)."
        if digest in self._claimed:
            return "Another copy of this recording is already queued."
        return None

    def inbox_files(self) -> list[Path]:
        if not self.s.inbox.exists():
            return []
        return sorted(p for p in self.s.inbox.iterdir()
                      if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
                      and not p.name.startswith((".", "~$")))

    # ================================================================ identify
    def identify(self, path: Path, turns: list[Turn]) -> Identity:
        stem = path.stem.strip()
        if PHONE_NAME_RE.match(stem):
            if self.dry_run:
                raise NeedsReview("Phone recording: a client picker would open.")
            return self._identify_phone(path)
        m = FILENAME_RE.match(stem)
        # "<Meeting title> - YYYY-MM-DD" (e.g. a Meetily export) names no client unless it is an existing one,
        # so only the full "<Client> - YYYY-MM-DD - <Title>" form creates a new client folder
        if m:
            raw_client = m.group("client").strip()
            client = self.match_client(raw_client)
            title = clean_title(m.group("title")) if m.group("title") else None
            if client:
                return Identity(client, m.group("date"), title)
            if m.group("title"):
                return Identity(sanitize_name(raw_client), m.group("date"), title, new_client=True)

        dm = DATE_RE.search(stem)
        date = dm.group(1) if dm else created_stamp(path)[0]
        title = clean_title(DATE_RE.sub(" ", stem))

        clients = self.existing_clients()
        if not clients:
            raise NeedsReview("No client folders exist yet and the file name doesn't name a client. "
                              "Rename it to '<Client> - YYYY-MM-DD - <Title>' and move it back to the inbox.")
        if self.llm is None:
            raise NeedsReview("Client not in file name (dry run: the LLM would try to identify it).")

        excerpt_words = turns_to_text(turns).split()[:1500]
        system, user = self.prompts.render(
            "identify_client", clients="\n".join(f"- {c}" for c in clients),
            filename=path.name, excerpt=" ".join(excerpt_words))
        guess = self.llm.complete_json(system, user, ClientGuess)
        client = self.match_client(guess.client) if guess.client else None
        if not client or guess.confidence < self.s.client_confidence:
            raise NeedsReview(f"Could not confidently identify the client "
                              f"(guess: {guess.client!r}, confidence {guess.confidence:.2f}). {guess.reason}")
        return Identity(client, date, title, how=f"llm ({guess.confidence:.2f})")

    def _identify_phone(self, path: Path) -> Identity:
        """A phone memo has no client in the name and no speaker labels. Ask, and use the creation time."""
        date, clock = created_stamp(path)
        choice = self._ask_phone_client(path)
        if choice is None:
            raise LeaveQueued(f"Choose a client for {path.name}")
        if choice == "skip":
            raise NeedsReview("Don't ingest this meeting.")
        client, title = choice if isinstance(choice, tuple) else (choice, None)
        if title:
            title = sanitize_name(title)
        return Identity(client, date, title or None, new_client=self.match_client(client) is None,
                        how="picker", clock=clock)

    def _ask_phone_client(self, path: Path) -> str | tuple[str, str | None] | None:
        """A client name, (client, title), 'skip', or None when the user has not chosen yet."""
        picker = getattr(self, "phone_client", None)
        if picker is not None:
            return picker(path)
        try:
            return self._picker_choice(path)
        except Exception as e:  # noqa: BLE001
            log.warning("Client picker unavailable (%s); asking with a toast.", e)
            return self._picker_from_toast(path)

    def _picker_choice(self, path: Path) -> str | tuple[str, str | None] | None:
        from .clients import MeetingMap, load_clients
        from .picker import Created, DecideLater, Select, Skip, apply_picker_result
        from .picker_ui import PickerWindow
        from .sessions import Session, now_local

        chosen: dict = {}
        done = threading.Event()
        meeting_map = MeetingMap(self.s.system / "meeting-map.json", self.vault)

        def on_result(_session, action) -> None:
            try:
                if isinstance(action, DecideLater):
                    chosen["later"] = True
                elif isinstance(action, Skip):
                    chosen["skip"] = True
                else:
                    client = apply_picker_result(action, None, None, self.s, meeting_map)
                    if isinstance(action, (Select, Created)) and client:
                        chosen["client"] = client
                        chosen["title"] = (getattr(action, "title", None) or "").strip() or None
            finally:
                done.set()

        window = PickerWindow(lambda: load_clients(self.s.clients), on_result, lambda _s: None, meeting_map)
        session = Session(id=path.stem, app="Phone", window_title=path.name, start=now_local(),
                          meeting_key=f"phone:{path.name}")
        window.show(session, activate=True)
        done.wait()
        window.close()
        if chosen.get("skip"):
            return "skip"
        if chosen.get("later") or not chosen.get("client"):
            return None
        return chosen["client"], chosen.get("title")

    def _picker_from_toast(self, path: Path) -> str | tuple[str, str | None] | None:
        click = self.s.system / f"picker-click-{os.getpid()}.flag"
        if click.exists():
            click.unlink()
        self._toast(f"Choose a client for {path.name}", click_file=click)
        if not self.announce:
            return None
        deadline = time.time() + 600
        while time.time() < deadline and not click.exists():
            time.sleep(0.5)
        if not click.exists():
            return None
        click.unlink(missing_ok=True)
        try:
            return self._picker_choice(path)
        except Exception:  # noqa: BLE001
            log.exception("Client picker did not open from the toast")
            return None

    # ================================================================ meeting session (companion)
    def session_for_transcript(self, path: Path, turns: list[Turn]) -> Session | None:
        """The companion's meeting session this transcript was recorded in, by date and time overlap."""
        date, _title = file_date_title(path)
        try:
            day = dt.date.fromisoformat(date)
        except ValueError:
            return None
        sessions = SessionStore(self.s.system / "meetings").load(
            [(day - dt.timedelta(days=1)).isoformat(), date])
        if not sessions:
            return None
        st = path.stat()
        windows = transcript_windows(to_local(created_ts(st)), to_local(st.st_mtime), last_timestamp_seconds(turns))
        return match_session(sessions, date, windows, self.s.meeting_match_tolerance_minutes * 60)

    def find_session(self, path: Path, ident: Identity, turns: list[Turn]) -> Session | None:
        return self.session_for_transcript(path, turns)

    def identity_from_picker(self, path: Path, session: Session) -> Identity:
        date, title = file_date_title(path)
        if (session.call_title or "").strip():
            titled = sanitize_name(session.call_title.strip())
            if titled and titled != "Untitled":
                title = titled
        return Identity(session.client, date, title, new_client=self.match_client(session.client) is None,
                        how="picker")

    def skip_meeting(self, path: Path) -> Result:
        """Don't ingest: nothing from this meeting is filed. The inbox file is set aside so the watcher stops."""
        msg = "Don't ingest this meeting."
        if not self.dry_run and path.exists() and self._under(path, self.s.inbox):
            self.vault.move(path, self.s.system / "skipped" / path.name)
            log.info("Skipped %s (don't ingest).", path.name)
        return Result(path, "skipped", message=msg)

    def session_shots(self, session: Session) -> list[Shot]:
        shots = []
        for e in session.screenshots:
            rel = e.get("path") or e.get("file")
            src = self.s.vault_path / str(rel or "")
            if not rel or not src.is_file():
                log.warning("Screenshot %s from meeting session %s is missing; skipped.", rel, session.id)
                continue
            sec = hms_seconds(e.get("offset"))
            if sec is None:
                taken = parse_iso(e.get("timestamp"))
                sec = max(0, int((taken - session.start).total_seconds())) if taken else 0
            shots.append(Shot(src, str(e.get("timestamp") or ""), sec, kind=str(e.get("kind") or "")))
        return sorted(shots, key=lambda x: x.seconds)

    def describe_screenshots(self, ident: Identity, call_title: str, shots: list[Shot]) -> None:
        """Vision step. A model without image support is not an error: the images are attached undescribed."""
        if not shots or self.llm is None or not self.vision_ok:
            return
        for shot in shots:
            system, user = self.prompts.render(
                "screenshot", language=self.s.notes_language, client=ident.client, call_title=call_title,
                date=ident.date, offset=shot.offset, purpose=SHOT_PURPOSE.get(shot.kind, SHOT_PURPOSE[""]))
            try:
                shot.desc = self.llm.complete_json_image(system, user, ImageInput.from_file(shot.source),
                                                         ScreenshotDescription)
            except VisionUnsupported as e:
                log.info("Screenshots are attached without descriptions: %s", e)
                self.vision_ok = False
                return
            except (LLMUnavailable, GpuInterrupted):
                raise
            except Exception as e:  # noqa: BLE001  one unreadable screenshot must not fail the call
                log.warning("Could not describe screenshot %s: %s", shot.source.name, str(e)[:300])

    @staticmethod
    def screenshots_context(shots: list[Shot]) -> str:
        if not shots:
            return "(No screenshots were captured.)"
        lines = []
        for sh in shots:
            if sh.desc:
                text = f"; text: {sh.visible_text}" if sh.visible_text else ""
                people = f"; people in the meeting window: {', '.join(sh.people)}" if sh.people else ""
                lines.append(f"- {sh.offset}: {sh.caption}{text}{people}")
            else:
                lines.append(f"- {sh.offset}: a screenshot was taken (no description available)")
        return "\n".join(lines)

    def shot_line(self, shot: Shot) -> str:
        text = f" ({shot.visible_text})" if shot.visible_text else ""
        return f"📷 {self.vault.link(shot.attachment)} — {shot.caption}{text}"

    # ================================================================ LLM steps
    def digest_chunks(self, ident: Identity, call_title: str, chunks: list[Chunk]) -> list[ChunkDigest]:
        def one(i_chunk: tuple[int, Chunk]) -> ChunkDigest:
            i, ch = i_chunk
            system, user = self.prompts.render(
                "chunk_digest", language=self.s.notes_language, client=ident.client,
                call_title=call_title, date=ident.date, index=str(i + 1), total=str(len(chunks)),
                time_range=f"{ch.start or '?'}–{ch.end or '?'}", chunk_text=ch.text())
            digest = self.llm.complete_json(system, user, ChunkDigest)
            self._log_llm(f"chunk {i + 1}/{len(chunks)} digest")
            return digest

        with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            return list(ex.map(one, enumerate(chunks)))

    def write_call_note(self, ident: Identity, call_title: str, turns: list[Turn], chunks: list[Chunk],
                        digests: list[ChunkDigest], shots: list[Shot] | None = None) -> CallNote:
        if total_words(turns) <= self.s.direct_summary_max_words:
            label, source = "Full transcript:", turns_to_text(turns)
        else:
            label = "The transcript was long, so here are per-chunk digests in order:"
            source = "\n\n".join(
                f"Chunk {i + 1} ({c.start or '?'}–{c.end or '?'}): {d.context}\n" + "\n".join(f"- {p}" for p in d.points)
                for i, (c, d) in enumerate(zip(chunks, digests)))
        system, user = self.prompts.render(
            "call_note", language=self.s.notes_language, client=ident.client, call_title=call_title,
            date=ident.date, source_label=label, source_text=source,
            screenshots=self.screenshots_context(shots or []), **self._speaker_context(ident.client))
        note = self.llm.complete_json(system, user, CallNote)
        self._log_llm("call note")
        return note

    def _speaker_context(self, client: str) -> dict[str, str]:
        path = self.profile_path(client)
        profile = path.read_text(encoding="utf-8") if path.is_file() else ""
        return {"me_name": self.s.me_name, "me": me_excerpt(self.s.vault_path),
                "snapshot": snapshot_excerpt(profile)}

    def check_evidence(self, ident: Identity, note: CallNote, turns: list[Turn]) -> tuple[CallNote, VerifiedQuotes]:
        """One cheap pass: drop claims whose quote is not in the transcript."""
        if not (note.decisions or note.action_items or note.notable_details):
            return note, VerifiedQuotes()
        transcript = turns_to_text(turns)

        def lines(items: list[str]) -> str:
            return "\n".join(f"- {item}" for item in items) or "(none)"

        # A long call does not fit one request on a small local context window, so the transcript is
        # checked in windows. Claims go out in groups of at most 8 so the answer is not cut off.
        windows = evidence_windows(transcript, EVIDENCE_WINDOW_WORDS)
        groups = evidence_groups(note.decisions, note.action_items, note.notable_details)
        requests = [(group, part) for group in groups for part in windows]
        check = EvidenceCheck()
        try:
            for n, ((decs, acts, nums), part) in enumerate(requests, start=1):
                actions = "\n".join(f"- {item.as_line()}" for item in acts) or "(none)"
                system, user = self.prompts.render(
                    "evidence", client=ident.client, transcript=part,
                    decisions=lines(decs), action_items=actions, numbers=lines(nums))
                found = self.llm.complete_json(system, user, EvidenceCheck)
                verified = quotes_verified(found)
                label = f"evidence check {n}/{len(requests)}"
                self._log_llm(label)
                log.info("%s: %d quotes verified", label, verified)
                check = merge_evidence(check, found)
        except (LLMTransient, LLMUnavailable, GpuInterrupted):
            raise
        except LLMError as e:
            # The evidence pass is a quality check. A call note without verified quotes is better than no note.
            log.warning("Evidence check failed (%s); keeping the call note without verified quotes.", e)
            return note, VerifiedQuotes()
        glossary = read_glossary(self.s.system / "Glossary.md")
        return apply_evidence(note, check, transcript, glossary)

    # ================================================================ rendering
    def render_screenshots(self, shots: list[Shot]) -> str:
        out = []
        for sh in shots:
            out += [f"- [{sh.offset} into the call] {sh.caption}", "", f"!{self.vault.link(sh.attachment)}", ""]
            if sh.people:
                out += [f"  People on screen: {', '.join(sh.people)}", ""]
            text = sh.desc.visible_text.strip() if sh.desc else ""
            if text:
                out += ["> [!note]- Text in image", *(f"> {line}" for line in text.splitlines() if line.strip()), ""]
        return "\n".join(out).strip()

    def render_call_note(self, ident: Identity, call_name: str, note: CallNote, chunk_paths: list[Path],
                         raw_rel: str, shots: list[Shot] | None = None, session: Session | None = None,
                         audio_source: str | None = None, transcribed_by: str | None = None,
                         quotes: VerifiedQuotes | None = None, audio_sha256: str | None = None) -> str:
        def bullets(items: list[str], empty: str = "_None._") -> str:
            return "\n".join(f"- {x}" for x in items) if items else empty

        def with_quotes(body: str, pairs: list[tuple[str, str]]) -> str:
            callout = evidence_callout(pairs)
            return f"{body}\n\n{callout}" if callout else body

        q = quotes or VerifiedQuotes()
        profile_link = self.vault.link(self.profile_path(ident.client), ident.client)
        actions = with_quotes("\n".join(f"- {a.as_line()}" for a in note.action_items) or "_None._", q.actions)
        chunks = "\n".join(f"- {self.vault.link(p, f'Part {i + 1}')}" for i, p in enumerate(chunk_paths))
        body = f"""
# {call_name}

**Client:** {profile_link} · **Date:** {ident.date if not ident.clock else f"{ident.date} {ident.clock}"}

## Summary
{note.summary.strip()}

## Key Points
{bullets(note.key_points)}

## Decisions
{with_quotes(bullets(note.decisions), q.decisions)}

## Action Items
{actions}

## Open Questions
{bullets(note.open_questions)}

## Notable Details
{with_quotes(bullets(note.notable_details), q.numbers)}
"""
        if shots:
            body += f"\n## Screenshots\n{self.render_screenshots(shots)}\n"
        body += f"\n## Transcript\n{chunks}\n"
        fm = {"type": "call", "client": ident.client, "date": ident.date,
              "participants": note.participants, "chunks": len(chunk_paths), "source": raw_rel}
        if session is not None:
            end = session.effective_end
            fm.update({"meeting_session": self.vault.rel(session.path) if session.path else session.id,
                       "meeting_start": iso(session.start), "meeting_end": iso(end),
                       "duration": hms((end - session.start).total_seconds()),
                       "screenshot_count": len(shots or [])})
        fm.update({"generated_by": self.generated_by, "tags": ["call"]})
        if audio_source:
            fm["audio_source"] = audio_source
        if audio_sha256:
            fm["audio_sha256"] = audio_sha256
        if transcribed_by:
            fm["transcribed_by"] = transcribed_by
        return make_note(fm, body)

    def turns_with_shots(self, turns: list[Turn], shots: list[Shot]) -> str:
        """Transcript text with a screenshot line after the turn that was going on when it was taken."""
        if not shots:
            return turns_to_text(turns)
        pending = sorted(shots, key=lambda x: x.seconds)
        blocks = []
        for t in turns:
            start = ts_seconds(t.start)
            if start is not None:
                while pending and pending[0].seconds < start:
                    blocks.append(self.shot_line(pending.pop(0)))
            blocks.append(format_turn(t))
        blocks += [self.shot_line(x) for x in pending]
        return "\n\n".join(blocks)

    def render_chunk(self, ident: Identity, call_path: Path, call_name: str, i: int, chunks: list[Chunk],
                     digest: ChunkDigest | None, raw_rel: str, chunk_paths: list[Path],
                     shots: list[Shot] | None = None, transcribed_by: str | None = None) -> str:
        ch = chunks[i]
        fm = {"type": "transcript-chunk", "client": ident.client, "date": ident.date,
              "call": self.vault.link(call_path), "chunk": i + 1, "of": len(chunks),
              "time_start": ch.start, "time_end": ch.end, "speakers": ch.speakers, "source": raw_rel,
              "generated_by": self.generated_by}
        if transcribed_by:
            fm["transcribed_by"] = transcribed_by
        parts = [f"\n> Context: {digest.context.strip()}" if digest else "\n> Context: (pending)"]
        if digest and digest.points:
            parts.append("\n".join(f"- {p}" for p in digest.points))
        nav = []
        if i > 0:
            nav.append(f"← {self.vault.link(chunk_paths[i - 1], f'Part {i}')}")
        nav.append(self.vault.link(call_path, call_name))
        if i < len(chunks) - 1:
            nav.append(f"{self.vault.link(chunk_paths[i + 1], f'Part {i + 2}')} →")
        parts.append("---")
        if ch.overlap:
            parts.append("_(continued from previous part)_\n\n" + turns_to_text(ch.overlap) + "\n\n---")
        parts.append(self.turns_with_shots(ch.turns, shots or []))
        parts.append("---\n" + " · ".join(nav))
        return make_note(fm, "\n\n".join(parts))

    # ================================================================ profile
    def ensure_client(self, client: str) -> Path:
        path = self.profile_path(client)
        if path.exists():
            return path
        tpl_path = self.s.vault_path / self.s.template_path
        tpl = tpl_path.read_text(encoding="utf-8") if tpl_path.exists() else DEFAULT_TEMPLATE
        tpl = tpl.replace("{{client}}", client).replace("{{title}}", client)
        # template example has "client: Acme" / "# Acme" style placeholders -> swap in the name
        tpl = re.sub(r"^client:.*$", f"client: {client}", tpl, count=1, flags=re.M)
        tpl = re.sub(r"^# .*$", f"# {client}", tpl, count=1, flags=re.M)
        for sub in ("Calls", "Transcripts", "Work"):
            self.vault.mkdir(self.s.clients / client / sub)
        self.vault.write(path, tpl)
        log.info("Created new client folder: %s", client)
        return path

    def backup(self, path: Path) -> None:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.s.system / "backups" / f"{path.stem} {stamp}{path.suffix}"
        self.vault.check(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

    def _status_call_notes(self, ident: Identity, new_md: str, replacing: Path | None) -> str:
        """Every call note for this client, oldest first. `new_md` takes the place of `replacing`."""
        folder = self.s.clients / ident.client / "Calls"
        rows: list[tuple[str, str, str]] = []
        skip = replacing.resolve() if replacing is not None else None
        if folder.is_dir():
            for path in folder.glob("*.md"):
                if skip is not None and path.resolve() == skip:
                    continue
                text = path.read_text(encoding="utf-8")
                stamp = str(get_frontmatter(text).get("date") or "")
                rows.append((stamp, path.name, text.strip()))
        rows.append((ident.date, "\uffff", new_md.strip()))
        rows.sort(key=lambda row: (row[0], row[1]))
        blocks = []
        for stamp, name, text in rows:
            label = "This call" if name == "\uffff" else name
            blocks.append(f"### {stamp} {label}\n\n{text}")
        return "\n\n".join(blocks)

    def plan_profile_update(self, ident: Identity, call_name: str, note: CallNote,
                            quotes: VerifiedQuotes | None = None, reprocess: bool = False,
                            replacing: Path | None = None) -> ProfileUpdate:
        """LLM step for the profile (runs before anything is written)."""
        path = self.profile_path(ident.client)
        text = path.read_text(encoding="utf-8") if path.exists() else DEFAULT_TEMPLATE.replace("{{client}}", ident.client)
        open_now = open_items(get_section(text, "Open Items") or "")
        new_md = self.render_call_note(ident, call_name, note, [], "", quotes=quotes)
        if reprocess:
            text = replace_section(text, "Current Status", "(none)")
            call_md = self._status_call_notes(ident, new_md, replacing)
            heading = "Call notes, oldest first. The newest is last."
            status_rule = ("Write it only from the call notes below, oldest first and the newest last. "
                           "Do not keep a fact that appears only in the previous Current Status. "
                           "Mention the date of the latest call.")
        else:
            call_md = new_md
            heading = f"New call note ({ident.date} — {call_name}):"
            status_rule = ("Merge what was true before with what changed in this call; "
                           "drop things that are no longer true. Mention the date of the latest call.")
        speaker = self._speaker_context(ident.client)
        open_text = "\n".join(f"- {o}" for o in open_now) or "(none)"
        status_words = str(max(120, self.s.profile_max_words // 5))
        trimmed = False

        def rendered(profile: str) -> tuple[str, str]:
            return self.prompts.render(
                "profile_update", language=self.s.notes_language, client=ident.client,
                profile=profile, open_items=open_text, date=ident.date, call_title=call_name,
                call_note=call_md, calls_heading=heading, status_rule=status_rule,
                status_words=status_words, **speaker)

        system, user = rendered(text)
        budget = int(getattr(self.s, "local_context", 8192) or 8192)

        def over() -> bool:
            return _approx_tokens(system) + _approx_tokens(user) + PROFILE_CONTEXT_RESERVE > budget

        while over():
            shorter = _drop_oldest_profile_line(text)
            if shorter is not None:
                text = shorter
            else:
                # The exact open-item list is sent beside the profile. Drop its oldest line only
                # after the profile itself has nothing left to cut. The call note stays whole.
                rows = [row for row in open_text.splitlines() if row.strip() and row.strip() != "(none)"]
                if len(rows) <= 1:
                    log.warning("Profile prompt for %s is still over the %d-token context after trimming.",
                                ident.client, budget)
                    break
                open_text = "\n".join(rows[1:])
            trimmed = True
            system, user = rendered(text)
        if trimmed:
            log.info("Trimmed the existing profile for %s so the prompt fits the context window.", ident.client)
        update = self.llm.complete_json(system, user, ProfileUpdate)
        self._log_llm("profile update")
        return update

    def apply_profile_update(self, ident: Identity, call_path: Path, note: CallNote, upd: ProfileUpdate,
                             retract: list[str] | None = None) -> list[str]:
        """Apply the profile update. Returns the bullet lines that were appended (so a later move can list them)."""
        path = self.ensure_client(ident.client)
        text = path.read_text(encoding="utf-8")
        self.backup(path)
        if retract:
            text = retract_lines(text, retract)
        call_link = self.vault.link(call_path, ident.date)
        added: list[str] = []

        text = replace_section(text, "Current Status", upd.current_status)

        new_items = [f"{a.as_line()} (from {call_link})" for a in upd.new_open_items]
        oi = merge_open_items(get_section(text, "Open Items") or "", upd.resolved_open_items, new_items, ident.date)
        text = replace_section(text, "Open Items", oi or "_None._")
        added.extend(f"- [ ] {item}" for item in new_items if f"- [ ] {item}" in oi)

        dec_body = get_section(text, "Decisions Log") or ""
        dec_norm = {normalize(l) for l in dec_body.split("\n")}
        dec_lines = [f"- {ident.date} — {d} ({call_link})" for d in note.decisions]
        dec_lines = [l for l in dec_lines if normalize(l) not in dec_norm]
        text = append_to_section(text, "Decisions Log", dec_lines)
        added.extend(dec_lines)

        work_body = get_section(text, "Work Log") or ""
        if self.vault.link(call_path).rstrip("]") not in work_body:
            first = re.split(r"(?<=[.!?])\s", note.summary.strip(), maxsplit=1)[0]
            work_line = f"- {ident.date} — Call: {first} ({call_link})"
            text = append_to_section(text, "Work Log", [work_line])
            added.append(work_line)

        for heading, additions in (("Snapshot", upd.snapshot_additions),
                                   ("Preferences & Watch-outs", upd.preferences_additions)):
            body = get_section(text, heading) or ""
            existing = {normalize(l) for l in body.split("\n")}
            lines = [f"- {a} ({call_link})" for a in additions if normalize(f"- {a} ({call_link})") not in existing]
            text = append_to_section(text, heading, lines)
            added.extend(lines)

        last = str(get_frontmatter(text).get("last_contact") or "")
        if not last or ident.date > last:
            text = set_frontmatter_field(text, "last_contact", ident.date)

        text = self.archive_if_needed(ident.client, text)
        self.vault.write(path, text)
        return added

    def archive_if_needed(self, client: str, text: str) -> str:
        if word_count(text) <= self.s.profile_max_words:
            return text
        archive_path = self.s.clients / client / f"{client} - Archive.md"
        archive = archive_path.read_text(encoding="utf-8") if archive_path.exists() else make_note(
            {"type": "client-archive", "client": client}, f"\n# {client} — Archive\n\nOlder log entries moved out of "
            f"{self.vault.link(self.profile_path(client), client)} to keep it short.\n")
        moved = 0
        for heading in ("Work Log", "Decisions Log"):
            body = get_section(text, heading) or ""
            entries = list_entries(body)
            if len(entries) <= self.s.log_keep_entries:
                continue
            old, keep = entries[:-self.s.log_keep_entries], entries[-self.s.log_keep_entries:]
            others = [l for l in body.split("\n") if l.strip() and l not in entries]
            text = replace_section(text, heading, "\n".join(others + keep))
            archive = append_to_section(archive, heading, old)
            moved += len(old)
        if moved:
            self.vault.write(archive_path, archive)
            log.info("Archived %d old log entries for %s", moved, client)
        if word_count(text) > self.s.profile_max_words:
            log.warning("Profile for %s is %d words (limit %d). Consider trimming Snapshot/Preferences by hand.",
                        client, word_count(text), self.s.profile_max_words)
        return text

    # ================================================================ review
    def send_to_review(self, path: Path, reason: str) -> Path:
        dest = self.vault.move(path, self.s.review / path.name)
        why = dest.with_name(dest.name + ".why.md")
        self.vault.write(why, f"# Needs review: {dest.name}\n\n{reason}\n\n"
                              f"Fix: rename the file to `<Client> - YYYY-MM-DD - <Title>{dest.suffix}` "
                              f"and move it back into `{self.s.inbox_dir}/`, then delete this note.\n")
        log.warning("Needs review: %s — %s", path.name, reason)
        return dest

    # ================================================================ main entry
    def process(self, path: Path, reprocess: dict | None = None) -> Result:
        path = Path(path)
        digest = sha256_file(path)
        if reprocess is None:
            reason = self._duplicate_reason(path, digest)
            if reason:
                if self.dry_run:
                    return Result(path, "skipped", message=reason)
                extra = " Use `vault-ingest reprocess` to redo it." if self.registry.get(digest) else ""
                self.send_to_review(path, reason + extra)
                return Result(path, "skipped", message=reason)
            self._claimed.add(digest)
        if not self.dry_run and reprocess is None:
            self._toast(f"Processing started: {path.name}")

        is_audio = path.suffix.lower() in AUDIO_EXTENSIONS
        transcript_text = ""
        audio_source = reprocess.get("audio_source") if reprocess else None
        transcribed_by = reprocess.get("transcribed_by") if reprocess else None
        if is_audio and self.dry_run:
            where = (f"move audio to: {self.s.system_dir}/audio/{path.name}" if self._under(path, self.s.inbox)
                     else f"leave audio where it is: {path}")
            r = Result(path, "planned")
            r.planned = [f"transcribe: {self._transcribed_by()} (the audio is not copied into Clients)", where,
                         "then process the transcript the same way as a text file"]
            return r
        if is_audio:
            try:
                cache = self.s.system / "transcripts" / f"{sanitize_name(path.stem)} [{digest[:12]}].txt"
                if cache.is_file() and cache.stat().st_size > 0:
                    transcript_text = cache.read_text(encoding="utf-8")
                    transcribed_by = self._transcribed_by()
                    log.info("Using the saved transcript for %s (%s); not transcribing again.",
                             path.name, self.vault.rel(cache))
                else:
                    self.set_step(path.name, "transcribing")
                    if self.models is not None:
                        self.models.unload_chat()
                    transcript_text, transcribed_by = self._transcribe(path)
                    if transcript_text.strip():
                        self.vault.write(cache, transcript_text)
            except GpuInterrupted:
                raise
            except TranscriberUnavailable as e:
                log.warning("%s", e)
                self._toast(f"Failed: {e}")
                self.set_step(path.name, "failed", str(e))
                return Result(path, "waiting", message=str(e))
            except Exception as e:  # noqa: BLE001
                reason = f"Could not transcribe the audio: {e}"
                if not self.dry_run:
                    self._toast(f"Failed: {reason}")
                    self.set_step(path.name, "failed", reason)
                    self.send_to_review(path, reason)
                return Result(path, "review", message=str(e))
            turns = parse_transcript_text(transcript_text)
        elif not reprocess and (mapped := lookup_source(self.s.system / "whisper-sources.json", path.name)):
            audio_source = str(mapped.get("audio") or "") or None
            transcribed_by = mapped.get("transcribed_by") or self._transcribed_by()
            try:
                turns = parse_file(path)
            except Exception as e:  # noqa: BLE001
                self.send_to_review(path, f"Could not parse the file: {e}")
                return Result(path, "review", message=str(e))
        else:
            try:
                turns = parse_file(path)
            except Exception as e:  # noqa: BLE001
                if self.dry_run:
                    return Result(path, "review", message=f"Could not parse: {e}")
                self.send_to_review(path, f"Could not parse the file: {e}")
                return Result(path, "review", message=str(e))
        session = None if reprocess else self.session_for_transcript(path, turns)
        if session is not None and session.skip and not reprocess:
            return self.skip_meeting(path)

        if total_words(turns) < 30:
            msg = "File is empty or too short to be a transcript."
            if not self.dry_run:
                self.send_to_review(path, msg)
            return Result(path, "review", message=msg)

        if not self.dry_run and self.models is not None:
            self.set_step(path.name, "summary")
            self.models.ensure_loaded()

        try:
            if reprocess:
                ident = Identity(reprocess["client"], reprocess["date"], reprocess.get("title"), how="registry")
            elif session is not None and session.client:
                ident = self.identity_from_picker(path, session)
            else:
                ident = self.identify(path, turns)
        except LeaveQueued as e:
            self.set_step(path.name, "client", str(e))
            return Result(path, "waiting", message=str(e))
        except NeedsReview as e:
            if not self.dry_run:
                self._toast(f"Failed: {e}")
                self.set_step(path.name, "failed", str(e))
                self.send_to_review(path, str(e))
            return Result(path, "review", message=str(e))
        except LLMTransient as e:
            return self._retry_llm(path, turns, session, reprocess, is_audio, transcript_text,
                                   audio_source, transcribed_by, digest, failed=e, attempt=0)

        chunks = chunk_turns(turns, self.s.chunk_min_words, self.s.chunk_max_words, self.s.chunk_overlap_words)
        client_dir = self.s.clients / ident.client
        provisional_title = ident.title or "Call"
        if reprocess:
            session = self.find_session(path, ident, turns)
        shots = self.session_shots(session) if session else []
        if session:
            log.info("%s matches meeting session %s (%s, %d screenshot(s))",
                     path.name, session.path.name if session.path else session.id, session.app, len(shots))

        if self.dry_run:
            call_name = sanitize_name(f"{ident.date} {provisional_title}")
            r = Result(path, "planned", client=ident.client)
            r.planned = [
                f"client: {ident.client}" + (" (NEW folder will be created)" if ident.new_client else "")
                + f"  [identified by {ident.how}]",
                f"words: {total_words(turns)}, turns: {len(turns)}, chunks: {len(chunks)} "
                f"(sizes: {', '.join(str(c.words) for c in chunks)})",
                f"write: {self.vault.rel(client_dir / 'Calls') }/{call_name}.md",
                f"write: {len(chunks)} chunk notes in {self.vault.rel(client_dir / 'Transcripts')}/{call_name}/",
                f"update: {self.vault.rel(self.profile_path(ident.client)) if (client_dir).exists() else ident.client + '/' + ident.client + '.md'}"
                " (Current Status, Open Items, Decisions Log, Work Log, last_contact)",
                f"move original to: {self.s.clients_dir}/{ident.client}/Transcripts/raw/{path.name}"
                if not is_audio else
                f"write transcript to: {self.s.clients_dir}/{ident.client}/Transcripts/raw/{path.with_suffix('.txt').name}",
            ]
            if session:
                r.planned.append(f"meeting session: {session.path.name if session.path else session.id} "
                                 f"({session.app}, {len(shots)} screenshot(s) to attach)")
            return r

        return self._finish_llm(path, turns, ident, chunks, client_dir, shots, session, reprocess, is_audio,
                                transcript_text, audio_source, transcribed_by, digest, provisional_title)

    def _retry_llm(self, path, turns, session, reprocess, is_audio, transcript_text, audio_source,
                   transcribed_by, digest, failed: LLMTransient, attempt: int) -> Result:
        """Identify failed before any notes. Same backoff as a failed summary."""
        phrase = failed.stats.speed_phrase()
        if attempt + 1 >= LLM_ATTEMPTS:
            reason = f"Processing failed: {failed} ({phrase})"
            if not self.dry_run and path.exists():
                self.send_to_review(path, reason)
            return Result(path, "review", message=reason)
        delay = self.llm_retry_delays[min(attempt, len(self.llm_retry_delays) - 1)]
        log.warning("%s failed (%s, %s); retry %d/%d in %.0f s.",
                    path.name, failed, phrase, attempt + 2, LLM_ATTEMPTS, delay)
        time.sleep(delay)
        try:
            ident = self.identify(path, turns)
        except NeedsReview as e:
            if not self.dry_run:
                self.send_to_review(path, str(e))
            return Result(path, "review", message=str(e))
        except LLMTransient as e:
            return self._retry_llm(path, turns, session, reprocess, is_audio, transcript_text,
                                   audio_source, transcribed_by, digest, failed=e, attempt=attempt + 1)
        chunks = chunk_turns(turns, self.s.chunk_min_words, self.s.chunk_max_words, self.s.chunk_overlap_words)
        client_dir = self.s.clients / ident.client
        if reprocess:
            session = self.find_session(path, ident, turns)
        shots = self.session_shots(session) if session else []
        return self._finish_llm(path, turns, ident, chunks, client_dir, shots, session, reprocess, is_audio,
                                transcript_text, audio_source, transcribed_by, digest, ident.title or "Call",
                                attempt=attempt + 1)

    def _finish_llm(self, path, turns, ident, chunks, client_dir, shots, session, reprocess, is_audio,
                    transcript_text, audio_source, transcribed_by, digest, provisional_title,
                    attempt: int = 0) -> Result:
        try:
            self.set_step(path.name, "summary")
            digests = self.digest_chunks(ident, provisional_title, chunks)
            self.describe_screenshots(ident, provisional_title, shots)
            note = self.write_call_note(ident, provisional_title, turns, chunks, digests, shots)
        except GpuInterrupted:
            raise
        except LLMTransient as e:
            phrase = e.stats.speed_phrase()
            if attempt + 1 >= LLM_ATTEMPTS:
                reason = f"Processing failed: {e} ({phrase})"
                if path.exists():
                    self._toast(f"Failed: {reason}")
                    self.set_step(path.name, "failed", reason)
                    self.send_to_review(path, reason)
                return Result(path, "review", message=reason)
            delay = self.llm_retry_delays[min(attempt, len(self.llm_retry_delays) - 1)]
            log.warning("%s failed (%s, %s); retry %d/%d in %.0f s.",
                        path.name, e, phrase, attempt + 2, LLM_ATTEMPTS, delay)
            time.sleep(delay)
            return self._finish_llm(path, turns, ident, chunks, client_dir, shots, session, reprocess, is_audio,
                                    transcript_text, audio_source, transcribed_by, digest, provisional_title,
                                    attempt=attempt + 1)

        # The call note exists. Later steps must not throw it away.
        pending: list[str] = []
        try:
            self.set_step(path.name, "evidence")
            note, quotes = self.check_evidence(ident, note, turns)
        except GpuInterrupted:
            raise
        except (LLMTransient, LLMUnavailable) as e:
            log.warning("Evidence check failed (%s); the call note will be written and the check retried.", e)
            quotes = VerifiedQuotes()
            pending.append("evidence")
        title = ident.title or (sanitize_name(note.title) if note.title else "Call")
        call_name = sanitize_name(f"{ident.date} {title}")
        if reprocess:
            call_path = self.s.vault_path / reprocess["call_note"]
            chunk_dir = self.s.vault_path / reprocess["chunk_dir"]
            call_name = call_path.stem
        else:
            call_path = client_dir / "Calls" / f"{call_name}.md"
            n = 2
            while call_path.exists():
                call_path = client_dir / "Calls" / f"{call_name} ({n}).md"
                n += 1
            call_name = call_path.stem
            chunk_dir = client_dir / "Transcripts" / call_name
        chunk_paths = [chunk_dir / f"{call_name} - Part {i + 1:02d}.md" for i in range(len(chunks))]
        attach_dir = call_path.parent / "attachments" / call_name
        for shot in shots:
            shot.attachment = attach_dir / shot.source.name
        raw_name = path.with_suffix(".txt").name if is_audio else path.name
        raw_dest = (self.s.vault_path / reprocess["raw"]) if reprocess else client_dir / "Transcripts" / "raw" / raw_name
        raw_rel = self.vault.rel(raw_dest) if reprocess else f"{self.s.clients_dir}/{ident.client}/Transcripts/raw/{raw_name}"
        profile_error = ""
        try:
            self.set_step(path.name, "profile")
            upd = self.plan_profile_update(ident, call_name, note, quotes, reprocess=bool(reprocess),
                                           replacing=call_path if reprocess else None)
        except GpuInterrupted:
            raise
        except (LLMError, LLMTransient, LLMUnavailable) as e:
            upd = None
            profile_error = str(e)
            pending.append("profile")
            log.warning("Profile update failed (%s); the call note will be written and the update retried.", e)

        # ================= writes start here =================
        self.ensure_client(ident.client)

        # ---- stale chunks from an earlier run (reprocess with fewer parts) -> backups, never deleted
        if chunk_dir.exists():
            for old in chunk_dir.glob("*.md"):
                if old not in chunk_paths:
                    self.vault.move(old, self.s.system / "backups" / "stale-chunks" / old.name)

        for shot in shots:                    # copies; the originals stay in _System/screenshots
            shot.attachment = self.vault.copy(shot.source, shot.attachment)
        chunk_shots: list[list[Shot]] = [[] for _ in chunks]
        for shot in shots:
            chunk_shots[chunk_for_offset(chunks, shot.seconds)].append(shot)

        if is_audio and not reprocess and self._under(path, self.s.inbox):
            moved_audio = self.vault.move(path, self.s.system / "audio" / path.name)
            audio_source = self.vault.rel(moved_audio)
        elif is_audio and not audio_source:
            audio_source = str(path.resolve())

        for i in range(len(chunks)):
            self.vault.write(chunk_paths[i], self.render_chunk(ident, call_path, call_name, i, chunks,
                                                               digests[i], raw_rel, chunk_paths, chunk_shots[i],
                                                               transcribed_by))
        self.vault.write(call_path, self.render_call_note(ident, call_name, note, chunk_paths, raw_rel,
                                                          shots, session, audio_source, transcribed_by, quotes,
                                                          digest if is_audio else None))
        previous = list(reprocess.get("profile_lines") or []) if reprocess else None
        profile_lines: list[str] = []
        if upd is not None:
            try:
                profile_lines = self.apply_profile_update(ident, call_path, note, upd, retract=previous)
            except Exception as e:  # noqa: BLE001  the notes are already on disk
                profile_error = str(e)
                if "profile" not in pending:
                    pending.append("profile")
                log.warning("Profile update failed (%s); the call note is kept.", e)

        if is_audio and not reprocess:
            self.vault.write(raw_dest, transcript_text if transcript_text.endswith("\n") else transcript_text + "\n")
        elif not reprocess:
            raw_dest = self.vault.move(path, raw_dest)
            raw_rel = self.vault.rel(raw_dest)

        record = {
            "source": f"{self.s.inbox_dir}/{path.name}" if not reprocess else reprocess.get("source"),
            "raw": raw_rel, "client": ident.client, "date": ident.date, "title": title,
            "call_note": self.vault.rel(call_path), "chunk_dir": self.vault.rel(chunk_dir),
            "chunks": len(chunks), "processed_at": dt.datetime.now().isoformat(timespec="seconds"),
            "identified_by": ident.how, "generated_by": self.generated_by,
            "profile_lines": profile_lines,
        }
        if session and session.path:
            record.update(meeting_session=self.vault.rel(session.path), screenshots=len(shots))
        if audio_source:
            record["audio_source"] = audio_source
        if is_audio:
            record["audio_sha256"] = digest
        if transcribed_by:
            record["transcribed_by"] = transcribed_by
        self.registry.put(digest, record)
        if is_audio and raw_dest.is_file():
            self.registry.put(sha256_file(raw_dest), record)
        if session is not None and not reprocess:
            self._mark_processed(session, ident.client)
        if pending:
            self._remember_pending(digest, ident, call_name, call_path, chunk_dir, raw_rel, note, quotes,
                                   pending, profile_error, audio_source, transcribed_by)
            if "profile" in pending:
                self._digest_profile_pending(ident, call_path)
        else:
            self._clear_pending(digest)
        log.info("Processed %s -> %s (%d chunks)", path.name, self.vault.rel(call_path), len(chunks))
        shown = title or call_name
        self._toast(f"Done: {ident.client} - {shown}", open_path=call_path)
        if not pending:
            self.last_error = ""
        self.set_step(None, None)
        return Result(path, "processed", ident.client, call_path, chunk_paths)

    # ================================================================ pending steps
    def _pending_path(self) -> Path:
        return self.s.system / "pending-jobs.json"

    def _load_pending(self) -> dict:
        path = self._pending_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_pending(self, data: dict) -> None:
        path = self._pending_path()
        if not data:
            if path.is_file():
                path.unlink()
            return
        self.vault.write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def _clear_pending(self, digest: str) -> None:
        data = self._load_pending()
        if digest in data:
            data.pop(digest, None)
            self._write_pending(data)

    def _remember_pending(self, digest, ident, call_name, call_path, chunk_dir, raw_rel, note, quotes,
                          steps, error, audio_source, transcribed_by) -> None:
        data = self._load_pending()
        data[digest] = {
            "steps": list(steps),
            "client": ident.client,
            "date": ident.date,
            "title": ident.title,
            "clock": ident.clock,
            "call_name": call_name,
            "call_note": self.vault.rel(call_path),
            "chunk_dir": self.vault.rel(chunk_dir),
            "raw": raw_rel,
            "note": note.model_dump(),
            "quotes": {"decisions": quotes.decisions, "actions": quotes.actions, "numbers": quotes.numbers},
            "retry_after": 0,
            "error": error,
            "audio_source": audio_source,
            "transcribed_by": transcribed_by,
        }
        self._write_pending(data)
        self.set_step(None, None, error or None)

    def _digest_profile_pending(self, ident: Identity, call_path: Path) -> None:
        from .gpu import append_digest
        append_digest(self.vault, self.s.system, f"profile update pending — {ident.client} — {call_path.stem}")

    def _quotes_from(self, raw: dict) -> VerifiedQuotes:
        raw = raw or {}
        return VerifiedQuotes(decisions=list(raw.get("decisions") or []),
                              actions=list(raw.get("actions") or []),
                              numbers=list(raw.get("numbers") or []))

    def _note_pending_failure(self, digest: str, job: dict, steps: list, step: str, error, now: float) -> None:
        counts = dict(job.get("failure_counts") or {})
        counts[step] = int(counts.get(step) or 0) + 1
        job["failure_counts"] = counts
        job["steps"] = list(steps)
        job["error"] = str(error)
        self.set_step(None, None, str(error))
        if counts[step] >= PENDING_REVIEW_AFTER:
            job["needs_review"] = True
            self._write_pending_review(digest, job, step, str(error))
            log.warning("Pending step needs review (%s, %s, %d failures): %s",
                        job.get("call_note"), step, counts[step], error)
            return
        delay = PENDING_BACKOFF_SECONDS[min(counts[step], len(PENDING_BACKOFF_SECONDS)) - 1]
        job["retry_after"] = now + delay
        log.warning("Pending step still failing (%s); retrying in %s.", error, _backoff_phrase(delay))

    def _write_pending_review(self, digest: str, job: dict, step: str, error: str) -> None:
        """A note next to the inbox. The call note stays where it is."""
        call = str(job.get("call_note") or job.get("call_name") or digest[:8])
        text = (f"# Needs review\n\nCall: {call}\n\nStep: {step}\n\n{error}\n\n"
                f"Retry with `vault-ingest pending retry {digest}` or `vault-ingest reprocess`.\n")
        self.vault.write(self.s.review / f"pending-{digest[:8]}-{step}.md", text)

    def reset_pending(self, key: str | None = None, path: Path | None = None) -> list[str]:
        """Clear needs-review and the failure count so the job is tried again. Returns the keys reset."""
        data = self._load_pending()
        digest = sha256_file(path) if path is not None and path.is_file() else None
        resolved = path.resolve() if path is not None else None
        reset = []
        for job_key, job in data.items():
            raw = self.s.vault_path / str(job.get("raw") or "")
            match = False
            if key and (job_key == key or job_key.startswith(key)):
                match = True
            if digest and job_key == digest:
                match = True
            if resolved is not None:
                try:
                    if raw.is_file() and raw.resolve() == resolved:
                        match = True
                except OSError:
                    pass
            if not match:
                continue
            job["needs_review"] = False
            job["failure_counts"] = {}
            job["retry_after"] = 0
            reset.append(job_key)
        if reset:
            self._write_pending(data)
        return reset

    def resume_pending(self) -> int:
        """Retry evidence or profile for calls whose notes are already written. Returns how many finished."""
        data = self._load_pending()
        if not data:
            return 0
        now = time.time()
        due = []
        for digest, job in list(data.items()):
            if job.get("needs_review"):
                continue
            if float(job.get("retry_after") or 0) > now:
                continue
            steps = list(job.get("steps") or [])
            if not steps:
                data.pop(digest, None)
                continue
            due.append(digest)
        if not due:
            self._write_pending(data)
            return 0
        if self.gpu is not None and (reason := self.gpu.start_blocked()):
            log.info("Not retrying pending steps (%s).", reason)
            self._write_pending(data)
            return 0
        done = 0
        loaded_for_retry = False
        try:
            for digest in due:
                job = data.get(digest)
                if not job:
                    continue
                steps = list(job.get("steps") or [])
                current = steps[0] if steps else "profile"
                try:
                    if self.models is not None and not loaded_for_retry:
                        self.models.ensure_loaded()
                        loaded_for_retry = True
                    ident = Identity(str(job.get("client") or ""), str(job.get("date") or ""), job.get("title"),
                                     how="pending", clock=job.get("clock"))
                    note = CallNote.model_validate(job.get("note") or {})
                    quotes = self._quotes_from(job.get("quotes"))
                    call_path = self.s.vault_path / str(job.get("call_note") or "")
                    if "evidence" in steps:
                        current = "evidence"
                        self.set_step(call_path.name, "evidence")
                        raw = self.s.vault_path / str(job.get("raw") or "")
                        turns = parse_file(raw) if raw.is_file() else []
                        note, quotes = self.check_evidence(ident, note, turns)
                        self._patch_evidence(call_path, note, quotes)
                        job["note"] = note.model_dump()
                        job["quotes"] = {"decisions": quotes.decisions, "actions": quotes.actions,
                                         "numbers": quotes.numbers}
                        steps.remove("evidence")
                    if "profile" in steps:
                        current = "profile"
                        self.set_step(call_path.name, "profile")
                        call_name = str(job.get("call_name") or call_path.stem)
                        upd = self.plan_profile_update(ident, call_name, note, quotes)
                        self.apply_profile_update(ident, call_path, note, upd)
                        steps.remove("profile")
                except GpuInterrupted:
                    log.info("Pending retry paused.")
                    break
                except (LLMError, LLMTransient, LLMUnavailable) as e:
                    self._note_pending_failure(digest, job, steps, current, e, now)
                    continue
                except Exception as e:  # noqa: BLE001
                    log.exception("Could not retry the pending step for %s", job.get("call_note"))
                    self._note_pending_failure(digest, job, steps, current, e, now)
                    continue
                done += 1
                data.pop(digest, None)
                log.info("Finished the pending step for %s", job.get("call_note"))
        finally:
            self._write_pending(data)
            if loaded_for_retry and self.models is not None:
                self.models.unload_chat()
        if done or loaded_for_retry:
            self.last_error = "" if done else self.last_error
            self.set_step(None, None)
        return done

    def _patch_evidence(self, call_path: Path, note: CallNote, quotes: VerifiedQuotes) -> None:
        if not call_path.is_file():
            return
        text = call_path.read_text(encoding="utf-8")

        def with_quotes(body: str, pairs: list[tuple[str, str]]) -> str:
            callout = evidence_callout(pairs)
            return f"{body}\n\n{callout}" if callout else body

        def bullets(items: list[str]) -> str:
            return "\n".join(f"- {x}" for x in items) if items else "_None._"

        actions = with_quotes("\n".join(f"- {a.as_line()}" for a in note.action_items) or "_None._", quotes.actions)
        text = replace_section(text, "Decisions", with_quotes(bullets(note.decisions), quotes.decisions))
        text = replace_section(text, "Action Items", actions)
        text = replace_section(text, "Notable Details", with_quotes(bullets(note.notable_details), quotes.numbers))
        self.vault.write(call_path, text)

    # ================================================================ picker changes after filing
    def _mark_processed(self, session: Session, client: str) -> None:
        """Remember which client this meeting was filed under, without touching a newer picker choice."""
        store = SessionStore(self.s.system / "meetings", self.vault)
        fresh = store.find(session.id)
        if fresh is not None:
            store.update(fresh, processed_client=client)

    def sync_meeting_selections(self) -> int:
        """Move a call when the picker client changed after processing. The old profile is left as it is."""
        store = SessionStore(self.s.system / "meetings", self.vault)
        moved = 0
        for session in store.load():
            if session.skip or not session.client or not session.processed_client:
                continue
            if session.client == session.processed_client:
                continue
            try:
                if self.reassign_session(session, store):
                    moved += 1
            except Exception:  # noqa: BLE001  one meeting must not stop the inbox
                log.exception("Could not move the call for session %s", session.id)
        return moved

    def reassign_session(self, session: Session, store: SessionStore | None = None) -> bool:
        if session.path is None or not session.client:
            return False
        rel = self.vault.rel(session.path)
        groups: dict[str, list[tuple[str, dict]]] = {}
        for digest, rec in list(self.registry.data.items()):
            if rec.get("meeting_session") != rel or not rec.get("call_note"):
                continue
            groups.setdefault(str(rec["call_note"]), []).append((digest, rec))
        if not groups:
            return False
        new_client = session.client
        self.ensure_client(new_client)
        for _call_rel, items in groups.items():
            rec = items[0][1]
            old_client = str(rec.get("client") or session.processed_client or "")
            if old_client == new_client:
                continue
            moved = self._move_call_files(rec, new_client)
            if moved is None:
                continue
            self._append_digest(old_client, new_client, moved["call"], list(rec.get("profile_lines") or []))
            for digest, old in items:
                updated = dict(old)
                updated.update(client=new_client, call_note=moved["call_rel"],
                               chunk_dir=moved["chunk_rel"], raw=moved["raw_rel"])
                self.registry.put(digest, updated)
        if store is None:
            store = SessionStore(self.s.system / "meetings", self.vault)
        fresh = store.find(session.id) or session
        store.update(fresh, processed_client=new_client)
        log.info("Moved the call for %s to %s", session.id, new_client)
        return True

    def _move_call_files(self, rec: dict, new_client: str) -> dict | None:
        old_client = str(rec.get("client") or "")
        call_path = self.s.vault_path / str(rec["call_note"])
        if not call_path.is_file():
            log.warning("Call note %s is missing; not moved.", rec.get("call_note"))
            return None
        new_call = self.vault.move(call_path, self.s.clients / new_client / "Calls" / call_path.name)
        old_stem, new_stem = call_path.stem, new_call.stem
        old_attach = call_path.parent / "attachments" / old_stem
        if old_attach.is_dir():
            for src in sorted(p for p in old_attach.iterdir() if p.is_file()):
                self.vault.move(src, new_call.parent / "attachments" / new_stem / src.name)
        chunk_dir = self.s.vault_path / str(rec["chunk_dir"]) if rec.get("chunk_dir") else None
        new_chunk_dir = self.s.clients / new_client / "Transcripts" / new_stem
        moved_chunks: list[Path] = []
        if chunk_dir is not None and chunk_dir.is_dir():
            for src in sorted(chunk_dir.glob("*.md")):
                moved_chunks.append(self.vault.move(src, new_chunk_dir / src.name))
        raw = self.s.vault_path / str(rec["raw"]) if rec.get("raw") else None
        if raw is not None and raw.is_file():
            new_raw = self.vault.move(raw, self.s.clients / new_client / "Transcripts" / "raw" / raw.name)
            raw_rel = self.vault.rel(new_raw)
        else:
            raw_rel = rec.get("raw")
        for path in (new_call, *moved_chunks):
            self._rewrite_moved(path, old_client, new_client, old_stem, new_stem)
        return {"call": new_call, "call_rel": self.vault.rel(new_call),
                "chunk_rel": self.vault.rel(new_chunk_dir), "raw_rel": raw_rel}

    def _rewrite_moved(self, path: Path, old_client: str, new_client: str, old_stem: str, new_stem: str) -> None:
        text = path.read_text(encoding="utf-8")
        clients = self.s.clients_dir
        text = text.replace(f"[[{clients}/{old_client}/{old_client}|{old_client}]]",
                            f"[[{clients}/{new_client}/{new_client}|{new_client}]]")
        text = text.replace(f"[[{clients}/{old_client}/{old_client}]]",
                            f"[[{clients}/{new_client}/{new_client}]]")
        text = text.replace(f"{clients}/{old_client}/", f"{clients}/{new_client}/")
        if old_stem != new_stem:
            text = text.replace(f"Calls/{old_stem}", f"Calls/{new_stem}")
            text = text.replace(f"Transcripts/{old_stem}/", f"Transcripts/{new_stem}/")
            text = text.replace(f"attachments/{old_stem}/", f"attachments/{new_stem}/")
        text = set_frontmatter_field(text, "client", new_client)
        self.vault.write(path, text)

    def _append_digest(self, old_client: str, new_client: str, call_path: Path, lines: list[str]) -> None:
        path = self.s.system / "digest.md"
        stamp = dt.datetime.now().astimezone().replace(microsecond=0).strftime("%Y-%m-%d %H:%M")
        listed = lines or ["_No profile lines were recorded for this call._"]
        entry = "\n".join([
            f"## {stamp} — Moved call from {old_client} to {new_client}",
            "",
            f"Call: {self.vault.link(call_path)}",
            "",
            f"Lines added to {old_client} (remove by hand):",
            "",
            *listed,
            "",
        ])
        if path.is_file():
            text = path.read_text(encoding="utf-8").rstrip() + "\n\n" + entry
        else:
            text = ("# Ingest digest\n\nCalls moved after they were processed. "
                    "The lines below were added to the old client; remove them by hand.\n\n" + entry)
        self.vault.write(path, text if text.endswith("\n") else text + "\n")

    def run_inbox(self) -> list[Result]:
        try:
            self.resume_pending()
        except Exception:  # noqa: BLE001
            log.exception("Could not retry a pending step")
        try:
            self.sync_meeting_selections()
        except Exception:  # noqa: BLE001
            log.exception("Could not apply a client change")
        results = []
        files = self.inbox_files()
        try:
            for n, f in enumerate(files):
                if self.gpu is not None and (reason := self.gpu.start_blocked()):
                    log.info("Not starting %s (%s).", f.name, reason)
                    results.extend(Result(g, "waiting", message=reason) for g in files[n:])
                    break
                try:
                    results.append(self.process(f))
                except GpuInterrupted as e:
                    log.info("Paused; %s stays in the inbox.", f.name)
                    results.append(Result(f, "waiting", message=str(e)))
                    results.extend(Result(g, "waiting", message=str(e)) for g in files[n + 1:])
                    if self.models is not None:
                        self.models.unload_chat()
                    break
                except LLMUnavailable as e:
                    # not the file's fault: leave it (and the rest) in the inbox for the next run
                    log.warning("%s", e)
                    self._note_unavailable(e)
                    results.extend(Result(g, "waiting", message=str(e)) for g in files[n:])
                    break
                except Exception as e:  # noqa: BLE001  keep going with the other files
                    log.exception("Failed on %s", f.name)
                    reason = f"Processing failed: {e}"
                    self._toast(f"Failed: {reason}")
                    self.set_step(f.name, "failed", reason)
                    if self.models is not None:
                        self.models.unload_chat()
                    if not self.dry_run and f.exists():
                        self.send_to_review(f, reason)
                    results.append(Result(f, "review", message=str(e)))
        finally:
            if self.models is not None:
                self.models.unload_chat()
        return results

    def _recordings_folder(self) -> Path:
        import os
        import tomllib

        default = Path(os.path.expandvars(os.path.expanduser(r"%USERPROFILE%\Music\meetily-recordings")))
        config = self.s.prompts_dir.parent / "companion.toml"
        if not config.is_file():
            return default
        try:
            raw = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return default
        recordings = (raw.get("meetily") or {}).get("recordings")
        if not recordings:
            return default
        return Path(os.path.expandvars(os.path.expanduser(str(recordings))))

    def _audio_for(self, path: Path, rec: dict) -> Path:
        """The recording this transcript came from. Raises SystemExit before anything is overwritten."""
        named = str(rec.get("audio_source") or "")
        if named:
            candidate = Path(named)
            if not candidate.is_file():
                candidate = self.s.vault_path / named
            if candidate.is_file():
                return candidate
        inbox_name = Path(str(rec.get("source") or "")).name
        mapped = lookup_source(self.s.system / "whisper-sources.json", inbox_name)
        if mapped and Path(str(mapped.get("audio") or "")).is_file():
            return Path(str(mapped["audio"]))
        target = last_timestamp_seconds(parse_file(path))
        best: Path | None = None
        best_gap: float | None = None
        folder = self._recordings_folder()
        if folder.is_dir():
            for audio in folder.rglob("audio.mp4"):
                dur = media_seconds(audio)
                if dur is None:
                    continue
                gap = abs(dur - target)
                if gap <= AUDIO_MATCH_SECONDS and (best_gap is None or gap < best_gap):
                    best, best_gap = audio, gap
        if best is None:
            raise SystemExit(
                "No audio file matches this transcript (checked audio_source, whisper-sources, "
                f"and recordings within 10 minutes of {hms(target)}). Nothing was changed.")
        log.info("Matched %s to %s (%.0fs from the transcript's last timestamp)", path.name, best, best_gap)
        return best

    def reprocess(self, path: Path, from_audio: bool = False) -> Result:
        path = Path(path)
        if not path.is_absolute():
            path = self.s.vault_path / path
        if not path.exists():
            raise SystemExit(f"File not found: {path}")
        self.reset_pending(path=path)
        digest = sha256_file(path)
        rec = self.registry.get(digest)
        if not rec:
            raise SystemExit("That file isn't in the ingest log. Put it in the inbox to process it as new.")
        if not from_audio:
            return self.process(path, reprocess=rec)

        audio = self._audio_for(path, rec)
        for rel in (rec.get("call_note"), rec.get("raw")):
            if rel:
                src = self.s.vault_path / str(rel)
                if src.is_file():
                    self.backup(src)
        profile = self.profile_path(str(rec.get("client") or ""))
        if profile.is_file():
            self.backup(profile)
        if self.models is not None:
            self.models.unload_chat()
        text, transcribed_by = self._transcribe(audio)
        old_text = path.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        path.write_text(text, encoding="utf-8")
        updated = dict(rec)
        updated["audio_source"] = str(audio)
        updated["transcribed_by"] = transcribed_by
        try:
            result = self.process(path, reprocess=updated)
        except Exception:
            if path.exists():
                path.write_text(old_text, encoding="utf-8")
            raise
        if result.status != "processed":
            if path.exists():
                path.write_text(old_text, encoding="utf-8")
            return result
        new_digest = sha256_file(path)
        if new_digest != digest:
            self.registry.remove(digest)
        return result

    def local_records(self) -> list[dict]:
        """Ingest-log records of calls whose notes were written in local mode."""
        return [rec for rec in self.registry.data.values()
                if str(rec.get("generated_by") or "").startswith("local/")]
