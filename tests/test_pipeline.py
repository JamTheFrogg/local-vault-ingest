import json
from pathlib import Path

import pytest

from conftest import fake_responder, make_pipeline, make_transcript

from vault_ingest.vault import get_frontmatter, get_section


def drop(vault: Path, name: str, words: int = 2500) -> Path:
    f = vault / "00_Inbox" / name
    f.write_text(make_transcript(words), encoding="utf-8")
    return f


def snapshot(vault: Path) -> dict:
    return {p.relative_to(vault).as_posix(): p.read_bytes() for p in vault.rglob("*") if p.is_file()}


def test_end_to_end_filename_convention(vault, settings):
    src = drop(vault, "Acme - 2026-09-30 - Kickoff.txt", 2500)
    p = make_pipeline(settings)
    [r] = p.run_inbox()
    assert r.status == "processed", r.message

    call = vault / "Clients/Acme/Calls/2026-09-30 Kickoff.md"
    assert call.exists()
    fm = get_frontmatter(call.read_text(encoding="utf-8"))
    assert fm["type"] == "call" and fm["client"] == "Acme" and fm["chunks"] == len(r.chunk_notes) >= 3

    chunk_dir = vault / "Clients/Acme/Transcripts/2026-09-30 Kickoff"
    parts = sorted(chunk_dir.glob("*.md"))
    assert len(parts) == len(r.chunk_notes)
    first = parts[0].read_text(encoding="utf-8")
    cfm = get_frontmatter(first)
    assert cfm["type"] == "transcript-chunk" and cfm["chunk"] == 1 and cfm["of"] == len(parts)
    assert cfm["call"] == "[[Clients/Acme/Calls/2026-09-30 Kickoff]]"
    assert cfm["speakers"] == ["Fares", "Amra"] and cfm["time_start"] == "00:00:00"
    assert "> Context: In the call with Acme" in first
    assert "Part 2]]" in first                                           # nav link to next part
    call_text = call.read_text(encoding="utf-8")
    for part in parts:                                                   # call note links every part
        assert part.stem in call_text

    profile = (vault / "Clients/Acme/Acme.md").read_text(encoding="utf-8")
    assert "Budget shifting to email" in get_section(profile, "Current Status")
    oi = get_section(profile, "Open Items")
    assert "- [x] Send revised proposal — Fares — Friday ✅" in oi
    assert "- [ ] Book product photographer — Amra — Oct 10 (from [[Clients/Acme/Calls/2026-09-30 Kickoff|2026-09-30]])" in oi
    assert "- [ ] Draft email campaign — Fares" in oi
    dec = get_section(profile, "Decisions Log")
    assert "2026-09-30 — Shift 1,000 EUR/month" in dec and "YYYY-MM-DD" not in dec
    assert "2026-09-30 — Call:" in get_section(profile, "Work Log")
    assert "prefers decisions summarized" in get_section(profile, "Preferences & Watch-outs")
    assert "Acme d.o.o." in get_section(profile, "Snapshot")               # untouched
    assert str(get_frontmatter(profile)["last_contact"]) == "2026-09-30"

    assert not src.exists()
    assert (vault / "Clients/Acme/Transcripts/raw/Acme - 2026-09-30 - Kickoff.txt").exists()
    assert list((vault / "_System/backups").glob("Acme *.md"))
    reg = json.loads((vault / "_System/ingest-log.json").read_text(encoding="utf-8"))
    [rec] = reg.values()
    assert rec["client"] == "Acme" and rec["chunks"] == len(parts)
    assert (vault / "Personal/diary.md").read_text() == "private"


def test_duplicate_file_goes_to_review(vault, settings):
    drop(vault, "Acme - 2026-09-30 - Kickoff.txt")
    make_pipeline(settings).run_inbox()
    raw = vault / "Clients/Acme/Transcripts/raw/Acme - 2026-09-30 - Kickoff.txt"
    (vault / "00_Inbox/copy.txt").write_bytes(raw.read_bytes())
    [r] = make_pipeline(settings).run_inbox()
    assert r.status == "skipped"
    assert (vault / "00_Inbox/_needs-review/copy.txt").exists()
    assert (vault / "00_Inbox/_needs-review/copy.txt.why.md").exists()
    assert len(list((vault / "Clients/Acme/Calls").glob("*.md"))) == 1


