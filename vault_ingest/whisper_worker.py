"""Child process for one Whisper load or one transcription.

A native crash in ctranslate2 stays in this process. The parent reads the JSON result and stderr.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

log = logging.getLogger("vault_ingest")


def _load(job: dict) -> dict:
    from .transcribe import load_whisper_exact, release_model

    model, device, compute = load_whisper_exact(job["model"], job["device"], job["compute"])
    release_model(model)
    return {"ok": True, "device": device, "compute_type": compute}


def _transcribe(job: dict) -> dict:
    from .transcribe import Engine, _collect_segments, _segment_json, whisper_options

    path = Path(job["path"])
    engine = Engine(job["model"], job.get("device") or "auto")
    try:
        segments = _collect_segments(
            engine, path, job["language"], job.get("initial_prompt") or "",
            job.get("me_name") or "Faris", job.get("clip_end"))
        device, compute = engine.device, engine.compute_type
    finally:
        engine.close()
    return {
        "ok": True,
        "language": whisper_options(job["language"])["language"],
        "device": device,
        "compute_type": compute,
        "model": job["model"],
        "segments": [_segment_json(seg) for seg in segments],
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stderr)
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m vault_ingest.whisper_worker JOB.json", file=sys.stderr)
        return 2
    job = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    result_path = Path(job["result"])
    try:
        payload = _load(job) if job.get("mode") == "load" else _transcribe(job)
    except Exception as e:  # noqa: BLE001  the parent logs stderr and the exit code
        log.exception("Whisper worker failed")
        try:
            result_path.write_text(json.dumps({"ok": False, "error": str(e)}), encoding="utf-8")
        except OSError:
            pass
        return 1
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
