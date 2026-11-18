"""LLM providers (Anthropic, Ollama, OpenAI-compatible e.g. LM Studio) with validated JSON output."""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings
from .gpu import GpuInterrupted, TTL_SECONDS, http_call, paused_now, run_external, timeout_label

log = logging.getLogger("vault_ingest")
T = TypeVar("T", bound=BaseModel)

JSON_RULES = (
    "\n\nReturn ONLY a single JSON object that matches this JSON schema. "
    "No markdown fences, no commentary before or after.\n\nJSON schema:\n{schema}"
)

THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
SERVER_DOWN_MSG = "LM Studio is not running - start its server; will retry"
NO_MODEL_MSG = "Load a model in LM Studio (Developer tab) and start the server."
CONTEXT_MSG = ("The prompt is too long for the model's context window. In LM Studio, load the model with a bigger "
               "context length or pick a model that supports one, or lower CHUNK_MAX_WORDS "
               "(and LOCAL_DIRECT_SUMMARY_MAX_WORDS) in .env.")
STALL_SECONDS = 90.0
CONTEXT_ERR_RE = re.compile(r"context (length|size|window)|n_ctx|too many tokens|exceeds the (available )?context"
                            r"|maximum context|context overflow", re.I)


class LLMError(RuntimeError):
    pass


@dataclass
class CompletionStats:
    """One streamed completion. `seconds` is wall time; `tokens_per_sec` is output tokens over the generation interval."""

    seconds: float = 0.0
    output_tokens: int = 0
    tokens_per_sec: float = 0.0
    reasoning_tokens: int = 0
    ttft_seconds: float | None = None
    no_tokens: bool = False

    def speed_phrase(self) -> str:
        if self.no_tokens or (self.output_tokens <= 0 and self.reasoning_tokens <= 0):
            return "no tokens"
        return f"{self.tokens_per_sec:.0f} tok/s"


class LLMUnavailable(LLMError):
    """The local server can't take work right now; the file should stay in the inbox and be retried."""


class LLMTransient(LLMError):
    """A timeout or LM Studio error. The file stays in the inbox and the whole job is tried again."""

    def __init__(self, message: str, stats: CompletionStats | None = None):
        super().__init__(message)
        self.stats = stats or CompletionStats(no_tokens=True)


class _Stall(Exception):
    def __init__(self, message: str, stats: CompletionStats):
        super().__init__(message)
        self.stats = stats


class _DropReasoning(Exception):
    pass


class _DropSchema(Exception):
    pass


def _tag_prefix_len(text: str, tag: str) -> int:
    """How many trailing characters of `text` are a prefix of `tag` (a tag split across deltas)."""
    low = text.lower()
    for n in range(min(len(tag) - 1, len(text)), 0, -1):
        if low.endswith(tag[:n]):
            return n
    return 0


class _ThinkSplit:
    """Separate `<think>` text from the answer when a model puts both in the content stream."""

    def __init__(self):
        self.in_think = False
        self.hold = ""

    def feed(self, piece: str) -> tuple[str, str]:
        data = self.hold + piece
        self.hold = ""
        answer: list[str] = []
        reason: list[str] = []
        while data:
            tag = "</think>" if self.in_think else "<think>"
            i = data.lower().find(tag)
            if i == -1:
                keep = _tag_prefix_len(data, tag)
                body, self.hold = (data[:-keep], data[-keep:]) if keep else (data, "")
                (reason if self.in_think else answer).append(body)
                break
            (reason if self.in_think else answer).append(data[:i])
            data = data[i + len(tag):]
            self.in_think = not self.in_think
        return "".join(answer), "".join(reason)

    def flush(self) -> tuple[str, str]:
        leftover, self.hold = self.hold, ""
        if not leftover:
            return "", ""
        return ("", leftover) if self.in_think else (leftover, "")


MAX_TOKENS = {
    "ChunkDigest": 400,
    "ClientGuess": 150,
    "CallNote": 1500,
    "ProfileUpdate": 1200,
    "ScreenshotDescription": 400,
    "EvidenceCheck": 1500,
}
THINKING_OPTIONAL = {"CallNote"}

