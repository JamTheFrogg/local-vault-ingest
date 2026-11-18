import json
import os
from pathlib import Path

import httpx
import pytest

from conftest import fake_responder, make_transcript

from vault_ingest import cli
from vault_ingest.config import PROJECT_DIR, Settings, load_settings, set_llm_mode
from vault_ingest.gpu import fits_in_dedicated
from vault_ingest.llm import (CONTEXT_MSG, NO_MODEL_MSG, SERVER_DOWN_MSG, CompletionStats, FakeLLM, LLMError,
                              LLMTransient, LLMUnavailable, LocalStudioLLM, NoModelLoaded, extract_json)
from vault_ingest.pipeline import Pipeline
from vault_ingest.schemas import ClientGuess
from vault_ingest.vault import get_frontmatter

MODEL_ENV_KEYS = ("LLM_MODE", "LLM_PROVIDER", "LLM_MODEL", "LLM_CONCURRENCY", "LLM_TIMEOUT", "ANTHROPIC_API_KEY",
                  "DIRECT_SUMMARY_MAX_WORDS", "LOCAL_BASE_URL", "LOCAL_MODEL", "LOCAL_CONCURRENCY", "LOCAL_TIMEOUT",
                  "LOCAL_DIRECT_SUMMARY_MAX_WORDS", "LOCAL_CONTEXT", "LOCAL_THINKING",
                  "LOCAL_MANAGE_MODELS", "VAULT_PATH",
                  "OPENAI_COMPAT_BASE_URL")


@pytest.fixture
def clean_env():
    """load_dotenv writes into os.environ; restore it exactly after each test."""
    saved = dict(os.environ)
    for k in MODEL_ENV_KEYS:
        os.environ.pop(k, None)
    yield
    os.environ.clear()
    os.environ.update(saved)


def local_settings(vault: Path, model: str = "auto") -> Settings:
    return Settings(vault_path=vault, prompts_dir=PROJECT_DIR / "prompts", llm_mode="local",
                    llm_provider="openai_compat", llm_model=model)


def chat_reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


def studio(vault: Path, handler, model: str = "auto") -> LocalStudioLLM:
    return LocalStudioLLM(local_settings(vault, model), auto_start=False, transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ model detection
def test_auto_model_skips_embedding_models(vault):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v0/models":
            return httpx.Response(404, json={"error": "not found"})
        assert req.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "text-embedding-nomic-embed-text-v1.5"},
                                                  {"id": "qwen2.5-14b-instruct"}, {"id": "gemma-3-12b"}]})

    assert studio(vault, handler).resolve_model() == "qwen2.5-14b-instruct"


def test_auto_model_prefers_loaded_models_from_lm_studio_api(vault):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v0/models"
        return httpx.Response(200, json={"data": [
            {"id": "llama-3.3-70b", "type": "llm", "state": "not-loaded"},
            {"id": "text-embedding-bge", "type": "embeddings", "state": "loaded"},
            {"id": "qwen3-14b", "type": "llm", "state": "loaded"}]})

    assert studio(vault, handler).resolve_model() == "qwen3-14b"


def test_no_loaded_llm_gives_clear_message(vault):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v0/models":
            return httpx.Response(404)
        return httpx.Response(200, json={"data": [{"id": "text-embedding-nomic-embed-text-v1.5"}]})

    with pytest.raises(NoModelLoaded, match="Load a model in LM Studio"):
        studio(vault, handler).resolve_model()
    assert NO_MODEL_MSG == "Load a model in LM Studio (Developer tab) and start the server."


def test_explicit_local_model_skips_detection(vault):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/chat/completions", "no /models call when LOCAL_MODEL is set"
        assert json.loads(req.content)["model"] == "my-model"
        return chat_reply('{"client": "Test", "confidence": 1, "reason": "ok"}')

    assert studio(vault, handler, model="my-model").complete_json("s", "u", ClientGuess).client == "Test"