def test_reprocess_does_not_duplicate_profile_lines(vault, settings):
    drop(vault, "Acme - 2026-09-30 - Kickoff.txt")
    make_pipeline(settings).run_inbox()
    profile_before = (vault / "Clients/Acme/Acme.md").read_text(encoding="utf-8")
    r = make_pipeline(settings).reprocess(Path("Clients/Acme/Transcripts/raw/Acme - 2026-09-30 - Kickoff.txt"))
    assert r.status == "processed"
    profile = (vault / "Clients/Acme/Acme.md").read_text(encoding="utf-8")
    assert profile.count("2026-09-30 — Call:") == 1
    assert profile.count("Shift 1,000 EUR/month") == 1
    assert profile.count("Book product photographer") == 1
    assert len(list((vault / "Clients/Acme/Calls").glob("*.md"))) == 1
    assert profile_before.count("Book product photographer") == 1


def test_client_identified_by_llm(vault, settings):
    drop(vault, "zoom_recording_2026-09-28.txt")
    [r] = make_pipeline(settings).run_inbox()
    assert r.status == "processed" and r.client == "Acme"
    # no title in the file name -> LLM title used
    assert (vault / "Clients/Acme/Calls/2026-09-28 Q4 launch planning.md").exists()


def test_low_confidence_goes_to_review_and_writes_nothing_else(vault, settings):
    drop(vault, "meeting.txt")
    before = {k: v for k, v in snapshot(vault).items() if not k.startswith("00_Inbox")}
    [r] = make_pipeline(settings, fake_responder(client_guess=("Acme", 0.4))).run_inbox()
    assert r.status == "review"
    assert (vault / "00_Inbox/_needs-review/meeting.txt").exists()
    after = {k: v for k, v in snapshot(vault).items() if not k.startswith("00_Inbox")}
    assert before == after


def test_unknown_client_name_from_llm_goes_to_review(vault, settings):
    drop(vault, "meeting.txt")
    [r] = make_pipeline(settings, fake_responder(client_guess=("Globex", 0.99))).run_inbox()
    assert r.status == "review"
    assert not (vault / "Clients/Globex").exists()


def test_new_client_created_from_explicit_filename(vault, settings):
    drop(vault, "Globex - 2026-09-29 - Intro call.txt", 800)
    [r] = make_pipeline(settings).run_inbox()
    assert r.status == "processed" and r.client == "Globex"
    profile = (vault / "Clients/Globex/Globex.md").read_text(encoding="utf-8")
    assert get_frontmatter(profile)["client"] == "Globex"
    assert profile.splitlines()[profile.splitlines().index("---", 1) + 1] == "# Globex"
    for sub in ("Calls", "Transcripts", "Work"):
        assert (vault / "Clients/Globex" / sub).is_dir()


def test_dry_run_writes_nothing(vault, settings):
    drop(vault, "Acme - 2026-09-30 - Kickoff.txt")
    drop(vault, "unnamed.txt", 80)
    before = snapshot(vault)
    results = make_pipeline(settings, dry_run=True).run_inbox()
    assert snapshot(vault) == before
    statuses = sorted(r.status for r in results)
    assert statuses == ["planned", "review"]
    planned = next(r for r in results if r.status == "planned")
    assert any("chunks:" in line for line in planned.planned)


def test_profile_failure_keeps_the_call_note_and_retries_only_that_step(vault, settings):
    drop(vault, "Acme - 2026-09-30 - Kickoff.txt")
    p = make_pipeline(settings, fake_responder(fail_profile=True))
    [r] = p.run_inbox()
    assert r.status == "processed", r.message
    assert r.call_note is not None and r.call_note.is_file()
    assert r.chunk_notes and all(c.is_file() for c in r.chunk_notes)
    assert not (vault / "00_Inbox" / "Acme - 2026-09-30 - Kickoff.txt").exists()
    assert not (vault / "00_Inbox/_needs-review").exists()
    pending = json.loads((vault / "_System/pending-jobs.json").read_text(encoding="utf-8"))
    job = next(iter(pending.values()))
    assert job["steps"] == ["profile"]
    assert "profile update pending" in (vault / "_System/digest.md").read_text(encoding="utf-8")
    assert sum("Currently open items" in u for _, u in p.llm.calls) == 3

    p2 = make_pipeline(settings)
    assert p2.resume_pending() == 1
    assert not (vault / "_System/pending-jobs.json").exists()
    assert sum("Currently open items" in u for _, u in p2.llm.calls) == 1
    assert not any("Write the call note" in u or "<chunk>" in u for _, u in p2.llm.calls)