BENCH_SYSTEM = "You index a short meeting excerpt. Be concrete. Write in English."
BENCH_USER = (
    "Client: Example Co\nCall: Kickoff (2026-09-30)\nChunk 1 of 1, time 00:00–00:02\n\n<chunk>\n"
    "Ana said the spring campaign budget is 12000 euros and the deadline is 15 March. "
    "They agreed to review the affiliate terms next Tuesday and to leave the trial plan unchanged.\n"
    "</chunk>\n\nReturn context (one sentence) and points (3-6 bullets)."
)


def _stats(started: float, first: float | None, content_events: int, reason_events: int,
           usage, no_tokens: bool) -> CompletionStats:
    end = time.perf_counter()
    wall = max(0.0, end - started)
    output = content_events
    reasoning = reason_events
    if isinstance(usage, dict):
        if usage.get("completion_tokens") is not None:
            output = int(usage["completion_tokens"])
        details = usage.get("completion_tokens_details") or {}
        if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
            reasoning = int(details["reasoning_tokens"])
            output = max(0, output - reasoning)
    gen = (end - first) if first is not None else wall
    rate = (output / gen) if gen > 0 and output else 0.0
    ttft = (first - started) if first is not None else None
    return CompletionStats(seconds=wall, output_tokens=output, tokens_per_sec=rate,
                           reasoning_tokens=reasoning, ttft_seconds=ttft, no_tokens=no_tokens)


class NoModelLoaded(LLMUnavailable):
    pass


class VisionUnsupported(LLMError):
    """The provider/model can't read images; screenshots are attached without descriptions."""


# a failed image request whose error text says this is about images, not the prompt
IMAGE_ERR_RE = re.compile(r"image|vision|multimodal|multi-modal|mmproj|modalit", re.I)
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".gif": "image/gif"}
IMAGE_MAX_EDGE = 1568            # Claude's recommended long edge; bigger images only cost more tokens
IMAGE_MAX_BYTES = 3_500_000      # stays under the 5 MB API limit once base64-encoded


@dataclass
class ImageInput:
    data: bytes
    media_type: str

    @classmethod
    def from_file(cls, path: Path) -> "ImageInput":
        path = Path(path)
        data = path.read_bytes()
        media_type = MEDIA_TYPES.get(path.suffix.lower(), "image/png")
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                if max(im.size) <= IMAGE_MAX_EDGE and len(data) <= IMAGE_MAX_BYTES:
                    return cls(data, media_type)
                im = im.convert("RGB")
                im.thumbnail((IMAGE_MAX_EDGE, IMAGE_MAX_EDGE))
                out = io.BytesIO()
                im.save(out, format="PNG", optimize=True)
                if out.tell() > IMAGE_MAX_BYTES:
                    out = io.BytesIO()
                    im.save(out, format="JPEG", quality=85)
                    return cls(out.getvalue(), "image/jpeg")
                return cls(out.getvalue(), "image/png")
        except Exception as e:  # noqa: BLE001  Pillow missing or odd file: send it as it is
            log.debug("Image not resized (%s): %s", path.name, e)
            return cls(data, media_type)

    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.b64()}"


def strip_thinking(text: str) -> str:
    text = THINK_RE.sub("", text)
    if "</think>" in text.lower():       # chat templates that open <think> in the prompt leave only the closing tag
        text = re.split(r"</think>", text, flags=re.I)[-1]
    return text


def extract_json(text: str) -> dict:
    text = strip_thinking(text).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in model output")
    return json.loads(text[start:end + 1])


