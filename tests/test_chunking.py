from collector.chunking import chunk_text, content_hash


def test_chunk_text_empty_string_returns_empty_list():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_chunk_text_short_text_single_chunk():
    text = " ".join(f"word{i}" for i in range(50))
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_chunk_text_long_paragraphs_split_with_overlap():
    para1 = " ".join(f"alpha{i}" for i in range(600))
    para2 = " ".join(f"beta{i}" for i in range(600))
    text = para1 + "\n\n" + para2
    chunks = chunk_text(text, target_tokens=(500, 900), overlap_tokens=(75, 150))
    assert len(chunks) >= 2
    assert "alpha0" in chunks[0]
    assert "beta599" in chunks[-1]
    overlap_words = set(chunks[0].split()[-150:])
    assert overlap_words & set(chunks[1].split()[:150])


def test_chunk_text_single_unbroken_line_does_not_crash():
    text = " ".join(f"x{i}" for i in range(5000))
    chunks = chunk_text(text, target_tokens=(500, 900), overlap_tokens=(75, 150))
    assert len(chunks) > 1
    assert all(chunk.strip() for chunk in chunks)


def test_content_hash_stable_for_same_text():
    assert content_hash("hello world") == content_hash("hello world")


def test_content_hash_differs_for_different_text():
    assert content_hash("hello world") != content_hash("hello there")