def test_empty_file_goes_to_review(vault, settings):
    (vault / "00_Inbox/Acme - 2026-09-30 - Empty.txt").write_text("hi", encoding="utf-8")
    [r] = make_pipeline(settings).run_inbox()
    assert r.status == "review"


def test_long_transcript_uses_chunk_digests_for_call_note(vault, settings):
    drop(vault, "Acme - 2026-09-30 - Workshop.txt", 9000)
    p = make_pipeline(settings)
    [r] = p.run_inbox()
    assert r.status == "processed"
    call_prompt = next(u for _, u in p.llm.calls if "Write the call note" in u)
    assert "per-chunk digests" in call_prompt
    assert len(call_prompt.split()) < 4000


def test_profile_archives_old_log_entries(vault, settings):
    settings.profile_max_words = 150
    settings.log_keep_entries = 3
    prof = vault / "Clients/Acme/Acme.md"
    text = prof.read_text(encoding="utf-8").replace(
        "- YYYY-MM-DD — what was done (link)",
        "\n".join(f"- 2026-0{m}-01 — did a long piece of work number {m} for the client" for m in range(1, 9)))
    prof.write_text(text, encoding="utf-8")
    drop(vault, "Acme - 2026-09-30 - Kickoff.txt")
    make_pipeline(settings).run_inbox()
    profile = prof.read_text(encoding="utf-8")
    assert len([l for l in get_section(profile, "Work Log").split("\n") if l.startswith("- ")]) == 3
    archive = (vault / "Clients/Acme/Acme - Archive.md").read_text(encoding="utf-8")
    assert "work number 1 " in archive and "work number 6 " in archive


def test_clean_title():
    from vault_ingest.pipeline import clean_title
    assert clean_title("zoom_recording_ ") is None
    assert clean_title("GMT 14.05.33 Recording") is None
    assert clean_title("Kickoff") == "Kickoff"
    assert clean_title("pricing_workshop") == "pricing workshop"


def test_call_and_profile_prompts_say_who_is_who(vault, settings):
    (vault / "Me.md").write_text("---\ntype: me\n---\n# Me\n\nFaris builds software for clients in Sarajevo.\n",
                                 encoding="utf-8")
    drop(vault, "Acme - 2026-09-30 - Kickoff.txt", 80)
    p = make_pipeline(settings)
    [r] = p.run_inbox()
    assert r.status == "processed", r.message
    call = next(u for _, u in p.llm.calls if "Write the call note" in u)
    profile = next(u for _, u in p.llm.calls if "Currently open items" in u)
    for prompt in (call, profile):
        assert "Faris is the consultant/developer; the client is the other side." in prompt
        assert "Faris builds software" in prompt
        assert "Acme d.o.o." in prompt
    digest = next(u for _, u in p.llm.calls if "<chunk>" in u)
    assert "Faris builds software" not in digest
    assert "human agent" in digest
    note = r.call_note.read_text(encoding="utf-8")
    assert "> [!quote]- Evidence" in note
    assert "The budget for paid ads is currently 4,000 euros per month." in note


