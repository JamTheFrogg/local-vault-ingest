"""Structured outputs expected from the LLM (validated with pydantic)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

SCREENSHOT_KINDS = ("slide", "document", "website", "chat", "diagram", "other")
VISIBLE_TEXT_MAX_WORDS = 80


def _clean_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [x for x in v if (x.strip() if isinstance(x, str) else x)]


class ClientGuess(BaseModel):
    client: str | None = Field(None, description="Exact existing client folder name, or null")
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    reason: str = ""


class ChunkDigest(BaseModel):
    context: str = Field(..., description="One sentence situating the chunk in the call")
    points: list[str] = Field(default_factory=list, description="3-6 bullets of what this chunk contains")

    @field_validator("points", mode="before")
    @classmethod
    def _clean_lists(cls, v):
        return _clean_list(v)


class ActionItem(BaseModel):
    task: str
    owner: str | None = None
    due: str | None = None

    def as_line(self) -> str:
        parts = [self.task.strip()]
        parts.append(self.owner.strip() if self.owner else "?")
        if self.due:
            parts.append(self.due.strip())
        return " — ".join(parts)


class CallNote(BaseModel):
    title: str | None = Field(None, description="Short descriptive call title if the filename gives none")
    participants: list[str] = Field(default_factory=list)
    summary: str
    key_points: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    notable_details: list[str] = Field(default_factory=list,
                                       description="Numbers, prices, names, dates, tools mentioned")

    @field_validator("participants", "key_points", "decisions", "open_questions",
                             "notable_details", mode="before")
    @classmethod
    def _clean_lists(cls, v):
        return _clean_list(v)

    @field_validator("action_items", mode="before")
    @classmethod
    def _coerce_items(cls, v):
        if v is None:
            return []
        out = []
        for x in v:
            out.append({"task": x} if isinstance(x, str) else x)
        return out


class ProfileUpdate(BaseModel):
    current_status: str = Field(..., description="Rewritten 'Current Status' section body")
    resolved_open_items: list[str] = Field(default_factory=list,
                                           description="Exact text of existing open items this call resolved")
    new_open_items: list[ActionItem] = Field(default_factory=list)
    snapshot_additions: list[str] = Field(default_factory=list,
                                          description="New durable facts about the client (who/what), if any")
    preferences_additions: list[str] = Field(default_factory=list,
                                             description="New preferences / watch-outs learned, if any")

    @field_validator("resolved_open_items", "snapshot_additions", "preferences_additions", mode="before")
    @classmethod
    def _clean_lists(cls, v):
        return _clean_list(v)

    @field_validator("new_open_items", mode="before")
    @classmethod
    def _coerce_items(cls, v):
        if v is None:
            return []
        return [{"task": x} if isinstance(x, str) else x for x in v]


class ClaimQuote(BaseModel):
    claim: str = ""
    quote: str = ""


class EvidenceCheck(BaseModel):
    """Quotes the model says support the call note. The pipeline checks them against the transcript."""

    decisions: list[ClaimQuote] = Field(default_factory=list)
    action_items: list[ClaimQuote] = Field(default_factory=list)
    numbers: list[ClaimQuote] = Field(default_factory=list)

    @field_validator("decisions", "action_items", "numbers", mode="before")
    @classmethod
    def _clean_claims(cls, v):
        if v is None:
            return []
        if isinstance(v, dict):
            return [v]
        return v


class ScreenshotDescription(BaseModel):
    caption: str = Field(..., description="1-2 sentences: what the screenshot shows")
    visible_text: str = Field("", description="Key text and numbers readable in the image, max ~80 words")
    kind: Literal["slide", "document", "website", "chat", "diagram", "other"] = "other"
    people: list[str] = Field(default_factory=list,
                              description="Names of meeting participants readable on screen (tiles, roster)")

    @field_validator("visible_text", mode="before")
    @classmethod
    def _cap_text(cls, v):
        if v is None:
            return ""
        if isinstance(v, list):
            v = " ".join(str(x) for x in v)
        lines, left = [], VISIBLE_TEXT_MAX_WORDS
        for line in str(v).splitlines():
            words = line.split()
            if not words:
                continue
            if left <= 0 or len(words) > left:
                if left > 0:
                    lines.append(" ".join(words[:left]))
                lines.append("…")
                break
            lines.append(" ".join(words))
            left -= len(words)
        return "\n".join(lines)

    @field_validator("people", mode="before")
    @classmethod
    def _clean_people(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = [x for x in v.replace(";", ",").split(",")]
        out = []
        for x in v:
            name = " ".join(str(x or "").split())
            if name and name.lower() not in {n.lower() for n in out}:
                out.append(name[:80])
        return out[:30]

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, v):
        v = str(v or "").strip().lower()
        return v if v in SCREENSHOT_KINDS else "other"