# ------------------------------------------------------------------ structured output
def test_json_schema_request_and_fallback_on_400(vault):
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3-14b", "type": "llm", "state": "loaded"}]})
        body = json.loads(req.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "'response_format' of type 'json_schema' is not supported"})
        return chat_reply('{"client": "Test", "confidence": 1, "reason": "ok"}')

    llm = studio(vault, handler)
    assert llm.complete_json("sys", "user", ClientGuess).client == "Test"
    rf = bodies[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"] == {"name": "ClientGuess", "strict": True, "schema": ClientGuess.model_json_schema()}
    assert "response_format" not in bodies[1]
    # the fallback is the plain prompt path: same messages, JSON rules in the user prompt
    assert bodies[1]["messages"] == bodies[0]["messages"]
    assert "Return ONLY a single JSON object" in bodies[1]["messages"][1]["content"]
    assert llm.structured is False

    llm.complete_json("sys", "user again", ClientGuess)
    assert len(bodies) == 3 and "response_format" not in bodies[2], "fallback is remembered for the run"


def test_json_schema_used_when_accepted(vault):
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return chat_reply('{"client": "Test", "confidence": 0.9, "reason": "ok"}')

    llm = studio(vault, handler, model="m")
    llm.complete_json("s", "u", ClientGuess)
    llm.complete_json("s", "u", ClientGuess)
    assert all(b["response_format"]["type"] == "json_schema" for b in bodies)


def test_context_length_error_is_explained_and_not_retried_as_fallback(vault, caplog):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(400, json={"error": "Trying to keep the first 9000 tokens when context the overflows. "
                                                  "However, the model is loaded with context length of only 4096"})

    with pytest.raises(LLMError, match="bigger context length"):
        studio(vault, handler, model="m").complete_json("s", "u", ClientGuess)
    assert len(calls) == 1
    assert "CHUNK_MAX_WORDS" in CONTEXT_MSG and "CHUNK_MAX_WORDS" in caplog.text


# ------------------------------------------------------------------ <think>
def test_think_blocks_are_stripped(vault):
    reply = '<think>The user wants {"client": "Wrong"}. Let me answer.</think>\n{"client": "Test", "confidence": 1, "reason": "ok"}'

    def handler(req: httpx.Request) -> httpx.Response:
        return chat_reply(reply)

    assert studio(vault, handler, model="m").complete_json("s", "u", ClientGuess).client == "Test"


def test_extract_json_handles_orphan_closing_think_tag():
    assert extract_json('reasoning with {"x": 1} braces</think>{"a": 2}') == {"a": 2}
    assert extract_json('<THINK>\n{"x": 1}\n</THINK>\n```json\n{"a": 3}\n```') == {"a": 3}


# ------------------------------------------------------------------ server down
def refuse(req: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=req)


def test_server_down_raises_unavailable(vault):
    with pytest.raises(LLMUnavailable, match="LM Studio is not running"):
        studio(vault, refuse).complete_json("s", "u", ClientGuess)
    assert SERVER_DOWN_MSG == "LM Studio is not running - start its server; will retry"


def test_server_down_keeps_files_in_inbox(vault, caplog):
    for name in ("Acme - 2026-09-30 - Kickoff.txt", "Acme - 2026-10-01 - Followup.txt"):
        (vault / "00_Inbox" / name).write_text(make_transcript(1500), encoding="utf-8")
    before = {p.relative_to(vault).as_posix(): p.read_bytes() for p in vault.rglob("*") if p.is_file()}

    p = Pipeline(local_settings(vault), studio(vault, refuse), concurrency=1)
    results = p.run_inbox()

    assert [r.status for r in results] == ["waiting", "waiting"]
    after = {p_.relative_to(vault).as_posix(): p_.read_bytes() for p_ in vault.rglob("*") if p_.is_file()}
    assert after == before, "nothing moved, nothing written"
    assert not (vault / "00_Inbox/_needs-review").exists()
    assert caplog.text.count("LM Studio is not running") == 1


def test_lms_server_start_is_tried_once(vault, monkeypatch, caplog):
    import subprocess

    from vault_ingest import llm as llm_mod

    runs = []
    monkeypatch.setattr(llm_mod.shutil, "which", lambda name: "C:/lms/lms.exe" if name == "lms" else None)
    monkeypatch.setattr(llm_mod.subprocess, "run",
                        lambda args, **kw: runs.append(args) or subprocess.CompletedProcess(args, 1, "", "no GUI"))
    llm = LocalStudioLLM(local_settings(vault, "m"), transport=httpx.MockTransport(refuse))
    for _ in range(2):
        with pytest.raises(LLMUnavailable):
            llm.complete_json("s", "u", ClientGuess)
    assert runs == [["C:/lms/lms.exe", "server", "start"]]
    assert "lms server start" in caplog.text


# ------------------------------------------------------------------ settings / mode command
def test_settings_profiles(vault, tmp_path, clean_env):
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    os.environ.update(VAULT_PATH=str(vault), LLM_PROVIDER="openai_compat", LLM_MODEL="claude-x",
                      OPENAI_COMPAT_BASE_URL="http://old:1234/v1", LLM_CONCURRENCY="4")

    s = load_settings(env)                                   # no LLM_MODE: old behaviour
    assert (s.llm_mode, s.llm_provider, s.llm_model, s.effective_mode) == (None, "openai_compat", "claude-x", "local")
    assert s.openai_compat_base_url == "http://old:1234/v1" and s.llm_concurrency == 4

    os.environ["LLM_MODE"] = "local"
    s = load_settings(env)
    assert (s.llm_provider, s.llm_model, s.openai_compat_base_url) == ("openai_compat", "auto", "http://localhost:1234/v1")
    assert (s.llm_concurrency, s.llm_timeout, s.direct_summary_max_words) == (1, 900.0, 3000)
    assert s.local_context == 8192 and s.local_thinking is False

    os.environ["LLM_MODE"] = "cloud"
    s = load_settings(env)
    assert (s.llm_provider, s.llm_model, s.effective_mode, s.llm_concurrency) == ("anthropic", "claude-x", "cloud", 4)
    assert s.direct_summary_max_words == 6000

    os.environ["LLM_MODE"] = "hybrid"
    with pytest.raises(SystemExit, match="LLM_MODE"):
        load_settings(env)


ENV_TEXT = (
    "# vault-ingest settings\r\n"
    "VAULT_PATH={vault}\r\n"
    "\r\n"
    "# LLM_MODE=local   <- a comment, must not be touched\r\n"
    "LLM_MODE=cloud   # toggled by vault-ingest mode\r\n"
    "LLM_MODEL=claude-sonnet-5-5\r\n"
    "ANTHROPIC_API_KEY=sk-ant-DUMMY-key-0123456789\r\n"
    "LOCAL_MODEL=auto\r\n"
    "NOTES_LANGUAGE=\"English, but keep quotes\"\r\n"
)


def test_mode_command_edits_only_llm_mode_line(vault, tmp_path, clean_env, monkeypatch, capsys):
    env = tmp_path / ".env"
    original = ENV_TEXT.format(vault=vault).encode("utf-8")
    env.write_bytes(original)
    seen = {}
    monkeypatch.setattr(cli, "check", lambda args: seen.setdefault("mode", os.environ.get("LLM_MODE")) and 0)

    assert cli.main(["--env", str(env), "mode", "local"]) == 0
    expected = original.replace(b"LLM_MODE=cloud   # toggled", b"LLM_MODE=local   # toggled")
    assert env.read_bytes() == expected
    assert seen["mode"] == "local", "connectivity check runs for the new mode"
    assert "sk-ant-DUMMY" not in capsys.readouterr().out

    assert cli.main(["--env", str(env), "mode", "cloud"]) == 0
    assert env.read_bytes() == original
    assert not list(tmp_path.glob(".env.*.tmp")), "no temp files left behind"


def test_set_llm_mode_adds_missing_line(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"VAULT_PATH=C:\\Vault\nANTHROPIC_API_KEY=sk-ant-DUMMY")        # no trailing newline
    set_llm_mode(env, "local")
    assert env.read_bytes() == b"VAULT_PATH=C:\\Vault\nANTHROPIC_API_KEY=sk-ant-DUMMY\nLLM_MODE=local\n"
    set_llm_mode(env, "cloud")
    assert env.read_bytes().endswith(b"ANTHROPIC_API_KEY=sk-ant-DUMMY\nLLM_MODE=cloud\n")
    assert env.read_bytes().count(b"LLM_MODE") == 1


def test_mode_switch_runs_real_check_without_printing_the_key(vault, tmp_path, clean_env, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_bytes(ENV_TEXT.format(vault=vault).encode("utf-8"))
    fake = FakeLLM(lambda s, u: '{"client": "Test", "confidence": 1, "reason": "ok"}')
    fake.model = "qwen3-14b"
    monkeypatch.setattr("vault_ingest.llm.make_llm", lambda s: fake)

    assert cli.main(["--env", str(env), "mode", "local"]) == 0
    out = capsys.readouterr().out
    assert "LLM answered: Test" in out and "local" in out
    assert "sk-ant-DUMMY" not in out

    monkeypatch.setattr(LocalStudioLLM, "resolve_model", lambda self: "qwen3-14b")
    assert cli.main(["--env", str(env), "mode"]) == 0
    out = capsys.readouterr().out
    assert "Mode:     local" in out and "auto -> qwen3-14b" in out and "sk-ant-DUMMY" not in out


# ------------------------------------------------------------------ generated_by
def lm_studio_handler(model_id: str = "qwen3-14b"):
    respond = fake_responder()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "nomic-embed-text"}, {"id": model_id}]})
        msgs = json.loads(req.content)["messages"]
        return chat_reply("<think>ok</think>" + respond(msgs[0]["content"], msgs[1]["content"]))

    return handler