def test_unverified_claims_stay_except_numbers():
    from vault_ingest.pipeline import apply_evidence
    from vault_ingest.schemas import CallNote, EvidenceCheck

    note = CallNote(
        summary="A short call.",
        decisions=["Ship the bridge"],
        action_items=[{"task": "Send the link", "owner": "Samir"}],
        notable_details=["Deposit of 50000 required"],
        open_questions=["Which broker?"],
    )
    check = EvidenceCheck.model_validate({
        "decisions": [{"claim": "Ship the bridge", "quote": "this sentence was never spoken"}],
        "action_items": [{"claim": "Send the link", "quote": "I will send you the link today"}],
        "numbers": [{"claim": "Deposit of 50000 required", "quote": "say I have 50000 saved"}],
    })
    cleaned, quotes = apply_evidence(note, check, "I will send you the link today and then we stop.")
    assert cleaned.decisions == ["Ship the bridge (unverified)"]
    assert [a.task for a in cleaned.action_items] == ["Send the link"]
    assert cleaned.notable_details == []
    assert cleaned.open_questions == ["Which broker?"]
    assert quotes.actions[0][1] == "I will send you the link today"


def test_quote_match_folds_diacritics_and_glossary_spellings():
    from vault_ingest.pipeline import quote_in_text

    glossary = "- Infinox\n- MetaTrader\n"
    transcript = "IndieNoxa kaze da radi na mostu, pa onda Infinoks."
    assert quote_in_text("Infinox kaže da radi na mostu", transcript, glossary)
    assert quote_in_text("Infinoxa", transcript, glossary)
    assert quote_in_text("Gold holds the line here today", transcript, glossary) is False


def test_glossary_entity_is_not_marked_unverified():
    from vault_ingest.pipeline import apply_evidence
    from vault_ingest.schemas import CallNote, EvidenceCheck

    glossary = "- Infinox\n"
    note = CallNote(
        summary="A short call.",
        decisions=["Infinox"],
        action_items=[{"task": "Ask Infinox", "owner": "Samir"}],
        notable_details=["Broker: Infinox", "Deposit of 50000 required"],
    )
    check = EvidenceCheck.model_validate({
        "decisions": [{"claim": "Infinox", "quote": "never spoken"}],
        "action_items": [{"claim": "Ask Infinox", "quote": "never spoken"}],
        "numbers": [{"claim": "Broker: Infinox", "quote": "never spoken"},
                    {"claim": "Deposit of 50000 required", "quote": "never spoken"}],
    })
    cleaned, _quotes = apply_evidence(note, check, "nista od toga.", glossary)
    assert cleaned.decisions == ["Infinox"]
    assert [a.task for a in cleaned.action_items] == ["Ask Infinox (unverified)"]
    assert cleaned.notable_details == ["Broker: Infinox"]
    assert cleaned.open_questions == []


def test_retract_lines_leaves_the_snapshot_section():
    from vault_ingest.vault import retract_lines

    text = """# Client

## Snapshot
- Broker partner: Infinox (from the call)
- Hand edit: Samir prefers morning calls

## Decisions Log
- 2026-10-01 — US citizens will be rejected (from the call)

## Preferences & Watch-outs
- Preference for human agents (from the call)
"""
    out = retract_lines(text, [
        "- Broker partner: Infinox (from the call)",
        "- 2026-10-01 — US citizens will be rejected (from the call)",
        "- Preference for human agents (from the call)",
    ])
    assert "Broker partner: Infinox" in out
    assert "Hand edit: Samir prefers morning calls" in out
    assert "US citizens will be rejected" not in out
    assert "human agents" not in out


def test_reprocess_from_audio_replaces_the_transcript(vault, settings, monkeypatch):
    drop(vault, "Acme - 2026-09-30 - Kickoff.txt")
    p = make_pipeline(settings)
    p.run_inbox()
    raw = vault / "Clients/Acme/Transcripts/raw/Acme - 2026-09-30 - Kickoff.txt"
    before = raw.read_text(encoding="utf-8")
    reg_path = vault / "_System/ingest-log.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    digest, rec = next(iter(reg.items()))
    audio = vault / "_System/audio/call.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")
    rec["audio_source"] = str(audio)
    reg_path.write_text(json.dumps({digest: rec}), encoding="utf-8")
    fresh = ("[00:00:00] Fresh transcript. The budget for paid ads is currently 4,000 euros per month. "
             "I think we should move part of that into email campaigns instead. "
             "Let's schedule the new product photos for the second week of October. "
             "Our conversion rate on the landing page dropped to 1.8 percent last month. "
             + ("word " * 40) + "\n")
    p.registry.data[digest]["audio_source"] = str(audio)
    reg_path.write_text(json.dumps(p.registry.data), encoding="utf-8")
    monkeypatch.setattr(p, "_transcribe", lambda path: (fresh, "faster-whisper/large-v3/hr"))

    r = p.reprocess(raw, from_audio=True)
    assert r.status == "processed", r.message
    assert raw.read_text(encoding="utf-8") != before
    assert "Fresh transcript" in raw.read_text(encoding="utf-8")
    new_reg = json.loads(reg_path.read_text(encoding="utf-8"))
    assert digest not in new_reg
    assert any(str(v.get("audio_source", "")).endswith("call.mp3") for v in new_reg.values())
    profile = (vault / "Clients/Acme/Acme.md").read_text(encoding="utf-8")
    assert profile.count("Shift 1,000 EUR/month") == 1
    assert "Acme d.o.o." in profile


