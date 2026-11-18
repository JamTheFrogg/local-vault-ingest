"""Configuration loaded from .env / environment variables."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
DEFAULT_ENV_FILE = PROJECT_DIR / ".env"

LLM_MODES = ("cloud", "local")
LOCAL_PROVIDERS = ("openai_compat", "lmstudio", "ollama")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    vault_path: Path
    llm_mode: str | None = None              # cloud | local; None = follow llm_provider (older .env files)
    llm_provider: str = "anthropic"          # anthropic | ollama | openai_compat
    llm_model: str = "claude-sonnet-5-5"     # in local mode: LOCAL_MODEL ("auto" = whatever LM Studio has loaded)
    llm_concurrency: int = 4
    anthropic_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    openai_compat_base_url: str = "http://localhost:1234/v1"   # LM Studio default
    openai_compat_api_key: str = "lm-studio"
    llm_timeout: float = 300.0
    notes_language: str = "the same language as the transcript"
    chunk_min_words: int = 600
    chunk_max_words: int = 900
    chunk_overlap_words: int = 100
    direct_summary_max_words: int = 6000     # longer transcripts are summarized chunk-by-chunk first
    profile_max_words: int = 1500
    log_keep_entries: int = 15               # entries kept per log section before archiving older ones
    client_confidence: float = 0.75
    watch_debounce_seconds: float = 10.0
    meeting_match_tolerance_minutes: float = 15.0   # transcript <-> companion meeting session time matching
    transcribe_language: str = "hr"
    transcribe_model: str = "large-v3"
    transcribe_device: str = "auto"          # auto | cuda | cpu
    me_name: str = "Faris"                   # label for a separate microphone track
    transcript_source: str = "whisper"       # whisper | meetily
    local_context: int = 8192               # context length passed to `lms load` in local mode
    local_thinking: bool = False            # reasoning on the call note; profile updates stay off so the answer is JSON
    local_manage_models: bool = True        # load and unload the chat model; embeddings are never touched
    prompts_dir: Path = field(default_factory=lambda: PROJECT_DIR / "prompts")

    # ---- vault layout (relative to vault root) ----
    inbox_dir: str = "00_Inbox"
    review_dir: str = "00_Inbox/_needs-review"
    clients_dir: str = "Clients"
    system_dir: str = "_System"
    template_path: str = "_System/Templates/Client.md"

    @property
    def inbox(self) -> Path:
        return self.vault_path / self.inbox_dir

    @property
    def review(self) -> Path:
        return self.vault_path / self.review_dir

    @property
    def clients(self) -> Path:
        return self.vault_path / self.clients_dir

    @property
    def system(self) -> Path:
        return self.vault_path / self.system_dir

    @property
    def effective_mode(self) -> str:
        """cloud | local, also for older .env files that only set LLM_PROVIDER."""
        if self.llm_mode:
            return self.llm_mode
        if self.llm_provider == "anthropic":
            return "cloud"
        if self.llm_provider in LOCAL_PROVIDERS:
            return "local"
        return self.llm_provider


def read_llm_mode() -> str | None:
    raw = (os.getenv("LLM_MODE") or "").strip().strip("\"'").lower()
    if not raw:
        return None
    if raw not in LLM_MODES:
        raise SystemExit(f"LLM_MODE must be 'cloud' or 'local' (got '{raw}').")
    return raw


def load_settings(env_file: str | Path | None = None, **overrides) -> Settings:
    if env_file:
        load_dotenv(env_file, override=False)
    else:
        load_dotenv(DEFAULT_ENV_FILE, override=False)
        load_dotenv(override=False)  # also a .env in the current directory

    vault = overrides.pop("vault_path", None) or os.getenv("VAULT_PATH")
    if not vault:
        raise SystemExit("VAULT_PATH is not set. Put it in .env (see .env.example).")
    vault_path = Path(vault).expanduser().resolve()
    if not vault_path.is_dir():
        raise SystemExit(f"VAULT_PATH does not exist or is not a folder: {vault_path}")

    mode = read_llm_mode()
    s = Settings(
        vault_path=vault_path,
        llm_mode=mode,
        llm_provider=os.getenv("LLM_PROVIDER", "anthropic").strip().lower(),
        llm_model=os.getenv("LLM_MODEL", "claude-sonnet-5-5"),
        llm_concurrency=_int("LLM_CONCURRENCY", 4),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        openai_compat_base_url=os.getenv("OPENAI_COMPAT_BASE_URL", "http://localhost:1234/v1"),
        openai_compat_api_key=os.getenv("OPENAI_COMPAT_API_KEY", "lm-studio"),
        llm_timeout=_float("LLM_TIMEOUT", 300.0),
        notes_language=os.getenv("NOTES_LANGUAGE") or "the same language as the transcript",
        chunk_min_words=_int("CHUNK_MIN_WORDS", 600),
        chunk_max_words=_int("CHUNK_MAX_WORDS", 900),
        chunk_overlap_words=_int("CHUNK_OVERLAP_WORDS", 100),
        direct_summary_max_words=_int("DIRECT_SUMMARY_MAX_WORDS", 6000),
        profile_max_words=_int("PROFILE_MAX_WORDS", 1500),
        log_keep_entries=_int("LOG_KEEP_ENTRIES", 15),
        client_confidence=_float("CLIENT_CONFIDENCE", 0.75),
        watch_debounce_seconds=_float("WATCH_DEBOUNCE_SECONDS", 10.0),
        meeting_match_tolerance_minutes=_float("MEETING_MATCH_TOLERANCE_MINUTES", 15.0),
        transcribe_language=(os.getenv("TRANSCRIBE_LANGUAGE") or "hr").strip().lower(),
        transcribe_model=(os.getenv("TRANSCRIBE_MODEL") or "large-v3").strip(),
        transcribe_device=(os.getenv("TRANSCRIBE_DEVICE") or "auto").strip().lower(),
        me_name=(os.getenv("ME_NAME") or "Faris").strip() or "Faris",
        transcript_source=(os.getenv("TRANSCRIPT_SOURCE") or "whisper").strip().lower(),
        local_context=_int("LOCAL_CONTEXT", 8192),
        local_thinking=_bool("LOCAL_THINKING", False),
        local_manage_models=_bool("LOCAL_MANAGE_MODELS", True),
    )
    if s.transcript_source not in ("whisper", "meetily"):
        raise SystemExit("TRANSCRIPT_SOURCE must be 'whisper' or 'meetily' "
                         f"(got {s.transcript_source!r}).")
    if mode == "cloud":
        s.llm_provider = "anthropic"
    elif mode == "local":
        s.llm_provider = "openai_compat"
        s.llm_model = (os.getenv("LOCAL_MODEL") or "auto").strip()
        s.openai_compat_base_url = os.getenv("LOCAL_BASE_URL") or "http://localhost:1234/v1"
        s.llm_concurrency = _int("LOCAL_CONCURRENCY", 1)
        s.llm_timeout = _float("LOCAL_TIMEOUT", 900.0)
        s.direct_summary_max_words = _int("LOCAL_DIRECT_SUMMARY_MAX_WORDS", 3000)
    if os.getenv("PROMPTS_DIR"):
        s.prompts_dir = Path(os.environ["PROMPTS_DIR"]).expanduser().resolve()
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


_MODE_LINE_RE = re.compile(r"^(\ufeff?[ \t]*(?:export[ \t]+)?LLM_MODE[ \t]*=[ \t]*)(\S*)(.*)$")


def set_llm_mode(env_file: str | Path, mode: str) -> Path:
    """Rewrite only the LLM_MODE line of a .env file (added if missing). Atomic; other bytes stay as they were."""
    if mode not in LLM_MODES:
        raise ValueError(f"mode must be one of {LLM_MODES}")
    path = Path(env_file)
    # surrogateescape round-trips any byte, newline="" keeps \r\n vs \n as-is
    text = ""
    if path.exists():
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as fh:
            text = fh.read()
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        m = _MODE_LINE_RE.match(body)
        if not m:
            continue
        rest = m.group(3) if re.match(r"^[ \t]+#", m.group(3)) else ""
        lines[i] = m.group(1) + mode + rest + line[len(body):]
        break
    else:
        newline = "\r\n" if "\r\n" in text else "\n"
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        lines.append(f"LLM_MODE={mode}{newline}")
    new_text = "".join(lines)

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            fh.write(new_text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path