def test_generated_by_is_written_everywhere(vault):
    (vault / "00_Inbox/Acme - 2026-09-30 - Kickoff.txt").write_text(make_transcript(2500), encoding="utf-8")
    p = Pipeline(local_settings(vault), studio(vault, lm_studio_handler()), concurrency=2)
    [r] = p.run_inbox()
    assert r.status == "processed", r.message

    assert get_frontmatter(r.call_note.read_text(encoding="utf-8"))["generated_by"] == "local/qwen3-14b"
    for part in r.chunk_notes:
        assert get_frontmatter(part.read_text(encoding="utf-8"))["generated_by"] == "local/qwen3-14b"
    [rec] = json.loads((vault / "_System/ingest-log.json").read_text(encoding="utf-8")).values()
    assert rec["generated_by"] == "local/qwen3-14b"


def test_reprocess_all_local_asks_first_and_uses_current_mode(vault, settings, monkeypatch, capsys):
    (vault / "00_Inbox/Acme - 2026-09-30 - Kickoff.txt").write_text(make_transcript(2500), encoding="utf-8")
    Pipeline(local_settings(vault), studio(vault, lm_studio_handler()), concurrency=1).run_inbox()
    log_path = vault / "_System/ingest-log.json"
    call = vault / "Clients/Acme/Calls/2026-09-30 Kickoff.md"
    before = call.read_bytes()

    settings.llm_mode = "cloud"
    cloud = FakeLLM(fake_responder())
    cloud.model = "claude-sonnet-5-5"
    p = Pipeline(settings, cloud, concurrency=1)
    assert len(p.local_records()) == 1

    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.reprocess_all_local(p) == []
    assert "1 call(s)" in capsys.readouterr().out
    assert call.read_bytes() == before and not cloud.calls

    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    [r] = cli.reprocess_all_local(p)
    assert r.status == "processed"
    assert get_frontmatter(call.read_text(encoding="utf-8"))["generated_by"] == "cloud/claude-sonnet-5-5"
    [rec] = json.loads(log_path.read_text(encoding="utf-8")).values()
    assert rec["generated_by"] == "cloud/claude-sonnet-5-5"
    assert Pipeline(settings, cloud).local_records() == []