def test_reprocess_rebuilds_status_from_call_notes(vault, settings):
    from vault_ingest.pipeline import Identity
    from vault_ingest.schemas import CallNote

    (vault / "Clients/Acme/Calls/2026-08-01 Older.md").write_text(
        "---\ntype: call\ndate: 2026-08-01\n---\n# Older\n\nThe August fact about the shop.\n",
        encoding="utf-8")
    current = vault / "Clients/Acme/Calls/2026-09-30 Kickoff.md"
    current.write_text(
        "---\ntype: call\ndate: 2026-09-30\n---\n# Old kickoff\n\nStale deposit requirement of 20000.\n",
        encoding="utf-8")
    profile = vault / "Clients/Acme/Acme.md"
    profile.write_text(profile.read_text(encoding="utf-8").replace(
        "Where things stand right now (rewritten after each call).",
        "OLD STATUS requires a 20000 deposit that the new note does not."), encoding="utf-8")
    p = make_pipeline(settings)
    note = CallNote(summary="A fresh summary about the bridge.", decisions=[], action_items=[],
                    notable_details=[])
    p.plan_profile_update(Identity("Acme", "2026-09-30", "Kickoff"), "2026-09-30 Kickoff", note,
                          reprocess=True, replacing=current)
    user = next(u for _, u in p.llm.calls if "Currently open items" in u)
    assert "OLD STATUS requires a 20000 deposit" not in user
    assert "Stale deposit requirement" not in user
    assert "The August fact about the shop." in user
    assert "A fresh summary about the bridge." in user
    assert user.index("The August fact about the shop.") < user.index("A fresh summary about the bridge.")
    assert "Write it only from the call notes" in user
    assert "Currently open items" in user


def test_reprocess_from_audio_stops_when_nothing_matches(vault, settings, monkeypatch):
    drop(vault, "Acme - 2026-09-30 - Kickoff.txt", 80)
    p = make_pipeline(settings)
    p.run_inbox()
    raw = vault / "Clients/Acme/Transcripts/raw/Acme - 2026-09-30 - Kickoff.txt"
    before = raw.read_bytes()
    monkeypatch.setattr(p, "_recordings_folder", lambda: vault / "no-recordings")
    with pytest.raises(SystemExit, match="Nothing was changed"):
        p.reprocess(raw, from_audio=True)
    assert raw.read_bytes() == before


# ---------------------------------------------------------------- long calls and the evidence check
def test_evidence_windows_split_long_transcripts_and_long_single_lines():
    from vault_ingest.pipeline import evidence_windows
    text = "\n\n".join(f"[00:{i:02d}:00] " + " ".join(f"w{i}_{k}" for k in range(300)) for i in range(10))
    windows = evidence_windows(text, 1000)
    assert len(windows) == 4 and all(len(w.split()) <= 1000 for w in windows)
    assert " ".join(windows).split() == text.split()            # nothing lost, order kept
    one_line = " ".join(f"w{k}" for k in range(3500))            # no speaker labels: one huge line
    cut = evidence_windows(one_line, 1000)
    assert [len(w.split()) for w in cut] == [1000, 1000, 1000, 500]
    assert evidence_windows("short call", 1000) == ["short call"]


