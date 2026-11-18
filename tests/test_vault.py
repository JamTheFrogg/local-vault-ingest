from pathlib import Path

import pytest

from vault_ingest.vault import (UnsafePathError, Vault, append_to_section, get_frontmatter, get_section,
                                merge_open_items, replace_section, sanitize_name, set_frontmatter_field)

DOC = """---
type: client
client: Acme
last_contact:
---
# Acme

## Current Status
old status

### detail kept inside section
more

## Open Items
- [ ] Send revised proposal — Fares — Friday
- [x] Old done thing

## Work Log
- 2026-01-01 — first
"""


def test_replace_section_keeps_neighbours():
    out = replace_section(DOC, "Current Status", "new status")
    assert get_section(out, "Current Status") == "new status"
    assert "### detail kept" not in out            # H3 belonged to the replaced section
    assert "## Open Items" in out and "- [ ] Send revised proposal" in out


def test_replace_missing_section_appends():
    out = replace_section(DOC, "Brand New", "hello")
    assert out.rstrip().endswith("## Brand New\nhello")


def test_append_to_section():
    out = append_to_section(DOC, "Work Log", ["- 2026-02-02 — second"])
    assert get_section(out, "Work Log") == "- 2026-01-01 — first\n- 2026-02-02 — second"


def test_append_drops_template_placeholders():
    doc = "## Decisions Log\n- YYYY-MM-DD — decision (link to call)\n"
    out = append_to_section(doc, "Decisions Log", ["- 2026-09-30 — real decision"])
    assert "YYYY-MM-DD" not in out and "real decision" in out


def test_merge_open_items_resolves_dedupes_never_removes():
    body = get_section(DOC, "Open Items")
    out = merge_open_items(body, ["Send revised proposal — Fares — Friday"],
                           ["Book photographer — Amra — Oct 10", "Book photographer — Amra — Oct 10"],
                           "2026-09-30")
    lines = out.split("\n")
    assert lines[0] == "- [x] Send revised proposal — Fares — Friday ✅ 2026-09-30"
    assert "- [x] Old done thing" in lines
    assert lines.count("- [ ] Book photographer — Amra — Oct 10") == 1
    assert len(lines) == 3


def test_merge_open_items_fuzzy_match():
    out = merge_open_items("- [ ] Send revised proposal — Fares — Friday (from [[Clients/Acme/Calls/x|2026-09-01]])",
                           ["Send revised proposal — Fares — Friday"], [], "2026-09-30")
    assert out.startswith("- [x] ")


def test_frontmatter_field_set_and_insert():
    out = set_frontmatter_field(DOC, "last_contact", "2026-09-30")
    assert get_frontmatter(out)["last_contact"].isoformat() == "2026-09-30"
    out2 = set_frontmatter_field(DOC, "status", "active")
    assert get_frontmatter(out2)["status"] == "active"
    assert out2.split("---")[2] == DOC.split("---")[2]   # body untouched


def test_sanitize_name():
    assert sanitize_name('Acme: "Q4" plan/launch?') == "Acme Q4 plan launch"
    assert sanitize_name("  ...  ") == "Untitled"


def test_vault_refuses_writes_outside_allowed(tmp_path: Path):
    (tmp_path / "Clients").mkdir()
    v = Vault(tmp_path, ("Clients",))
    v.write(tmp_path / "Clients" / "ok.md", "x")
    with pytest.raises(UnsafePathError):
        v.write(tmp_path / "Personal" / "nope.md", "x")
    with pytest.raises(UnsafePathError):
        v.write(tmp_path / "Clients" / ".." / "escape.md", "x")


def test_move_never_overwrites(tmp_path: Path):
    (tmp_path / "Clients").mkdir()
    v = Vault(tmp_path, ("Clients",))
    a = v.write(tmp_path / "Clients" / "a.txt", "A")
    (tmp_path / "Clients" / "b.txt").write_text("B")
    final = v.move(a, tmp_path / "Clients" / "b.txt")
    assert final.name == "b (2).txt"
    assert (tmp_path / "Clients" / "b.txt").read_text() == "B"