def test_local_request_caps_tokens_and_disables_reasoning(vault):
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return chat_reply('{"client": "Test", "confidence": 1, "reason": "ok"}')

    studio(vault, handler, model="m").complete_json("s", "u", ClientGuess)
    assert bodies[0]["max_tokens"] == 150
    assert bodies[0]["reasoning_effort"] == "none"
    assert bodies[0]["stream"] is True


def test_call_note_thinking_follows_the_setting(vault):
    from vault_ingest.schemas import CallNote

    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return chat_reply('{"summary": "A short note."}')

    s = local_settings(vault, "m")
    s.local_thinking = True
    llm = LocalStudioLLM(s, auto_start=False, transport=httpx.MockTransport(handler))
    llm.complete_json("s", "u", CallNote)
    assert bodies[0]["reasoning_effort"] == "low"
    assert bodies[0]["max_tokens"] == 1500


def test_reasoning_stays_on_for_the_whole_request(vault):
    from vault_ingest.schemas import CallNote

    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        lines = ["data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "think "}}]})
                 for _ in range(40)]
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": '{"summary": "Done with thinking."}'}}]}))
        lines.append("data: [DONE]")
        return httpx.Response(200, text="\n".join(lines) + "\n")

    s = local_settings(vault, "m")
    s.local_thinking = True
    llm = LocalStudioLLM(s, auto_start=False, transport=httpx.MockTransport(handler))
    assert llm.complete_json("s", "u", CallNote).summary == "Done with thinking."
    assert len(bodies) == 1
    assert bodies[0]["reasoning_effort"] == "low" and bodies[0]["max_tokens"] == 1500