def test_merge_evidence_keeps_only_real_quotes():
    from vault_ingest.pipeline import merge_evidence
    from vault_ingest.schemas import ClaimQuote, EvidenceCheck
    a = EvidenceCheck(decisions=[ClaimQuote(claim="A", quote="")], numbers=[ClaimQuote(claim="N", quote="ten")])
    b = EvidenceCheck(decisions=[ClaimQuote(claim="A", quote="we agreed on A")])
    merged = merge_evidence(merge_evidence(EvidenceCheck(), a), b)
    assert [(c.claim, c.quote) for c in merged.decisions] == [("A", "we agreed on A")]
    assert [(c.claim, c.quote) for c in merged.numbers] == [("N", "ten")]


def test_long_call_checks_evidence_in_windows(vault, settings, monkeypatch):
    monkeypatch.setattr("vault_ingest.pipeline.EVIDENCE_WINDOW_WORDS", 800)
    drop(vault, "Acme - 2026-09-30 - Workshop.txt", 2500)
    p = make_pipeline(settings)
    [r] = p.run_inbox()
    assert r.status == "processed", r.message
    evidence_prompts = [u for sy, u in p.llm.calls if "verbatim quote" in sy]
    assert len(evidence_prompts) >= 3
    assert all(len(u.split()) < 1100 for u in evidence_prompts)   # each request stays small
    note = (vault / "Clients/Acme/Calls/2026-09-30 Workshop.md").read_text(encoding="utf-8")
    assert "(unverified)" not in note                               # quotes from any window count


def test_evidence_claims_are_sent_in_batches_of_eight():
    from vault_ingest.llm import MAX_TOKENS
    from vault_ingest.pipeline import evidence_groups
    groups = evidence_groups([f"d{i}" for i in range(9)], ["a"], [])
    assert [len(g[0]) + len(g[1]) + len(g[2]) for g in groups] == [8, 2]
    assert MAX_TOKENS["EvidenceCheck"] == 1500


def test_evidence_check_logs_quotes_per_batch(vault, settings, caplog):
    from vault_ingest.parsers import parse_transcript_text
    from vault_ingest.pipeline import Identity
    from vault_ingest.schemas import CallNote
    note = CallNote(summary="A short note.", decisions=[f"decision {i}" for i in range(9)])
    turns = parse_transcript_text("[00:00:00] The budget for paid ads is currently 4,000 euros per month.")
    caplog.set_level("INFO", logger="vault_ingest")
    p = make_pipeline(settings)
    p.check_evidence(Identity("Acme", "2026-09-30", "Kickoff"), note, turns)
    prompts = [u for system, u in p.llm.calls if "verbatim quote" in system]
    assert len(prompts) == 2
    assert "decision 0" in prompts[0] and "decision 7" in prompts[0] and "decision 8" not in prompts[0]
    assert "decision 8" in prompts[1]
    assert "evidence check 1/2: " in caplog.text and "quotes verified" in caplog.text


def test_phone_recording_uses_the_picker_and_the_creation_time(vault, settings, monkeypatch):
    import datetime as dt
    stamp = dt.datetime(2026, 10, 5, 15, 7, tzinfo=dt.timezone.utc).timestamp()
    monkeypatch.setattr("vault_ingest.pipeline.created_ts", lambda st: stamp)
    drop(vault, "Voice 001.txt", 80)
    p = make_pipeline(settings)
    p.phone_client = lambda path: ("Acme", "Kickoff")
    [r] = p.run_inbox()
    assert r.status == "processed", r.message
    text = r.call_note.read_text(encoding="utf-8")
    local = dt.datetime.fromtimestamp(stamp).astimezone()
    assert f"{local.date().isoformat()} {local.strftime('%H:%M')}" in text
    assert not any("Which client is this call with" in u for _, u in p.llm.calls)


def test_phone_recording_stays_queued_until_a_client_is_chosen(vault, settings):
    drop(vault, "Voice 001.txt", 80)
    p = make_pipeline(settings)
    p.phone_client = lambda path: None
    [r] = p.run_inbox()
    assert r.status == "waiting"
    assert (vault / "00_Inbox" / "Voice 001.txt").is_file()
    assert not any("Which client is this call with" in u for _, u in p.llm.calls)


