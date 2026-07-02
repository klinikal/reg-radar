import hashlib
import re


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _window_split(words: list[str], window: int, overlap: int) -> list[str]:
    chunks = []
    step = max(1, window - overlap)
    for i in range(0, len(words), step):
        window_words = words[i:i + window]
        if window_words:
            chunks.append(" ".join(window_words))
        if i + window >= len(words):
            break
    return chunks


def chunk_text(text: str, target_tokens: tuple[int, int] = (500, 900), overlap_tokens: tuple[int, int] = (75, 150)) -> list[str]:
    # "Tokens" here means whitespace-split words, not a real tokenizer's
    # tokens - a word-count approximation is close enough for chunk sizing
    # and avoids pulling in a tokenizer dependency (e.g. tiktoken) for a
    # job that only needs rough, consistent chunk boundaries.
    text = text.strip()
    if not text:
        return []

    _, target_max = target_tokens
    overlap_min, overlap_max = overlap_tokens

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[str] = []
    current_words: list[str] = []

    for para in paragraphs:
        para_words = para.split()
        if len(para_words) > target_max:
            if current_words:
                chunks.append(" ".join(current_words))
                current_words = []
            chunks.extend(_window_split(para_words, target_max, overlap_max))
            continue
        if current_words and len(current_words) + len(para_words) > target_max:
            chunks.append(" ".join(current_words))
            overlap = current_words[-overlap_min:] if len(current_words) > overlap_min else current_words
            current_words = overlap + para_words
        else:
            current_words.extend(para_words)

    if current_words:
        chunks.append(" ".join(current_words))

    return chunks
