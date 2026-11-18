"""Client search, creation, and recurring-meeting keys shared by the picker and the CLI."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from .vault import Vault, append_to_section, get_frontmatter, get_section, sanitize_name, split_frontmatter

log = logging.getLogger("vault_ingest")

# casefold first, then đ -> dj (a digraph), then the single-letter folds
_FOLDS = str.maketrans({"č": "c", "ć": "c", "š": "s", "ž": "z"})
_MEET_CODE = re.compile(r"\b[a-z]{3}-[a-z]{4}-[a-z]{3}\b")
_ZOOM_SPACED = re.compile(r"\b(\d{3})[\s-](\d{3})[\s-](\d{3,5})\b")
_ZOOM_PLAIN = re.compile(r"\b(\d{9,11})\b")
_BROWSER_SUFFIX = re.compile(
    r"\s+[-–—]\s+(Google Chrome|Microsoft Edge|Mozilla Firefox|Brave|Opera).*$", re.I)
_TEAMS_SUFFIX = re.compile(r"\s*\|\s*Microsoft Teams\s*$", re.I)
_CONTACT = re.compile(r"Contact:\s*([^(\n\r]+)", re.I)
RANK_LIMIT = 8
RANK_CUTOFF = 60.0
NEAR_DUPLICATE = 90.0


def fold(text: str) -> str:
    """Case- and diacritics-insensitive form: č/ć→c, š→s, ž→z, đ→dj."""
    s = (text or "").casefold().replace("đ", "dj")
    return s.translate(_FOLDS)


@dataclass
class ClientInfo:
    name: str
    aliases: list[str] = field(default_factory=list)
    contacts: list[str] = field(default_factory=list)


@dataclass
class Hit:
    name: str
    score: float
    via: str          # name | alias | contact
    matched: str


def contact_names(snapshot: str) -> list[str]:
    names = []
    for m in _CONTACT.finditer(snapshot or ""):
        name = m.group(1).strip(" .,-")
        if name and name not in names:
            names.append(name)
    return names


def load_clients(clients_dir: Path) -> list[ClientInfo]:
    if not clients_dir.is_dir():
        return []
    out = []
    for p in sorted((x for x in clients_dir.iterdir() if x.is_dir() and not x.name.startswith((".", "_"))),
                    key=lambda x: x.name.lower()):
        aliases: list[str] = []
        contacts: list[str] = []
        profile = p / f"{p.name}.md"
        if profile.is_file():
            try:
                text = profile.read_text(encoding="utf-8")
            except OSError:
                text = ""
            raw = get_frontmatter(text).get("aliases") or []
            if isinstance(raw, str):
                raw = [raw]
            aliases = [str(a).strip() for a in raw if str(a).strip()]
            contacts = contact_names(get_section(text, "Snapshot") or "")
        out.append(ClientInfo(p.name, aliases, contacts))
    return out


def _best(query: str, info: ClientInfo) -> tuple[float, str, str] | None:
    best = None
    candidates = [("name", info.name), *(("alias", a) for a in info.aliases),
                  *(("contact", c) for c in info.contacts)]
    for via, text in candidates:
        if not (text or "").strip():
            continue
        score = float(fuzz.ratio(query, fold(text)))
        if best is None or score > best[0]:
            best = (score, via, text)
    return best


def rank_clients(records: list[ClientInfo], query: str, limit: int = RANK_LIMIT) -> list[Hit]:
    """Top matches. An empty query lists folder names alphabetically (the caller already sorted them)."""
    q = fold(query.strip())
    if not q:
        return [Hit(r.name, 100.0, "name", r.name) for r in records[:limit]]
    hits = []
    for info in records:
        best = _best(q, info)
        if best and best[0] >= RANK_CUTOFF:
            hits.append(Hit(info.name, best[0], best[1], best[2]))
    hits.sort(key=lambda h: (-h.score, h.name.lower()))
    return hits[:limit]


def near_duplicate(name: str, records: list[ClientInfo]) -> str | None:
    """Existing client whose folder name or alias scores above 90 against `name`, if any."""
    folded = fold(sanitize_name(name))
    if not folded or folded == fold("Untitled"):
        return None
    best_name, best_score = None, 0.0
    for info in records:
        for text in (info.name, *info.aliases):
            score = float(fuzz.ratio(folded, fold(text)))
            if score > best_score:
                best_name, best_score = info.name, score
    if best_name is not None and best_score > NEAR_DUPLICATE:
        return best_name
    return None


def _yaml_scalar(value: str) -> str:
    """One YAML scalar. json.dumps quotes it; PyYAML's dump of a bare string adds a document end."""
    return json.dumps(value, ensure_ascii=False)