def test_duplicate_hash_in_the_transcript_cache_skips_whisper(vault, settings, monkeypatch):
    from vault_ingest.transcribe import Transcript
    from vault_ingest.vault import sha256_file
    calls = []

    def fake(path, **kwargs):
        calls.append(path.name)
        return Transcript(make_transcript(80), kwargs["language"], "large-v3", "cpu", "int8", 1.0)

    monkeypatch.setattr("vault_ingest.pipeline.transcribe_path", fake)
    src = vault / "00_Inbox" / "Acme - 2026-09-30 - Kickoff.m4a"
    src.write_bytes(b"same-audio")
    [r] = make_pipeline(settings).run_inbox()
    assert r.status == "processed", r.message
    (vault / "_System/ingest-log.json").write_text("{}\n", encoding="utf-8")
    copy = vault / "00_Inbox" / "Voice 002.m4a"
    copy.write_bytes(b"same-audio")
    assert sha256_file(copy) == sha256_file(vault / "_System/audio" / "Acme - 2026-09-30 - Kickoff.m4a")
    p2 = make_pipeline(settings)
    p2.phone_client = lambda path: (_ for _ in ()).throw(AssertionError("picker must not open"))
    [r2] = p2.run_inbox()
    assert r2.status == "skipped"
    assert calls == ["Acme - 2026-09-30 - Kickoff.m4a"]
    assert (vault / "00_Inbox/_needs-review/Voice 002.m4a").is_file()


def test_status_prints_watcher_inbox_and_the_current_step(vault, settings):
    from vault_ingest import cli
    (vault / "00_Inbox" / "a.txt").write_text("x", encoding="utf-8")
    review = vault / "00_Inbox" / "_needs-review"
    review.mkdir()
    (review / "b.txt").write_text("y", encoding="utf-8")
    (vault / "_System" / "ingest.pid").write_text("424242\n", encoding="utf-8")
    (vault / "_System" / "companion.lock").write_text("7\n", encoding="utf-8")
    (vault / "_System" / "ingest-status.json").write_text(
        json.dumps({"file": "a.txt", "step": "summary", "last_error": "boom"}), encoding="utf-8")
    lines = cli.format_ingest_status(settings, pid_alive=lambda pid: pid == 7, mcp_ok=True, lm_ok=False)
    text = "\n".join(lines)
    assert "watcher: stopped" in text
    assert "companion: running (pid 7)" in text
    assert "MCP: up" in text and "LM Studio: down" in text
    assert "inbox: a.txt" in text and "_needs-review: b.txt" in text
    assert "current: a.txt (summary)" in text and "last error: boom" in text


def test_second_ingest_lock_exits_with_the_pid(tmp_path):
    import os
    from vault_ingest.cli import IngestLock
    system = tmp_path / "_System"
    first = IngestLock(system)
    try:
        assert first.acquire() is None
        second = IngestLock(system)
        message = second.acquire()
        assert message == f"vault-ingest is already running (pid {os.getpid()})."
    finally:
        first.release()


def test_failed_evidence_check_does_not_lose_the_call(vault, settings):
    base = fake_responder()

    def respond(system, user):
        if "verbatim quote" in system:
            return ""                                               # what a context overflow looks like
        return base(system, user)

    drop(vault, "Acme - 2026-09-30 - Kickoff.txt")
    p = make_pipeline(settings, respond)
    [r] = p.run_inbox()
    assert r.status == "processed", r.message
    note = (vault / "Clients/Acme/Calls/2026-09-30 Kickoff.md").read_text(encoding="utf-8")
    assert "Shift 1,000 EUR/month" in note and "Book product photographer" in note
    assert "4,000 EUR/month ads" in note                           # details are kept, just not verified
    assert not (vault / "00_Inbox/_needs-review").exists() or not list((vault / "00_Inbox/_needs-review").iterdir())


