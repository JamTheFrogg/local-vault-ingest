import json
import re
import shutil
from pathlib import Path

import pytest

from vault_ingest.config import Settings, PROJECT_DIR
from vault_ingest.llm import FakeLLM
from vault_ingest.pipeline import Pipeline

TEMPLATE = """---
type: client
client: Acme
status: active
last_contact:
tags: [client]
---
# Acme

## Snapshot
Who they are, what they do, who you deal with, how they like to work.

## Current Status
Where things stand right now (rewritten after each call).

## Open Items
- [ ] item — owner — due

## Decisions Log
- YYYY-MM-DD — decision (link to call)

## Preferences & Watch-outs

## Work Log
- YYYY-MM-DD — what was done (link)
"""

ACME_PROFILE = TEMPLATE.replace("Who they are, what they do, who you deal with, how they like to work.",
                                "Acme d.o.o. — Sarajevo e-commerce shop. Contact: Amra (CEO).") \
    .replace("- [ ] item — owner — due", "- [ ] Send revised proposal — Fares — Friday")

SENTENCES = [
    "We looked at the Q4 launch plan and the numbers are tight.",
    "The budget for paid ads is currently 4,000 euros per month.",
    "I think we should move part of that into email campaigns instead.",
    "Our conversion rate on the landing page dropped to 1.8 percent last month.",
    "Let's schedule the new product photos for the second week of October.",
    "The developer said the checkout bug is fixed on staging.",
    "We need legal to approve the new privacy text before launch.",
    "Customers keep asking about delivery times to Croatia.",
]


def make_transcript(words_target: int = 2500, speakers=("Fares", "Amra")) -> str:
    lines, total, i, sec = [], 0, 0, 0
    while total < words_target:
        speaker = speakers[i % len(speakers)]
        text = " ".join(SENTENCES[(i + k) % len(SENTENCES)] for k in range(3))
        h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
        lines.append(f"[{h:02d}:{m:02d}:{s:02d}] {speaker}: {text}")
        total += len(text.split())
        sec += 37
        i += 1
    return "\n".join(lines)


def fake_responder(client_guess=("Acme", 0.93), fail_profile=False):
    def respond(system: str, user: str) -> str:
        if "Which client is this call with" in user:
            c, conf = client_guess
            return json.dumps({"client": c, "confidence": conf, "reason": "names match"})
        m = re.search(r"Chunk (\d+) of (\d+)", user)
        if m and "<chunk>" in user:
            return json.dumps({"context": f"In the call with Acme, part {m.group(1)} covers the Q4 launch budget.",
                               "points": ["Paid ads budget is 4,000 EUR/month", "Conversion dropped to 1.8%"]})
        if "verbatim quote" in system:
            return json.dumps({
                "decisions": [{"claim": "Shift 1,000 EUR/month from paid ads to email",
                               "quote": "The budget for paid ads is currently 4,000 euros per month."}],
                "action_items": [
                    {"claim": "Book product photographer",
                     "quote": "Let's schedule the new product photos for the second week of October."},
                    {"claim": "Draft email campaign",
                     "quote": "I think we should move part of that into email campaigns instead."},
                ],
                "numbers": [
                    {"claim": "4,000 EUR/month ads",
                     "quote": "The budget for paid ads is currently 4,000 euros per month."},
                    {"claim": "1.8% conversion",
                     "quote": "Our conversion rate on the landing page dropped to 1.8 percent last month."},
                ],
            })
        if "Write the call note" in user:
            return json.dumps({
                "title": "Q4 launch planning",
                "participants": ["Fares", "Amra (Acme CEO)"],
                "summary": "Fares and Amra reviewed the Q4 launch. They agreed to shift budget to email. Photos are scheduled.",
                "key_points": ["Paid ads at 4,000 EUR/month", "Conversion 1.8%"],
                "decisions": ["Shift 1,000 EUR/month from paid ads to email"],
                "action_items": [{"task": "Book product photographer", "owner": "Amra", "due": "Oct 10"},
                                 {"task": "Draft email campaign", "owner": "Fares", "due": None}],
                "open_questions": ["Delivery times to Croatia?"],
                "notable_details": ["4,000 EUR/month ads", "1.8% conversion"],
            })
        if "Currently open items" in user:
            if fail_profile:
                return "not json at all"
            return json.dumps({
                "current_status": "- Q4 launch in planning (last call 2026-09-30).\n- Budget shifting to email.",
                "resolved_open_items": ["Send revised proposal — Fares — Friday"],
                "new_open_items": [{"task": "Book product photographer", "owner": "Amra", "due": "Oct 10"},
                                   {"task": "Draft email campaign", "owner": "Fares"}],
                "snapshot_additions": [],
                "preferences_additions": ["Amra prefers decisions summarized in writing after calls"],
            })
        raise AssertionError(f"unexpected prompt: {user[:200]}")
    return respond


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "Vault"
    (v / "00_Inbox").mkdir(parents=True)
    (v / "_System" / "Templates").mkdir(parents=True)
    (v / "_System" / "Templates" / "Client.md").write_text(TEMPLATE, encoding="utf-8")
    acme = v / "Clients" / "Acme"
    for sub in ("Calls", "Transcripts", "Work"):
        (acme / sub).mkdir(parents=True)
    (acme / "Acme.md").write_text(ACME_PROFILE, encoding="utf-8")
    (v / "Personal").mkdir()
    (v / "Personal" / "diary.md").write_text("private", encoding="utf-8")
    return v


@pytest.fixture
def settings(vault: Path) -> Settings:
    return Settings(vault_path=vault, prompts_dir=PROJECT_DIR / "prompts", llm_provider="fake")


def make_pipeline(settings, responder=None, dry_run=False):
    llm = None if dry_run else FakeLLM(responder or fake_responder())
    return Pipeline(settings, llm, dry_run=dry_run, concurrency=2)