def set_frontmatter_key(text: str, key: str, value) -> str:
    """Replace one frontmatter key and keep the other lines as they are."""
    if isinstance(value, list):
        block = key + ":\n" + "\n".join(f"  - {_yaml_scalar(str(v))}" for v in value)
    else:
        block = yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True, width=1000).strip()
    fm, body = split_frontmatter(text)
    if not fm:
        return f"---\n{block}\n---\n{body}"
    lines = fm.splitlines()
    out, i = [], 0
    while i < len(lines):
        if re.match(rf"^{re.escape(key)}\s*:", lines[i]):
            i += 1
            while i < len(lines) and (lines[i].startswith(" ") or lines[i].startswith("\t")):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    if out and out[-1].strip() == "---":
        closing = out.pop()
        if out and out[-1] == "":
            out.pop()
        out.extend([block, closing])
    else:
        out.append(block)
    new_fm = "\n".join(out)
    if not new_fm.endswith("\n"):
        new_fm += "\n"
    return new_fm + body


def create_client(settings, name: str, *, aliases: list[str] | None = None, contact: str | None = None,
                  company: str | None = None, note: str | None = None) -> str:
    """Create the client folder (template, Calls/Transcripts/Work) and write the optional quick fields.

    Contact and company go to Snapshot and to `aliases`. The note goes to Snapshot only.
    Returns the sanitized folder name.
    """
    from .pipeline import Pipeline

    folder = sanitize_name(name)
    if not folder or folder == "Untitled":
        raise ValueError("Client name is empty")
    pipe = Pipeline(settings, None)
    pipe.ensure_client(folder)
    path = pipe.profile_path(folder)
    text = path.read_text(encoding="utf-8")
    existing = get_frontmatter(text).get("aliases") or []
    if isinstance(existing, str):
        existing = [existing]
    seen = set()
    merged: list[str] = []
    for item in [*existing, *(aliases or []), contact, company]:
        item = (item or "").strip()
        key = fold(item)
        if item and key not in seen:
            seen.add(key)
            merged.append(item)
    if merged:
        text = set_frontmatter_key(text, "aliases", merged)
    snap = get_section(text, "Snapshot") or ""
    lines = []
    if (contact or "").strip():
        lines.append(f"- Contact: {contact.strip()}")
    if (company or "").strip():
        lines.append(f"- Company: {company.strip()}")
    if (note or "").strip():
        lines.append(f"- {note.strip()}")
    lines = [ln for ln in lines if ln not in snap]
    if lines:
        text = append_to_section(text, "Snapshot", lines)
    if merged or lines:
        pipe.vault.write(path, text)
    log.info("Client %s ready", folder)
    return folder


# ------------------------------------------------------------------ recurring meetings
def _zoom_key(title: str) -> str | None:
    spaced = _ZOOM_SPACED.search(title)
    if spaced:
        digits = "".join(spaced.groups())
        if 9 <= len(digits) <= 11:
            return f"zoom:{digits}"
    plain = _ZOOM_PLAIN.search(title)
    if plain:
        return f"zoom:{plain.group(1)}"
    cleaned = _BROWSER_SUFFIX.sub("", title).strip()
    return f"zoom-title:{fold(cleaned)}" if cleaned else None


CHAT_APPS = frozenset({"whatsapp", "viber", "signal", "telegram", "messenger", "discord", "slack", "skype"})


def meeting_key(app: str, title: str) -> str | None:
    """Stable key for a recurring meeting, or None when the title doesn't identify one yet."""
    app_l = (app or "").lower()
    title = title or ""
    if "zoom" in app_l:
        return _zoom_key(title)
    if "teams" in app_l:
        cleaned = _TEAMS_SUFFIX.sub("", _BROWSER_SUFFIX.sub("", title)).strip()
        return f"teams:{fold(cleaned)}" if cleaned else None
    if "meet" in app_l:
        code = _MEET_CODE.search(title)
        return f"meet:{code.group(0)}" if code else None
    if app_l in CHAT_APPS:
        # found through the microphone; the call window is titled with the contact or group
        name = fold(title.strip())
        if name and name not in CHAT_APPS and name != f"{app_l} call":
            return f"{app_l}:{name}"
    return None


class MeetingMap:
    """`_System/meeting-map.json`: meeting key -> client chosen last time."""

    def __init__(self, path: Path, vault: Vault | None = None):
        self.path = Path(path)
        self.vault = vault
        self.data: dict = {}
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data = raw
            except ValueError:
                log.warning("Meeting map %s is unreadable; starting a new one", self.path)

    def get(self, key: str | None) -> str | None:
        if not key:
            return None
        entry = self.data.get(key)
        if isinstance(entry, str) and entry.strip():
            return entry.strip()
        if isinstance(entry, dict) and entry.get("client"):
            return str(entry["client"])
        return None

    def put(self, key: str, client: str) -> None:
        self.data[key] = {"client": client}
        payload = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        if self.vault is not None:
            self.vault.write(self.path, payload)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(payload, encoding="utf-8")
