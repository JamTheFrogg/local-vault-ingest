from pathlib import Path

import docx

from vault_ingest.parsers import is_filler, normalize_ts, parse_file


def test_normalize_ts():
    assert normalize_ts("1:02") == "00:01:02"
    assert normalize_ts("01:02:03.450") == "01:02:03"
    assert normalize_ts("00:00:05,000") == "00:00:05"


def test_filler_detection_keeps_short_answers():
    assert is_filler("Um, uh...")
    assert is_filler("mhm")
    assert not is_filler("Yes.")
    assert not is_filler("Da.")
    assert not is_filler("um, the budget is fine")


def test_txt_bracket_timestamps_and_merge(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("[00:00:01] Fares: Hello Amra.\n[00:00:04] Fares: Let's start.\n"
                 "[00:00:09] Amra: Um.\n[00:00:10] Amra: Sure, the budget is 4,000.\n", encoding="utf-8")
    turns = parse_file(f)
    assert [t.speaker for t in turns] == ["Fares", "Amra"]
    assert turns[0].text == "Hello Amra. Let's start."
    assert turns[0].start == "00:00:01"
    assert turns[1].text == "Sure, the budget is 4,000."          # filler-only "Um." dropped
    assert turns[0].end == "00:00:10"                              # filled from next turn


def test_name_then_timestamp_otter_style(tmp_path: Path):
    f = tmp_path / "otter.txt"
    f.write_text("Fares Rnja  0:03\nGood morning, let's review the launch.\n\n"
                 "Amra  0:15\nWe moved the photoshoot to October.\n", encoding="utf-8")
    turns = parse_file(f)
    assert [(t.speaker, t.start) for t in turns] == [("Fares Rnja", "00:00:03"), ("Amra", "00:00:15")]


def test_name_with_timestamp_in_parens(tmp_path: Path):
    f = tmp_path / "b.md"
    f.write_text("# Call transcript\n\nFares (00:01:02): Welcome everyone.\nAmra [00:01:10]: Thanks.\n",
                 encoding="utf-8")
    turns = parse_file(f)
    assert [(t.speaker, t.start, t.text) for t in turns] == [
        ("Fares", "00:01:02", "Welcome everyone."), ("Amra", "00:01:10", "Thanks.")]


def test_vtt_voice_tags(tmp_path: Path):
    f = tmp_path / "a.vtt"
    f.write_text("WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\n<v Fares>Hello there.</v>\n\n"
                 "2\n00:00:04.500 --> 00:00:08.000\n<v Amra>Hi! The launch is on track.</v>\n", encoding="utf-8")
    turns = parse_file(f)
    assert [(t.speaker, t.start, t.end) for t in turns] == [
        ("Fares", "00:00:01", "00:00:04"), ("Amra", "00:00:04", "00:00:08")]


def test_srt_with_speaker_prefix(tmp_path: Path):
    f = tmp_path / "a.srt"
    f.write_text("1\n00:00:01,000 --> 00:00:03,000\nFares: First line.\n\n"
                 "2\n00:00:03,000 --> 00:00:06,000\nFares: Second line.\n\n"
                 "3\n00:00:06,000 --> 00:00:09,000\nAmra: Reply.\n", encoding="utf-8")
    turns = parse_file(f)
    assert len(turns) == 2
    assert turns[0].text == "First line. Second line."
    assert turns[0].end == "00:00:06"


def test_docx(tmp_path: Path):
    d = docx.Document()
    d.add_paragraph("Fares: We need the new photos.")
    d.add_paragraph("Amra: I'll book the photographer.")
    f = tmp_path / "a.docx"
    d.save(f)
    turns = parse_file(f)
    assert [t.speaker for t in turns] == ["Fares", "Amra"]


def test_windows_1250_encoding(tmp_path: Path):
    f = tmp_path / "bs.txt"
    f.write_bytes("Amra: Šta je sa dostavom u Hrvatsku? Čekamo odgovor.\n".encode("cp1250"))
    turns = parse_file(f)
    assert turns[0].speaker == "Amra"
    assert "Šta" in turns[0].text and "Čekamo" in turns[0].text


def test_plain_text_without_speakers(tmp_path: Path):
    f = tmp_path / "plain.txt"
    f.write_text("this is a call about budgets.\nand another line about photos.\n", encoding="utf-8")
    turns = parse_file(f)
    assert len(turns) == 1 and turns[0].speaker is None
