"""Exact source slices; no added context, execution, downloads or truncation."""

import asyncio
from bisect import bisect_right
import hashlib
import re

from app.core.embedding_model import get_embedder, MAX_TOKENS
from app.models.documents import MAX_CHUNKS

CHUNK_RECIPE = "source-slices:v1:target448:overlap32:paragraph-heading"


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_text(text, tokenizer):
    """Tokenizer must implement encode(text, add_special_tokens=True)."""
    lines = [0] + [m.end() for m in re.finditer("\n", text)]
    headings = [(m.start(), m.group(1)) for m in re.finditer(r"(?m)^#{1,6}[ \t]+([^\r\n]+)", text)]
    boundaries = sorted({m.end() for m in re.finditer(r"\n[ \t]*\r?\n", text)} |
                        {offset for offset, _ in headings if offset})
    chunks, start, covered = [], 0, 0
    while covered < len(text):
        # Character bisection is followed by a real-tokenizer check, including specials.
        low, high, end = covered + 1, min(len(text), start + 16000), covered
        while low <= high:
            middle = (low + high) // 2
            count = len(tokenizer.encode(text[start:middle], add_special_tokens=True).ids)
            if count <= 448:
                end, low = middle, middle + 1
            else:
                high = middle - 1
        if end <= covered:
            raise ValueError("document_chunking_failed")
        candidates = [b for b in boundaries if max(covered, start + (end-start)//2) < b <= end]
        if candidates and end < len(text):
            end = candidates[-1]
        piece = text[start:end]
        if not piece.strip():
            if chunks:
                # Extend the exact slice; appending overlap would duplicate source bytes.
                chunks[-1]["text"] = text[chunks[-1]["start_char"]:end]
                chunks[-1]["end_char"] = end
                chunks[-1]["end_line"] = bisect_right(lines, end-1)
                chunks[-1]["content_hash"] = digest(chunks[-1]["text"])
                covered, start = end, end
                continue
            first_content = re.search(r"\S",text[start:start+64000])
            if first_content is None:
                raise ValueError("document_chunking_failed")
            end = start + first_content.end()
            piece = text[start:end]
        encoded = tokenizer.encode(piece, add_special_tokens=True)
        if len(encoded.ids) > MAX_TOKENS:
            raise ValueError("document_chunking_failed")
        section = next((title[:200] for offset, title in reversed(headings) if offset <= start), None)
        chunks.append(dict(position=len(chunks), text=piece, content_hash=digest(piece),
                           start_char=start, end_char=end, start_line=bisect_right(lines, start),
                           end_line=bisect_right(lines, end-1), section=section))
        if len(chunks) > MAX_CHUNKS:
            raise ValueError("document_too_many_chunks")
        covered = end
        offsets = tokenizer.encode(piece, add_special_tokens=False).offsets
        # Overlap uses at most 32 actual source tokens, excluding special tokens.
        overlap_start = start + offsets[-32][0] if len(offsets) > 32 else end
        start = overlap_start if start < overlap_start < end else end
    if not chunks:
        raise ValueError("document_empty")
    if any(len(tokenizer.encode(c["text"],add_special_tokens=True).ids)>MAX_TOKENS
           or len(c["text"])>64000 for c in chunks):
        raise ValueError("document_chunking_failed")
    return chunks


async def local_chunks(text):
    model = get_embedder()

    def run():
        with model.lock:
            model._load()
            return chunk_text(text, model.tokenizer)

    # Shares the loaded model, tokenizer lock and bounded inference executor.
    return await asyncio.get_running_loop().run_in_executor(model.executor, run)