def test_stall_still_fires_while_thinking(vault):
    from vault_ingest.schemas import CallNote

    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body["reasoning_effort"])
        if len(seen) == 1:
            raise httpx.ReadTimeout("idle", request=req)
        return chat_reply('{"summary": "A short note."}')

    s = local_settings(vault, "m")
    s.local_thinking = True
    llm = LocalStudioLLM(s, auto_start=False, transport=httpx.MockTransport(handler))
    assert llm.complete_json("s", "u", CallNote).summary == "A short note."
    assert seen == ["low", "low"]


def test_sse_stream_is_assembled(vault):
    def handler(req: httpx.Request) -> httpx.Response:
        line = json.dumps({"choices": [{"delta": {"content": '{"client": "Test", "confidence": 1, "reason": "ok"}'}}]})
        usage = json.dumps({"usage": {"completion_tokens": 12,
                                      "completion_tokens_details": {"reasoning_tokens": 0}}})
        return httpx.Response(200, text=f"data: {line}\n\ndata: {usage}\n\ndata: [DONE]\n\n")

    llm = studio(vault, handler, model="m")
    assert llm.complete_json("s", "u", ClientGuess).client == "Test"
    assert llm.last_stats.output_tokens == 12
    assert llm.last_stats.reasoning_tokens == 0
    assert llm.last_stats.ttft_seconds is not None


def test_stall_retries_the_request_once(vault):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) == 1:
            raise httpx.ReadTimeout("idle", request=req)
        return chat_reply('{"client": "Test", "confidence": 1, "reason": "ok"}')

    assert studio(vault, handler, model="m").complete_json("s", "u", ClientGuess).client == "Test"
    assert len(seen) == 2


def test_timeout_stays_in_inbox_until_the_third_failure(vault):
    class Boom(FakeLLM):
        def __init__(self):
            super().__init__(lambda system, user: "{}")
            self.n = 0

        def _complete(self, system, user):
            self.n += 1
            raise LLMTransient("timed out", CompletionStats(no_tokens=True))

    boom = Boom()
    name = "Acme - 2026-09-30 - Kickoff.txt"
    (vault / "00_Inbox" / name).write_text(make_transcript(80), encoding="utf-8")
    p = Pipeline(local_settings(vault), boom, concurrency=1)
    p.llm_retry_delays = (0, 0, 0)
    [r] = p.run_inbox()
    assert r.status == "review"
    assert boom.n == 3
    assert not (vault / "00_Inbox" / name).exists()
    why = vault / "00_Inbox" / "_needs-review" / f"{name}.why.md"
    assert "no tokens" in why.read_text(encoding="utf-8")


def test_invalid_json_logs_the_output_and_the_retry_is_different(caplog):
    import logging

    from vault_ingest.schemas import ClientGuess

    caplog.set_level(logging.WARNING, logger="vault_ingest")
    seen = []

    def responder(system, user):
        seen.append(user)
        if "Return only the JSON object" not in user:
            return ""
        return '{"client": "Test", "confidence": 1, "reason": "ok"}'

    assert FakeLLM(responder).complete_json("s", "u", ClientGuess).client == "Test"
    assert seen[0] != seen[1]
    assert "Return only the JSON object" not in seen[0]
    assert "Return only the JSON object" in seen[1]
    assert "length 0" in caplog.text


def test_reasoning_only_json_is_accepted(vault):
    from vault_ingest.schemas import ProfileUpdate

    def handler(req: httpx.Request) -> httpx.Response:
        line = json.dumps({"choices": [{"delta": {"reasoning_content": '{"current_status": "From reasoning."}'}}]})
        return httpx.Response(200, text=f"data: {line}\n\ndata: [DONE]\n\n")

    llm = LocalStudioLLM(local_settings(vault, "m"), auto_start=False, transport=httpx.MockTransport(handler))
    assert llm.complete_json("s", "u", ProfileUpdate).current_status == "From reasoning."


def test_profile_update_does_not_request_thinking(vault):
    from vault_ingest.schemas import ProfileUpdate

    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return chat_reply('{"current_status": "Fine."}')

    s = local_settings(vault, "m")
    s.local_thinking = True
    llm = LocalStudioLLM(s, auto_start=False, transport=httpx.MockTransport(handler))
    assert llm.complete_json("s", "u", ProfileUpdate).current_status == "Fine."
    assert bodies[0]["reasoning_effort"] == "none"
    assert bodies[0]["max_tokens"] == 1200


def test_dedicated_vram_fit():
    assert fits_in_dedicated(7592, 8188) is True
    assert fits_in_dedicated(8188, 8188) is False
    assert fits_in_dedicated(None, 8188) is None
