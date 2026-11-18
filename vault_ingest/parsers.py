"""Parse transcript files (.txt, .md, .vtt, .srt, .docx) into speaker turns."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

TEXT_EXTENSIONS = {".txt", ".md", ".vtt", ".srt", ".docx"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4", ".mkv", ".webm", ".ogg", ".flac", ".aac", ".amr", ".3ga", ".opus"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | AUDIO_EXTENSIONS

# Only pure disfluencies. Short answers like "yes" / "da" / "okay" are kept on purpose:
# "Do we approve the budget?" -> "Yes." is information.
FILLER_WORDS = {
    "um", "uh", "uhm", "umm", "erm", "hm", "hmm", "mhm", "mm", "mmm", "ah", "eh",
    "uh-huh", "mm-hmm", "e", "ovaj", "aaa", "eee",
}

_TS = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?"
CUE_RE = re.compile(rf"^\s*(?P<start>{_TS})\s*-->\s*(?P<end>{_TS})")
VOICE_TAG_RE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>")
TAG_RE = re.compile(r"</?[^>]+>")
# "[00:01:02] Name: text" | "(00:01) Name: text" | "00:01:02 Name: text"
TS_PREFIX_RE = re.compile(rf"^\s*[\[(]?(?P<ts>{_TS})[\])]?\s*[-–—]?\s*(?P<rest>.*)$")
# "Name (00:01:02): text" | "Name [00:01]: text"
NAME_TS_RE = re.compile(rf"^\s*(?P<name>[^\n:\[\]()]{{1,40}}?)\s*[\[(](?P<ts>{_TS})[\])]\s*:?\s*(?P<rest>.*)$")
# "Name: text"  (name = 1-4 words, no sentence punctuation)
SPEAKER_RE = re.compile(r"^\s*(?P<name>[A-ZÀ-ÖØ-ÞČĆŽŠĐ][\w.'’\-]*(?:\s+[\w.'’\-]+){0,3})\s*:\s+(?P<rest>.+)$")
# Otter / Teams style: a line with just "Name  0:03" or "Name 00:01:02"
NAME_LINE_RE = re.compile(rf"^\s*(?P<name>[^\n:]{{1,40}}?)\s+(?P<ts>{_TS})\s*$")


@dataclass
class Turn:
    speaker: str | None
    text: str
    start: str | None = None
    end: str | None = None
    # (word index, HH:MM:SS) of lines merged into this turn, so splitting it later keeps their times
    marks: list[tuple[int, str]] = field(default_factory=list)

    @property
    def words(self) -> int:
        return len(self.text.split())


def normalize_ts(ts: str | None) -> str | None:
    """Return HH:MM:SS (drop milliseconds)."""
    if not ts:
        return None
    ts = re.split(r"[.,]", ts.strip())[0]
    parts = [int(p) for p in ts.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return f"{h:02d}:{m:02d}:{s:02d}"


def is_filler(text: str) -> bool:
    tokens = [t for t in re.split(r"[\s,.!?…]+", text.lower()) if t]
    return bool(tokens) and all(t in FILLER_WORDS for t in tokens)


def _merge(turns: list[Turn]) -> list[Turn]:
    """Merge consecutive turns from the same speaker and drop filler-only turns."""
    out: list[Turn] = []
    for t in turns:
        t.text = re.sub(r"\s+", " ", t.text).strip()
        if not t.text or is_filler(t.text):
            continue
        if out and out[-1].speaker == t.speaker:
            prev = out[-1]
            if t.start:
                prev.marks.append((prev.words, t.start))
            prev.text = f"{prev.text} {t.text}"
            prev.end = t.end  # None -> filled from the next speaker's start
        else:
            out.append(t)
    return out


# ---------------------------------------------------------------- subtitle formats
def _parse_cues(text: str) -> list[Turn]:
    turns: list[Turn] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = [l for l in block.split("\n") if l.strip()]
        cue_idx = next((i for i, l in enumerate(lines) if CUE_RE.match(l)), None)
        if cue_idx is None:
            continue
        m = CUE_RE.match(lines[cue_idx])
        body = " ".join(lines[cue_idx + 1:])
        speaker = None
        vm = VOICE_TAG_RE.search(body)
        if vm:
            speaker = vm.group(1).strip()
        body = TAG_RE.sub("", body).strip()
        if speaker is None:
            sm = SPEAKER_RE.match(body)
            if sm:
                speaker, body = sm.group("name").strip(), sm.group("rest").strip()
        turns.append(Turn(speaker, body, normalize_ts(m.group("start")), normalize_ts(m.group("end"))))
    return turns


# ---------------------------------------------------------------- plain text
def _parse_lines(lines: list[str]) -> list[Turn]:
    turns: list[Turn] = []
    pending_speaker: str | None = None
    pending_ts: str | None = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") and len(line) < 80 and not turns:
            # skip headings at the top of markdown transcripts
            continue
        m = NAME_LINE_RE.match(line)
        if m and not SPEAKER_RE.match(line):
            pending_speaker, pending_ts = m.group("name").strip(), normalize_ts(m.group("ts"))
            continue
        ts = None
        m = NAME_TS_RE.match(line)
        if m and m.group("rest"):
            turns.append(Turn(m.group("name").strip(), m.group("rest"), normalize_ts(m.group("ts"))))
            pending_speaker = None
            continue
        m = TS_PREFIX_RE.match(line)
        if m and re.match(rf"^\s*[\[(]?{_TS}", line):
            ts, line = normalize_ts(m.group("ts")), m.group("rest").strip()
        m = SPEAKER_RE.match(line)
        if m:
            turns.append(Turn(m.group("name").strip(), m.group("rest"), ts))
            pending_speaker = None
        elif pending_speaker is not None:
            turns.append(Turn(pending_speaker, line, ts or pending_ts))
            pending_ts = None
        elif turns and ts is None:
            turns.append(Turn(turns[-1].speaker, line, None))
        else:
            turns.append(Turn(None, line, ts))
    return turns


def _read_docx(path: Path) -> list[str]:
    import docx  # python-docx

    doc = docx.Document(str(path))
    return [p.text for p in doc.paragraphs]


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1250", "latin-1"):
        try:
            text = raw.decode(enc)
            if enc == "utf-16" and "\x00" in text:
                continue
            return text
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_transcript_text(text: str, ext: str = "") -> list[Turn]:
    """Parse transcript text that is already in memory (for example a Whisper result)."""
    if ext in (".vtt", ".srt") or CUE_RE.search(text[:2000] or ""):
        turns = _parse_cues(text)
    else:
        turns = _parse_lines(text.splitlines())
    turns = _merge(turns)
    # fill missing end times from the next turn's start
    for i, t in enumerate(turns[:-1]):
        if t.end is None and turns[i + 1].start:
            t.end = turns[i + 1].start
    return turns


def parse_file(path: str | Path) -> list[Turn]:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in AUDIO_EXTENSIONS:
        raise ValueError(f"{path.name} is audio; transcribe it before parsing")
    if ext not in TEXT_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}")
    if ext == ".docx":
        turns = _parse_lines(_read_docx(path))
    else:
        text = read_text(path)
        turns = parse_transcript_text(text, ext)
        return turns
    turns = _merge(turns)
    for i, t in enumerate(turns[:-1]):
        if t.end is None and turns[i + 1].start:
            t.end = turns[i + 1].start
    return turns


def total_words(turns: list[Turn]) -> int:
    return sum(t.words for t in turns)


def ts_seconds(ts: str | None) -> int | None:
    """HH:MM:SS (or MM:SS) -> seconds."""
    norm = normalize_ts(ts)
    if not norm:
        return None
    h, m, s = (int(p) for p in norm.split(":"))
    return h * 3600 + m * 60 + s


def last_timestamp_seconds(turns: list[Turn]) -> int:
    """Latest timestamp anywhere in the transcript (0 if it has none): roughly the call's duration."""
    best = 0
    for t in turns:
        for ts in [t.start, t.end, *(m[1] for m in t.marks)]:
            sec = ts_seconds(ts)
            if sec is not None and sec > best:
                best = sec
    return best


def format_turn(t: Turn) -> str:
    if t.speaker:
        ts = f" [{t.start}]" if t.start else ""
        return f"**{t.speaker}**{ts}: {t.text}"
    if t.start:
        return f"[{t.start}] {t.text}"
    return t.text


def turns_to_text(turns: list[Turn]) -> str:
    return "\n\n".join(format_turn(t) for t in turns)
