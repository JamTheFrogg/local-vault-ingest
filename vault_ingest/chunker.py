"""Split speaker turns into overlapping chunks that respect turn boundaries."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .parsers import Turn, ts_seconds, turns_to_text


@dataclass
class Chunk:
    turns: list[Turn]
    overlap: list[Turn] = field(default_factory=list)   # carried over from the previous chunk

    @property
    def words(self) -> int:
        return sum(t.words for t in self.turns)

    @property
    def start(self) -> str | None:
        return next((t.start for t in self.turns if t.start), None)

    @property
    def end(self) -> str | None:
        """Latest timestamp in the chunk (start, an interior mark, or end)."""
        best: str | None = None
        best_s = -1
        for t in self.turns:
            for ts in (t.start, t.end, *(mark[1] for mark in t.marks)):
                sec = ts_seconds(ts)
                if sec is not None and sec >= best_s:
                    best, best_s = ts, sec
        return best

    @property
    def speakers(self) -> list[str]:
        seen: list[str] = []
        for t in self.overlap + self.turns:
            if t.speaker and t.speaker not in seen:
                seen.append(t.speaker)
        return seen

    def text(self) -> str:
        body = turns_to_text(self.turns)
        if self.overlap:
            return f"{turns_to_text(self.overlap)}\n\n---\n\n{body}"
        return body


def _split_long_turn(turn: Turn, max_words: int) -> list[Turn]:
    """Split a single oversized turn on sentence boundaries (hard-split as a last resort)."""
    sentences = re.split(r"(?<=[.!?…])\s+", turn.text)
    pieces: list[str] = []
    cur: list[str] = []
    cur_words = 0
    for s in sentences:
        sw = len(s.split())
        if sw > max_words:  # one giant "sentence" (no punctuation) -> hard split
            if cur:
                pieces.append(" ".join(cur))
                cur, cur_words = [], 0
            words = s.split()
            for i in range(0, len(words), max_words):
                pieces.append(" ".join(words[i:i + max_words]))
            continue
        if cur_words + sw > max_words and cur:
            pieces.append(" ".join(cur))
            cur, cur_words = [], 0
        cur.append(s)
        cur_words += sw
    if cur:
        pieces.append(" ".join(cur))
    built: list[tuple[str, str | None, list]] = []
    word_at = 0
    for i, p in enumerate(pieces):
        start = turn.start if i == 0 else next((ts for w, ts in reversed(turn.marks) if w <= word_at), None)
        n = len(p.split())
        marks = [(w - word_at, ts) for w, ts in turn.marks if word_at < w < word_at + n]
        built.append((p, start, marks))
        word_at += n
    out = []
    for i, (p, start, marks) in enumerate(built):
        if i < len(built) - 1:
            end = built[i + 1][1] or (marks[-1][1] if marks else None)
        else:
            end = turn.end or (marks[-1][1] if marks else None)
        out.append(Turn(turn.speaker, p, start, end, marks))
    return out


def _overlap_from(turns: list[Turn], overlap_words: int) -> list[Turn]:
    if overlap_words <= 0 or not turns:
        return []
    picked: list[Turn] = []
    total = 0
    for t in reversed(turns):
        if total + t.words > overlap_words:
            break
        picked.insert(0, t)
        total += t.words
    if picked:
        return [Turn(t.speaker, t.text, t.start, t.end) for t in picked]
    # last turn alone is longer than the overlap budget -> take its tail
    last = turns[-1]
    tail = " ".join(last.text.split()[-overlap_words:])
    return [Turn(last.speaker, "… " + tail, None, last.end)]


def chunk_turns(turns: list[Turn], min_words: int = 600, max_words: int = 900,
                overlap_words: int = 100) -> list[Chunk]:
    if min_words > max_words:
        raise ValueError("min_words must be <= max_words")
    expanded: list[Turn] = []
    for t in turns:
        expanded.extend(_split_long_turn(t, max_words) if t.words > max_words else [t])

    groups: list[list[Turn]] = []
    cur: list[Turn] = []
    cur_words = 0
    for t in expanded:
        if cur and cur_words + t.words > max_words and cur_words >= min_words:
            groups.append(cur)
            cur, cur_words = [], 0
        elif cur and cur_words + t.words > max_words and cur_words < min_words:
            # adding would overshoot but we're still under the minimum: allow a modest overshoot
            # only if it stays within max + 25%, otherwise close the chunk early.
            if cur_words + t.words > int(max_words * 1.25):
                groups.append(cur)
                cur, cur_words = [], 0
        cur.append(t)
        cur_words += t.words
    if cur:
        # merge a tiny tail into the previous chunk when it fits
        if groups and cur_words < min_words // 3 and sum(x.words for x in groups[-1]) + cur_words <= int(max_words * 1.25):
            groups[-1].extend(cur)
        else:
            groups.append(cur)

    chunks: list[Chunk] = []
    for i, g in enumerate(groups):
        overlap = _overlap_from(groups[i - 1], overlap_words) if i > 0 else []
        chunks.append(Chunk(turns=g, overlap=overlap))
    return chunks