class LLM:
    """Base class: subclasses implement `_complete(system, user) -> str`."""

    name = "base"

    def _complete(self, system: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def _complete_schema(self, system: str, user: str, schema: type[BaseModel]) -> str:
        """Providers with native structured output override this; the default is the plain prompt."""
        return self._complete(system, user)

    def _complete_image(self, system: str, user: str, image: ImageInput, schema: type[BaseModel]) -> str:
        raise VisionUnsupported(f"The {self.name} provider can't read images.")

    def complete_json(self, system: str, user: str, schema: type[T], retries: int = 2,
                      image: ImageInput | None = None) -> T:
        schema_txt = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        prompt = user + JSON_RULES.format(schema=schema_txt)
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            if image is not None:
                raw = self._complete_image(system, prompt, image, schema)
            else:
                raw = self._complete_schema(system, prompt, schema)
            try:
                return schema.model_validate(extract_json(raw))
            except (ValueError, ValidationError, json.JSONDecodeError) as e:
                last_err = e
                raw_text = raw if isinstance(raw, str) else str(raw or "")
                spent = getattr(getattr(self, "last_stats", None), "output_tokens", 0)
                log.warning("LLM output invalid (attempt %d/%d): %s; length %d; output tokens %s; output: %s",
                            attempt + 1, retries + 1, str(e)[:300], len(raw_text), spent, raw_text[:500])
                prompt = (
                    user + JSON_RULES.format(schema=schema_txt)
                    + "\n\nReturn only the JSON object."
                    + f"\n\nYour previous answer was invalid ({str(e)[:300]}). Return valid JSON only."
                )
        raise LLMError(f"LLM did not return valid {schema.__name__} after {retries + 1} attempts: {last_err}")

    def complete_json_image(self, system: str, user: str, image: ImageInput, schema: type[T], retries: int = 2) -> T:
        """Like `complete_json` with one image attached. Raises VisionUnsupported for text-only models."""
        return self.complete_json(system, user, schema, retries, image=image)


class AnthropicLLM(LLM):
    name = "anthropic"

    def __init__(self, s: Settings):
        if not s.anthropic_api_key:
            raise SystemExit("Cloud mode (Claude API) needs ANTHROPIC_API_KEY in .env")
        import anthropic

        self.client = anthropic.Anthropic(api_key=s.anthropic_api_key, timeout=s.llm_timeout, max_retries=3)
        self.model = s.llm_model

    def _complete(self, system: str, user: str) -> str:
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def _complete_image(self, system: str, user: str, image: ImageInput, schema: type[BaseModel]) -> str:
        import anthropic

        content = [{"type": "image", "source": {"type": "base64", "media_type": image.media_type, "data": image.b64()}},
                   {"type": "text", "text": user}]
        try:
            msg = self.client.messages.create(model=self.model, max_tokens=2000, system=system,
                                              messages=[{"role": "user", "content": content}])
        except anthropic.BadRequestError as e:
            if IMAGE_ERR_RE.search(str(e)):
                raise VisionUnsupported(f"Claude rejected the image: {str(e)[:200]}") from e
            raise
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


class OllamaLLM(LLM):
    name = "ollama"

    def __init__(self, s: Settings):
        self.base = s.ollama_base_url.rstrip("/")
        self.model = s.llm_model
        self.http = httpx.Client(timeout=s.llm_timeout)

    def _complete(self, system: str, user: str) -> str:
        r = self.http.post(f"{self.base}/api/chat", json={
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2, "num_ctx": 32768},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        })
        if r.status_code >= 400:
            raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]}")
        return r.json()["message"]["content"]

    def _complete_image(self, system: str, user: str, image: ImageInput, schema: type[BaseModel]) -> str:
        r = self.http.post(f"{self.base}/api/chat", json={
            "model": self.model, "stream": False, "format": "json", "options": {"temperature": 0.2},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user, "images": [image.b64()]}],
        })
        if r.status_code >= 400:
            if IMAGE_ERR_RE.search(r.text):
                raise VisionUnsupported(f"Ollama model can't read images: {r.text[:200]}")
            raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]}")
        return r.json()["message"]["content"]


def image_content(user: str, image: ImageInput) -> list[dict]:
    """OpenAI-style user content with an inline base64 image."""
    return [{"type": "text", "text": user}, {"type": "image_url", "image_url": {"url": image.data_url()}}]


class OpenAICompatLLM(LLM):
    """Any /v1/chat/completions server: LM Studio, vLLM, OpenRouter, OpenAI."""

    name = "openai_compat"

    def __init__(self, s: Settings):
        self.base = s.openai_compat_base_url.rstrip("/")
        self.model = s.llm_model
        self.http = httpx.Client(timeout=s.llm_timeout,
                                 headers={"Authorization": f"Bearer {s.openai_compat_api_key}"})

    def _complete(self, system: str, user: str) -> str:
        r = self.http.post(f"{self.base}/chat/completions", json={
            "model": self.model,
            "temperature": 0.2,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        })
        if r.status_code >= 400:
            raise LLMError(f"LLM server error {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"]

    def _complete_image(self, system: str, user: str, image: ImageInput, schema: type[BaseModel]) -> str:
        r = self.http.post(f"{self.base}/chat/completions", json={
            "model": self.model, "temperature": 0.2,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": image_content(user, image)}],
        })
        if r.status_code >= 400:
            if IMAGE_ERR_RE.search(r.text):
                raise VisionUnsupported(f"The model can't read images: {r.text[:200]}")
            raise LLMError(f"LLM server error {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"] or ""


