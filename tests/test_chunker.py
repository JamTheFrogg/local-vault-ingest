from vault_ingest.chunker import chunk_turns
from vault_ingest.parsers import Turn


def turns_of(sizes, speakers=("A", "B")):
    return [Turn(speakers[i % 2], " ".join(f"w{i}_{k}" for k in range(n)), f"00:00:{i:02d}")
            for i, n in enumerate(sizes)]


def test_chunks_respect_bounds_and_turns():
    turns = turns_of([120] * 40)                      # 4800 words, 120-word turns
    chunks = chunk_turns(turns, 600, 900, 100)
    assert all(600 <= c.words <= 900 for c in chunks[:-1])
    # every original turn appears exactly once, in order (no mid-turn splits)
    flat = [t.text for c in chunks for t in c.turns]
    assert flat == [t.text for t in turns]


def test_overlap_is_carried_and_bounded():
    turns = turns_of([50] * 60)
    chunks = chunk_turns(turns, 600, 900, 100)
    assert not chunks[0].overlap
    for prev, cur in zip(chunks, chunks[1:]):
        assert cur.overlap, "later chunks carry overlap"
        assert sum(t.words for t in cur.overlap) <= 100
        assert cur.overlap[-1].text == prev.turns[-1].text


def test_overlap_tail_when_last_turn_is_long():
    turns = turns_of([700, 700, 700])
    chunks = chunk_turns(turns, 600, 900, 100)
    assert len(chunks) == 3
    tail = chunks[1].overlap[0]
    assert tail.text.startswith("… ")
    assert len(tail.text.split()) == 101            # ellipsis + 100 words


def test_oversized_turn_is_split_on_sentences():
    long_text = " ".join(f"Sentence number {i} is here." for i in range(500))   # 2500 words
    chunks = chunk_turns([Turn("A", long_text, "00:00:01")], 600, 900, 100)
    assert len(chunks) >= 3
    assert all(c.words <= 900 * 1.25 for c in chunks)
    assert chunks[0].start == "00:00:01"


def test_tiny_tail_merged_into_previous():
    turns = turns_of([300, 300, 300, 50])
    chunks = chunk_turns(turns, 600, 900, 100)
    assert len(chunks) == 2 or chunks[-1].words >= 200


def test_short_transcript_single_chunk():
    chunks = chunk_turns(turns_of([40, 40, 40]), 600, 900, 100)
    assert len(chunks) == 1 and chunks[0].words == 120