def test_pending_step_backs_off_then_needs_review(vault, settings, monkeypatch):
    from vault_ingest.pipeline import PENDING_BACKOFF_SECONDS

    drop(vault, "Acme - 2026-09-30 - Kickoff.txt")
    make_pipeline(settings, fake_responder(fail_profile=True)).run_inbox()
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr("vault_ingest.pipeline.time.time", lambda: clock["now"])

    def bad(system, user):
        return "not json"

    p = make_pipeline(settings, bad)
    delays = []
    for _ in range(4):
        assert p.resume_pending() == 0
        job = next(iter(json.loads((vault / "_System/pending-jobs.json").read_text(encoding="utf-8")).values()))
        delays.append(job["retry_after"] - clock["now"])
        assert job.get("needs_review") is not True
        clock["now"] = job["retry_after"]
    assert delays == list(PENDING_BACKOFF_SECONDS)
    assert p.resume_pending() == 0
    pending = json.loads((vault / "_System/pending-jobs.json").read_text(encoding="utf-8"))
    digest, job = next(iter(pending.items()))
    assert job["needs_review"] is True
    assert job["failure_counts"]["profile"] == 5
    note = vault / "00_Inbox" / "_needs-review" / f"pending-{digest[:8]}-profile.md"
    text = note.read_text(encoding="utf-8")
    assert "profile" in text and "Call:" in text
    assert (vault / "Clients/Acme/Calls/2026-09-30 Kickoff.md").is_file()
    before = len(p.llm.calls)
    clock["now"] += 10**7
    assert p.resume_pending() == 0
    assert len(p.llm.calls) == before
    assert p.reset_pending(key=digest[:12]) == [digest]
    assert p.resume_pending() == 0
    assert len(p.llm.calls) > before


def test_pending_retry_does_not_load_while_a_transcription_is_running(vault, settings):
    drop(vault, "Acme - 2026-09-30 - Kickoff.txt")
    p = make_pipeline(settings, fake_responder(fail_profile=True))
    p.run_inbox()
    loads = []
    p.models = type("M", (), {"ensure_loaded": lambda self: loads.append("load"),
                              "unload_chat": lambda self: None})()
    p.gpu = type("G", (), {"start_blocked": lambda self: "transcription"})()
    assert p.resume_pending() == 0
    assert loads == []


def test_profile_prompt_trims_old_sections_and_keeps_the_call(vault, settings, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="vault_ingest")
    settings.local_context = 800
    prof = vault / "Clients/Acme/Acme.md"
    extra = "\n".join(
        f"- 2020-01-{i:02d} — ancient work item number {i} that should be trimmed first" for i in range(1, 28))
    prof.write_text(prof.read_text(encoding="utf-8").replace(
        "- YYYY-MM-DD — what was done (link)", extra), encoding="utf-8")
    drop(vault, "Acme - 2026-09-30 - Kickoff.txt")
    p = make_pipeline(settings)
    [r] = p.run_inbox()
    assert r.status == "processed", r.message
    prompt = next(user for _, user in p.llm.calls if "Currently open items" in user)
    assert "ancient work item number 1 " not in prompt
    assert "2026-09-30" in prompt
    assert "Trimmed the existing profile for Acme" in caplog.text


def test_profile_trim_drops_the_profile_copy_and_keeps_the_open_item_list(vault, settings):
    settings.local_context = 4000
    prof = vault / "Clients/Acme/Acme.md"
    text = prof.read_text(encoding="utf-8")
    text = text.replace("- [ ] Send revised proposal — Fares — Friday",
                        "- [ ] Send revised proposal — Fares — Friday\n- [ ] unique-open-item-keep", 1)
    prof.write_text(text, encoding="utf-8")
    drop(vault, "Acme - 2026-09-30 - Kickoff.txt")
    p = make_pipeline(settings)
    [r] = p.run_inbox()
    assert r.status == "processed", r.message
    prompt = next(user for _, user in p.llm.calls if "Currently open items" in user)
    profile_block = prompt.split("<profile>", 1)[1].split("</profile>", 1)[0]
    open_block = prompt.split("<open_items>", 1)[1].split("</open_items>", 1)[0]
    assert "unique-open-item-keep" not in profile_block
    assert "unique-open-item-keep" in open_block
    assert "2026-09-30" in prompt.split("<call_note>", 1)[1]