class LocalStudioLLM(LLM):
    """LM Studio in local mode: auto model pick, json_schema structured output, clear errors when it's not running."""

    name = "local"

    def __init__(self, s: Settings, auto_start: bool = True, transport: httpx.BaseTransport | None = None):
        self.s = s
        self.base = s.openai_compat_base_url.rstrip("/")
        self.root = re.sub(r"/v1$", "", self.base)
        configured = (s.llm_model or "auto").strip()
        self.model: str | None = None if configured.lower() == "auto" else configured
        self.auto_start = auto_start
        self.structured = True                 # flipped off for the rest of the run if the server rejects json_schema
        self._reasoning_ok = True              # flipped off if the server rejects reasoning_effort
        self.last_stats = CompletionStats()
        self.manage_models = bool(getattr(s, "local_manage_models", True))
        self.gate = None                       # WorkGate; closing `http` from another thread cancels the request
        self._lms_tried = False
        self._lock = threading.RLock()
        self._client_kwargs = dict(timeout=s.llm_timeout, transport=transport,
                                   headers={"Authorization": f"Bearer {s.openai_compat_api_key}"})
        self.http = httpx.Client(**self._client_kwargs)

    def _raise_if_held(self) -> None:
        if (self.gate is not None and self.gate.cancel.is_set()) or paused_now(self.s):
            raise GpuInterrupted("GPU hold")

    def _reopen(self) -> None:
        self.http = httpx.Client(**self._client_kwargs)
        if self.gate is not None:
            self.gate.bind(self.http)

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        if self.gate is not None:
            self.gate.check()
        timeout = kw.get("timeout", self.s.llm_timeout)
        label = f"HTTP {method} {url}"
        log.info("Starting %s (timeout %s)", label, timeout_label(timeout))
        try:
            response = self.http.request(method, url, **kw)
        except RuntimeError as e:
            if "closed" not in str(e).lower():
                log.info("Finished %s: %s", label, e)
                raise
            self._reopen()
            if self.gate is not None and self.gate.cancel.is_set():
                raise GpuInterrupted("GPU hold") from e
            try:
                response = self.http.request(method, url, **kw)
            except httpx.HTTPError as e2:
                log.info("Finished %s: %s", label, e2)
                raise
        except httpx.HTTPError as e:
            log.info("Finished %s: %s", label, e)
            raise
        log.info("Finished %s: %s", label, response.status_code)
        return response

    # ---- connection
    def _send(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            r = self._request(method, url, **kw)
        except GpuInterrupted:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            if not self._try_start_server():
                raise LLMUnavailable(SERVER_DOWN_MSG) from e
            try:
                r = self._request(method, url, **kw)
            except GpuInterrupted:
                raise
            except (httpx.ConnectError, httpx.ConnectTimeout) as e2:
                raise LLMUnavailable(SERVER_DOWN_MSG) from e2
        self._lms_tried = False                # reachable again: a later outage may try `lms` once more
        return r

    def _try_start_server(self) -> bool:
        """Run `lms server start` once per outage if the LM Studio CLI is on PATH. True if the server then answers."""
        with self._lock:
            if not self.auto_start or self._lms_tried:
                return False
            self._lms_tried = True
            lms = shutil.which("lms")
            if not lms:
                log.info("LM Studio server is not reachable at %s and the `lms` CLI is not on PATH, "
                         "so it can't be started automatically.", self.base)
                return False
            log.info("LM Studio server is not reachable; trying `lms server start` ...")
            try:
                p = run_external([lms, "server", "start"], timeout=60, label="lms server start",
                                 run=subprocess.run)
            except (OSError, subprocess.TimeoutExpired) as e:
                log.warning("`lms server start` failed: %s", e)
                return False
            out = " ".join(((p.stdout or "") + " " + (p.stderr or "")).split())[-300:]
            if p.returncode != 0:
                log.warning("`lms server start` exited with code %d: %s", p.returncode, out)
                return False
            log.info("`lms server start` finished: %s", out or "ok")
            for _ in range(15):
                try:
                    http_call("GET", f"{self.base}/models", timeout=5, label="LM Studio port 1234",
                              client=self.http)
                    log.info("LM Studio server is up.")
                    return True
                except httpx.HTTPError:
                    time.sleep(1)
            log.warning("LM Studio server still not reachable after `lms server start`.")
            return False

    # ---- model
    def _loaded_llms(self) -> list[str]:
        # LM Studio's own API says which models are actually loaded; /v1/models may list every downloaded
        # model when just-in-time loading is on
        try:
            r = self._send("GET", f"{self.root}/api/v0/models")
            data = r.json().get("data") if r.status_code == 200 else None
            if isinstance(data, list) and data and all(isinstance(m, dict) and "state" in m for m in data):
                return [m["id"] for m in data if m.get("state") == "loaded" and m.get("type") != "embeddings"
                        and "embed" not in str(m.get("id", "")).lower()]
        except (ValueError, AttributeError, KeyError):
            pass
        r = self._send("GET", f"{self.base}/models")
        if r.status_code >= 400:
            raise LLMError(f"LM Studio /models error {r.status_code}: {r.text[:300]}")
        ids = [str(m.get("id", "")) for m in r.json().get("data", []) if isinstance(m, dict)]
        return [i for i in ids if i and "embed" not in i.lower()]

    def resolve_model(self) -> str:
        if self.model:
            return self.model
        with self._lock:
            if not self.model:
                models = self._loaded_llms()
                if not models:
                    raise NoModelLoaded(NO_MODEL_MSG)
                self.model = models[0]
                log.info("LM Studio model (auto): %s", self.model)
        return self.model

    # ---- completion
    def _answer_tokens(self, schema: type[BaseModel] | None) -> int:
        return MAX_TOKENS.get(schema.__name__ if schema is not None else "", 400)

    def _max_tokens(self, schema: type[BaseModel] | None) -> int:
        return self._answer_tokens(schema)

    def _reasoning_effort(self, schema: type[BaseModel] | None) -> str:
        name = schema.__name__ if schema is not None else ""
        if name in THINKING_OPTIONAL and getattr(self.s, "local_thinking", False):
            return "low"
        return "none"

    def _body(self, system: str, user: str, schema: type[BaseModel] | None,
              image: ImageInput | None) -> dict:
        body = {
            "model": self.resolve_model(),
            "temperature": 0.2,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": self._max_tokens(schema),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": image_content(user, image) if image else user}],
        }
        if self._reasoning_ok:
            body["reasoning_effort"] = self._reasoning_effort(schema)
        if self.manage_models:
            body["ttl"] = TTL_SECONDS
        if schema is not None and self.structured:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "strict": True, "schema": schema.model_json_schema()}}
        return body

    def _chat(self, system: str, user: str, schema: type[BaseModel] | None, image: ImageInput | None = None) -> str:
        body = self._body(system, user, schema, image)
        dropped = False
        stalls = 0
        while True:
            try:
                text, stats = self._read_once(body, image)
                self.last_stats = stats
                return text
            except _DropSchema:
                continue
            except _DropReasoning:
                if dropped:
                    raise LLMTransient("LM Studio rejected reasoning_effort", CompletionStats(no_tokens=True))
                dropped = True
                self._reasoning_ok = False
                body.pop("reasoning_effort", None)
                body["max_tokens"] = self._answer_tokens(schema)
                log.warning("LM Studio rejected reasoning_effort; continuing without it.")
            except _Stall as e:
                stalls += 1
                self.last_stats = e.stats
                if stalls <= 1:
                    log.warning("No tokens for %.0f s; retrying the request once.", STALL_SECONDS)
                    continue
                raise LLMTransient(f"no tokens for {STALL_SECONDS:.0f}s", e.stats) from e

    def _read_once(self, body: dict, image: ImageInput | None) -> tuple[str, CompletionStats]:
        url = f"{self.base}/chat/completions"
        timeout = httpx.Timeout(30.0, read=STALL_SECONDS, write=60.0, pool=30.0)

        label = f"HTTP POST {url}"

        def open_stream():
            self._raise_if_held()
            return self.http.stream("POST", url, json=body, timeout=timeout)

        log.info("Starting %s (timeout %s)", label, timeout_label(timeout))
        try:
            with open_stream() as resp:
                try:
                    text, stats = self._consume(resp, body, image)
                except httpx.HTTPError as e:
                    log.info("Finished %s: %s", label, e)
                    raise
                log.info("Finished %s: %s", label, resp.status_code)
                return text, stats
        except GpuInterrupted:
            raise
        except _DropReasoning:
            raise
        except _Stall:
            raise
        except LLMTransient:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            log.info("Finished %s: %s", label, e)
            if not self._try_start_server():
                raise LLMUnavailable(SERVER_DOWN_MSG) from e
            log.info("Starting %s (timeout %s)", label, timeout_label(timeout))
            try:
                with open_stream() as resp:
                    text, stats = self._consume(resp, body, image)
                    log.info("Finished %s: %s", label, resp.status_code)
                    return text, stats
            except (httpx.ConnectError, httpx.ConnectTimeout) as e2:
                log.info("Finished %s: %s", label, e2)
                raise LLMUnavailable(SERVER_DOWN_MSG) from e2
        except RuntimeError as e:
            if "closed" not in str(e).lower():
                log.info("Finished %s: %s", label, e)
                raise
            self._reopen()
            if self.gate is not None and self.gate.cancel.is_set():
                raise GpuInterrupted("GPU hold") from e
            log.info("Starting %s (timeout %s)", label, timeout_label(timeout))
            with open_stream() as resp:
                text, stats = self._consume(resp, body, image)
                log.info("Finished %s: %s", label, resp.status_code)
                return text, stats
        except httpx.TimeoutException as e:
            log.info("Finished %s: timed out (%s)", label, e)
            raise _Stall("timed out", CompletionStats(no_tokens=True)) from e
        except httpx.HTTPError as e:
            log.info("Finished %s: %s", label, e)
            raise LLMTransient(f"LM Studio error: {e}", CompletionStats(no_tokens=True)) from e

    def _consume(self, resp: httpx.Response, body: dict, image: ImageInput | None) -> tuple[str, CompletionStats]:
        if resp.status_code >= 400:
            raw = resp.read().decode("utf-8", "replace")
            self._raise_http(resp.status_code, raw, body, image)
        started = time.perf_counter()
        first: float | None = None
        parts: list[str] = []
        reason_parts: list[str] = []
        content_events = 0
        reason_events = 0
        usage = None
        plain: list[str] = []
        splitter = _ThinkSplit()

        def absorb(answer: str, reason: str) -> None:
            nonlocal content_events, reason_events, first
            if answer or reason:
                if first is None:
                    first = time.perf_counter()
            if answer:
                content_events += 1
                parts.append(answer)
            if reason:
                reason_events += 1
                reason_parts.append(reason)

        try:
            for line in resp.iter_lines():
                self._raise_if_held()
                if not line:
                    continue
                if not line.startswith("data:"):
                    plain.append(line)
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    absorb(*splitter.flush())
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = delta.get("content") or ""
                    reason = delta.get("reasoning_content") or ""
                    if isinstance(reason, dict):
                        reason = json.dumps(reason)
                    answer, thought = splitter.feed(piece) if piece else ("", "")
                    absorb(answer, thought + str(reason or ""))
            else:
                absorb(*splitter.flush())
        except httpx.TimeoutException as e:
            stats = _stats(started, first, content_events, reason_events, usage, no_tokens=first is None)
            raise _Stall("timed out", stats) from e
        except httpx.HTTPError as e:
            raise LLMTransient(f"LM Studio error: {e}", CompletionStats(no_tokens=first is None)) from e
        if not parts and not reason_parts and plain:
            blob = "\n".join(plain).strip()
            if blob.startswith("{"):
                try:
                    msg = json.loads(blob)["choices"][0]["message"]
                except (KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
                    raise LLMTransient(f"LM Studio returned an unreadable response: {blob[:200]}") from e
                content = msg.get("content") or ""
                reason = msg.get("reasoning_content") or ""
                if isinstance(reason, dict):
                    reason = json.dumps(reason)
                whole = _ThinkSplit()
                answer, thought = whole.feed(content)
                answer2, thought2 = whole.flush()
                absorb(answer + answer2, thought + thought2 + str(reason or ""))
        stats = _stats(started, first, content_events, reason_events, usage, no_tokens=not parts and not reason_parts)
        text = "".join(parts)
        reason = "".join(reason_parts)
        if "{" not in strip_thinking(text) and reason.strip():
            recovered = strip_thinking(reason).strip()
            if "{" in recovered:
                log.info("Answer had no JSON object; using the reasoning text (%d characters).", len(recovered))
                text = recovered
        return text, stats

    def _raise_http(self, status: int, raw: str, body: dict, image: ImageInput | None) -> None:
        if image is not None and IMAGE_ERR_RE.search(raw):
            raise VisionUnsupported(f"The LM Studio model '{body['model']}' can't read images "
                                    f"(load a vision model to get screenshot descriptions): {raw[:200]}")
        if status == 400 and CONTEXT_ERR_RE.search(raw):
            log.error(CONTEXT_MSG)
            raise LLMTransient(f"{CONTEXT_MSG} (server said: {raw[:300]})")
        if status == 400 and "response_format" in body and self.structured:
            self.structured = False
            log.warning("LM Studio rejected structured output (json_schema); using plain JSON prompts for the "
                        "rest of this run. Server said: %s", raw[:200])
            body.pop("response_format")
            raise _DropSchema()
        if status == 400 and "reasoning_effort" in body and (
                "reasoning" in raw.lower() or "unknown" in raw.lower() or "unrecognized" in raw.lower()):
            raise _DropReasoning()
        if status == 404 and "model" in raw.lower():
            self._raise_if_held()
            raise NoModelLoaded(f"{NO_MODEL_MSG} (model '{body['model']}' not found: {raw[:200]})")
        self._raise_if_held()
        raise LLMTransient(f"LM Studio error {status}: {raw[:300]}")

    def _complete(self, system: str, user: str) -> str:
        return self._chat(system, user, None)

    def _complete_schema(self, system: str, user: str, schema: type[BaseModel]) -> str:
        return self._chat(system, user, schema)

    def _complete_image(self, system: str, user: str, image: ImageInput, schema: type[BaseModel]) -> str:
        return self._chat(system, user, schema, image)


class FakeLLM(LLM):
    """For tests / --fake runs: a function maps (system, user) -> JSON string.
    Without `vision_responder` it behaves like a model that can't read images."""

    name = "fake"

    def __init__(self, responder: Callable[[str, str], str],
                 vision_responder: Callable[[str, str, ImageInput], str] | None = None):
        self.responder = responder
        self.vision_responder = vision_responder
        self.calls: list[tuple[str, str]] = []
        self.image_calls: list[tuple[str, str, ImageInput]] = []

    def _complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.responder(system, user)

    def _complete_image(self, system: str, user: str, image: ImageInput, schema: type[BaseModel]) -> str:
        if self.vision_responder is None:
            raise VisionUnsupported("fake model has no vision")
        self.image_calls.append((system, user, image))
        return self.vision_responder(system, user, image)


def make_llm(s: Settings) -> LLM:
    if s.llm_mode == "local":
        return LocalStudioLLM(s)
    if s.llm_mode == "cloud":
        return AnthropicLLM(s)
    providers = {"anthropic": AnthropicLLM, "ollama": OllamaLLM, "openai_compat": OpenAICompatLLM,
                 "lmstudio": OpenAICompatLLM}
    if s.llm_provider not in providers:
        raise SystemExit(f"Unknown LLM_PROVIDER '{s.llm_provider}'. Use: anthropic, ollama, openai_compat")
    return providers[s.llm_provider](s)


# ------------------------------------------------------------------ prompts
class Prompts:
    """Loads editable prompt files from prompts/. Placeholders look like {{name}}."""

    def __init__(self, directory: Path):
        self.dir = directory

    def render(self, name: str, **values: str) -> tuple[str, str]:
        path = self.dir / f"{name}.md"
        if not path.exists():
            raise SystemExit(f"Missing prompt file: {path}")
        text = path.read_text(encoding="utf-8")
        missing = set(re.findall(r"\{\{(\w+)\}\}", text)) - set(values)
        if missing:
            raise ValueError(f"Prompt {name} has unfilled placeholders: {sorted(missing)}")
        # split the template first so transcript text can never be mistaken for a section header
        m = re.search(r"^## System\s*\n(.*?)^## User\s*\n(.*)$", text, re.S | re.M)
        if not m:
            raise SystemExit(f"Prompt {path} must contain '## System' and '## User' sections")

        def fill(part: str) -> str:  # single pass: inserted values are never re-scanned
            return re.sub(r"\{\{(\w+)\}\}", lambda mm: str(values[mm.group(1)]), part).strip()

        return fill(m.group(1)), fill(m.group(2))
