"""Text chunking for TTS — split long text into sentence-sized chunks."""

import re

# Sentence-ending punctuation followed by space or end-of-string
SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+')


def chunk_text(text: str, max_sentences: int = 3) -> list[str]:
    """Split text into chunks of at most max_sentences sentences.

    Short text (<= max_sentences) returns as-is in a single-element list.
    """
    text = text.strip()
    if not text:
        return []

    sentences = SENTENCE_SPLIT.split(text)
    if len(sentences) <= max_sentences:
        return [text]

    chunks = []
    for i in range(0, len(sentences), max_sentences):
        chunk = " ".join(sentences[i:i + max_sentences]).strip()
        if chunk:
            chunks.append(chunk)
    return chunks
